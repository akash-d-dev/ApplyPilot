"""Public job-board feeds that are not the Himalayas, Remotive, or Jobicy APIs."""

import hashlib
import json
import logging
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from applypilot import config
from applypilot.discovery.filters import (
    country_allowed,
    discovery_eligible,
    remote_only,
    target_country,
    title_has_keyword,
)

log = logging.getLogger(__name__)
PROVIDERS = {'remoteok', 'weworkremotely', 'hirist', 'wellfound'}

_WWR_FEEDS = (
    'https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss',
    'https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss',
)
_WELLFOUND_ROLES = ('software-engineer', 'backend-engineer')
_HIRIST_CATEGORIES = (1, 16)  # Backend Development, Full Stack


def _cached_text(client: httpx.Client, url: str, params: dict | None, cache_hours: float,
                 headers: dict | None = None) -> str:
    identity = json.dumps([url, params or {}, headers or {}], sort_keys=True)
    key = hashlib.sha256(identity.encode()).hexdigest()
    cache_dir = config.APP_DIR / 'discovery-cache'
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f'{key}.txt'
    if path.exists() and time.time() - path.stat().st_mtime < cache_hours * 3600:
        try:
            return path.read_text(encoding='utf-8')
        except OSError:
            pass
    response = client.get(url, params=params, headers=headers)
    response.raise_for_status()
    text = response.text
    with tempfile.NamedTemporaryFile(mode='w', dir=cache_dir, delete=False, encoding='utf-8') as handle:
        handle.write(text)
        temporary = Path(handle.name)
    temporary.replace(path)
    return text


def _plain(value: str | None) -> str:
    return BeautifulSoup(value or '', 'html.parser').get_text(' ', strip=True)


def _cache_hours(cfg: dict) -> float:
    # These providers ask for infrequent synchronization; six hours is the minimum cache interval.
    return max(6, float((cfg.get('remote_feeds') or {}).get('cache_hours', 6)))


def _pages(cfg: dict) -> int:
    return max(1, min(10, int((cfg.get('remote_feeds') or {}).get('max_pages', 2))))


def _record(title: str, location: str, url: str, apply_url: str | None, company: str | None,
            description: str, site: str, search_cfg: dict, employment: str = '', salary: str | None = None) -> dict | None:
    if not title or not title_has_keyword(title, search_cfg):
        return None
    if not discovery_eligible(title, location, search_cfg, employment):
        return None
    if not url or urlparse(url).scheme not in {'https', 'http'}:
        return None
    if apply_url and urlparse(apply_url).scheme not in {'https', 'http'}:
        apply_url = None
    body = (
        f"Employer: {company or 'Not provided'}\n"
        f"Employment: {employment or 'Not provided'}\n"
        f"Remote eligibility: {location}\n\n{description}"
    )
    return {
        'url': url, 'application_url': apply_url, 'title': title, 'salary': salary,
        'location': location, 'full_description': body, 'description': body, 'site': site,
        'detail_ready': bool(description),
    }


def normalize_remoteok(row: dict, search_cfg: dict) -> dict | None:
    title = row.get('position') or row.get('title')
    place = str(row.get('location') or '').strip()
    location = f'{place} (Remote)' if place else 'Worldwide (Remote)'
    low, high = row.get('salary_min'), row.get('salary_max')
    salary = None
    if str(low or '').isdigit() and int(low) > 0:
        salary = f"{low}-{high}" if str(high or '').isdigit() and int(high) > 0 else str(low)
    return _record(
        title, location, row.get('url') or '', row.get('apply_url') or row.get('url'),
        row.get('company'), _plain(row.get('description')), 'RemoteOK', search_cfg, salary=salary)


def parse_weworkremotely(xml_text: str, search_cfg: dict) -> list[dict | None]:
    root = ET.fromstring(xml_text)
    jobs = []
    for item in root.findall('./channel/item'):
        raw_title = (item.findtext('title') or '').strip()
        company, _, role = raw_title.partition(':')
        title = role.strip() if role.strip() else raw_title
        country = re.sub(r'[^\w\s,&.-]', ' ', (item.findtext('country') or ''))
        region = (item.findtext('region') or '').strip()
        place = country.strip() or region or 'Anywhere'
        configured = target_country(search_cfg)
        if country.strip() and configured and not country_allowed(country, configured):
            jobs.append(None)
            continue
        location = f'{place} (Remote)'
        jobs.append(_record(
            title, location, (item.findtext('link') or '').strip(), (item.findtext('link') or '').strip(),
            company.strip() or None, _plain(item.findtext('description')), 'WeWorkRemotely', search_cfg,
            (item.findtext('type') or '').strip()))
    return jobs


def normalize_hirist(row: dict, search_cfg: dict) -> dict | None:
    title = row.get('jobdesignation') or row.get('title')
    places = [item.get('name') for item in (row.get('locations') or row.get('location') or []) if isinstance(item, dict)]
    remote = bool(row.get('workFromHome'))
    if remote_only(search_cfg) and not remote:
        return None
    place = ', '.join(name for name in places if name) or 'India'
    location = f'{place}, India (Remote)' if remote else f'{place}, India'
    company = (row.get('companyData') or {}).get('companyName')
    tags = ', '.join(tag.get('name', '') for tag in (row.get('tags') or []) if isinstance(tag, dict))
    experience = f"{row.get('min')}-{row.get('max')} years" if row.get('min') is not None else 'Not provided'
    description = f"Experience: {experience}\nSkills: {tags or 'Not provided'}"
    job_id = row.get('id')
    url = f'https://www.hirist.tech/j/{job_id}' if job_id else ''
    record = _record(title, location, url, url, company, description, 'Hirist', search_cfg)
    if record is not None:
        record['detail_ready'] = False
    return record


def jobs_from_wellfound(html: str) -> list[dict]:
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not match:
        raise ValueError('Wellfound page has no job data')
    apollo = json.loads(match.group(1))['props']['pageProps']['apolloState']['data']
    companies: dict[str, str] = {}
    for value in apollo.values():
        if not isinstance(value, dict) or value.get('__typename') != 'StartupResult':
            continue
        for ref in value.get('highlightedJobListings') or []:
            job_id = str((ref or {}).get('__ref', '')).rsplit(':', 1)[-1]
            if job_id:
                companies[job_id] = value.get('name') or ''
    jobs = []
    for value in apollo.values():
        if isinstance(value, dict) and value.get('__typename') == 'JobListingSearchResult':
            jobs.append({**value, 'companyName': companies.get(str(value.get('id')))})
    return jobs


def normalize_wellfound(row: dict, search_cfg: dict) -> dict | None:
    kind = (row.get('remoteConfig') or {}).get('kind')
    remote = bool(row.get('remote')) and kind != 'ONSITE'
    places = row.get('acceptedRemoteLocationNames') or row.get('locationNames') or []
    place = ', '.join(str(name) for name in places if name) or 'Remote'
    location = f'{place} (Remote)' if remote else place
    job_id = row.get('id')
    slug = row.get('slug') or 'job'
    url = f'https://wellfound.com/jobs/{job_id}-{slug}' if job_id else ''
    return _record(
        row.get('title'), location, url, url, row.get('companyName'),
        _plain(row.get('description')), 'Wellfound', search_cfg, str(row.get('jobType') or ''))


def _fetch_many(client: httpx.Client, provider: str, cfg: dict):
    """Yield (payload text or parsed rows marker). Raises if every request fails."""
    options = cfg.get('remote_feeds') or {}
    hours = _cache_hours(cfg)
    failures = 0
    attempts = 0
    if provider == 'remoteok':
        attempts = 1
        tags = options.get('remoteok_tags', 'dev')
        try:
            yield json.loads(_cached_text(client, 'https://remoteok.com/api', {'tags': tags}, hours))
        except (httpx.HTTPError, ValueError, OSError) as exc:
            log.error('RemoteOK feed failed: %s', exc)
            failures += 1
    elif provider == 'weworkremotely':
        for url in options.get('weworkremotely_feeds', _WWR_FEEDS):
            attempts += 1
            try:
                yield _cached_text(client, url, None, hours)
            except (httpx.HTTPError, ValueError, OSError) as exc:
                failures += 1
                log.error('WeWorkRemotely feed failed: %s', exc)
    elif provider == 'hirist':
        categories = options.get('hirist_categories', _HIRIST_CATEGORIES)
        for category in categories:
            for page in range(_pages(cfg)):
                attempts += 1
                try:
                    text = _cached_text(
                        client, 'https://gladiator.hirist.tech/job/category/',
                        {'categoryId': category, 'size': 50, 'page': page}, hours, {'version': '2'})
                    yield json.loads(text)
                except (httpx.HTTPError, ValueError, OSError) as exc:
                    failures += 1
                    log.error('Hirist category %s page %s failed: %s', category, page, exc)
    elif provider == 'wellfound':
        for role in options.get('wellfound_roles', _WELLFOUND_ROLES):
            attempts += 1
            try:
                yield _cached_text(client, f'https://wellfound.com/role/r/{role}', None, hours)
            except (httpx.HTTPError, ValueError, OSError) as exc:
                failures += 1
                log.error('Wellfound role %s failed: %s', role, exc)
    else:
        raise ValueError(f'Unknown board provider: {provider}')
    if attempts and failures == attempts:
        raise RuntimeError(f'{provider} feed failed')


def _rows(provider: str, payload, cfg: dict):
    if provider == 'remoteok':
        rows = payload if isinstance(payload, list) else []
        for row in rows:
            if isinstance(row, dict):
                yield normalize_remoteok(row, cfg)
    elif provider == 'weworkremotely':
        yield from parse_weworkremotely(payload, cfg)
    elif provider == 'hirist':
        for row in (payload.get('data') or []) if isinstance(payload, dict) else []:
            if isinstance(row, dict):
                yield normalize_hirist(row, cfg)
    else:
        for row in jobs_from_wellfound(payload):
            yield normalize_wellfound(row, cfg)


def iter_jobs(client: httpx.Client, provider: str, cfg: dict):
    parsed = 0
    for payload in _fetch_many(client, provider, cfg):
        try:
            rows = list(_rows(provider, payload, cfg))
        except (ValueError, TypeError, KeyError, ET.ParseError) as exc:
            log.error('%s payload failed: %s', provider, exc)
            continue
        parsed += 1
        yield from rows
    if not parsed:
        raise RuntimeError(f'{provider} feed failed')
