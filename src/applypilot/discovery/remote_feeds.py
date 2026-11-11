"""Cached public remote-job APIs; no model calls or browser automation."""

import hashlib
import json
import logging
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from applypilot import config
from applypilot.database import get_connection, init_db
from applypilot.discovery.board_feeds import PROVIDERS as BOARD_PROVIDERS
from applypilot.discovery.board_feeds import iter_jobs
from applypilot.discovery.filters import country_allowed as _country_allowed
from applypilot.discovery.filters import title_excluded, title_has_keyword

log = logging.getLogger(__name__)
API_PROVIDERS = {'himalayas', 'remotive', 'jobicy'}
PROVIDERS = API_PROVIDERS | BOARD_PROVIDERS


def _get_json(client: httpx.Client, url: str, params: dict, cache_hours: float) -> dict:
    identity = json.dumps([url, params], sort_keys=True)
    key = hashlib.sha256(identity.encode()).hexdigest()
    cache_dir = config.APP_DIR / 'discovery-cache'
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f'{key}.json'
    if path.exists() and time.time() - path.stat().st_mtime < cache_hours * 3600:
        try:
            cached = json.loads(path.read_text())
            if isinstance(cached, dict) and isinstance(cached.get('jobs'), list):
                return cached
        except (OSError, ValueError):
            pass
    response = client.get(url, params=params)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get('jobs'), list):
        raise TypeError('Unexpected remote jobs response: jobs must be a list')
    with tempfile.NamedTemporaryFile(mode='w', dir=cache_dir, delete=False, encoding='utf-8') as handle:
        json.dump(data, handle)
        temporary = Path(handle.name)
    temporary.replace(path)
    return data


def _salary(row: dict, provider: str) -> str | None:
    if provider == 'remotive':
        return row.get('salary') or None
    low, high = (row.get('minSalary'), row.get('maxSalary')) if provider == 'himalayas' else (
        row.get('salaryMin'), row.get('salaryMax'))
    if low is None and high is None:
        return None
    currency = row.get('currency') if provider == 'himalayas' else row.get('salaryCurrency')
    values = '-'.join(str(value) for value in (low, high) if value is not None)
    return f"{currency or ''} {values}/{row.get('salaryPeriod') or 'yearly'}".strip()


def normalize(row: dict, provider: str, search_cfg: dict) -> dict | None:
    country = search_cfg.get('location', {}).get('country') or search_cfg.get('country', 'India')
    title = row.get('jobTitle') if provider == 'jobicy' else row.get('title')
    employment = row.get('jobType') if provider == 'jobicy' else row.get('employmentType', row.get('job_type', ''))
    employment_text = ', '.join(employment) if isinstance(employment, list) else str(employment or '')
    if not title or title_excluded(title, search_cfg):
        return None
    if search_cfg.get('preferences', {}).get('exclude_internships') and re.search(r'\bintern(?:ship)?s?\b', employment_text, re.IGNORECASE):
        return None
    if not title_has_keyword(title, search_cfg):
        return None
    if provider == 'himalayas':
        restrictions = row.get('locationRestrictions') or []
        if restrictions and country.casefold() not in {str(value).casefold() for value in restrictions}:
            return None
        zones = row.get('timezoneRestrictions') or []
        if country.casefold() == 'india' and zones and not any(str(zone) in {'5.5', 'UTC+05:30', '+05:30', 'Asia/Kolkata'} for zone in zones):
            return None
        location = ', '.join(restrictions) or 'Worldwide'
        if row.get('expiryDate') and float(row['expiryDate']) <= time.time():
            return None
        url, apply_url = row.get('guid'), row.get('applicationLink')
        company = row.get('companyName')
        description = row.get('description') or row.get('excerpt', '')
        published = row.get('pubDate')
        if published:
            published = datetime.fromtimestamp(float(published), UTC).isoformat()
    else:
        location = row.get('candidate_required_location', '') if provider == 'remotive' else row.get('jobGeo', '')
        if not _country_allowed(location, country):
            return None
        url = apply_url = row.get('url')
        company = row.get('company_name') if provider == 'remotive' else row.get('companyName')
        description = row.get('description', '') if provider == 'remotive' else row.get('jobDescription', row.get('jobExcerpt', ''))
        published = row.get('publication_date') if provider == 'remotive' else row.get('pubDate')
    if not url or urlparse(url).scheme not in {'https', 'http'}:
        return None
    if apply_url and urlparse(apply_url).scheme not in {'https', 'http'}:
        apply_url = None
    plain = BeautifulSoup(description, 'html.parser').get_text(' ', strip=True)
    description = f"Employer: {company or 'Not provided'}\nEmployment: {employment_text}\nPublished: {published or 'Not provided'}\nRemote eligibility: {location}\n\n{plain}"
    return {'url': url, 'application_url': apply_url, 'title': title, 'salary': _salary(row, provider),
            'location': f'{location} (Remote)', 'full_description': description,
            'description': description, 'site': provider.title()}


def _responses(client: httpx.Client, provider: str, cfg: dict):
    options = cfg.get('remote_feeds', {})
    # These providers ask for infrequent synchronization; six hours is the minimum cache interval.
    cache_hours = max(6, float(options.get('cache_hours', 6)))
    max_pages = max(1, min(10, int(options.get('max_pages', 2))))
    if provider == 'remotive':
        yield _get_json(client, 'https://remotive.com/api/remote-jobs', {'category': 'software-dev'}, cache_hours)
    elif provider == 'jobicy':
        cursor = None
        for _ in range(max_pages):
            params = {'count': 100, 'industry': 'engineering'}
            if cursor:
                params['cursor'] = cursor
            data = _get_json(client, 'https://jobicy.com/api/jobs', params, cache_hours)
            yield data
            next_cursor = data.get('nextCursor')
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
    elif provider == 'himalayas':
        for query in options.get('queries', ['backend', 'python', 'node']):
            for page in range(1, max_pages + 1):
                params = {'q': query, 'country': cfg.get('country', 'India'), 'sort': 'recent', 'page': page}
                data = _get_json(client, 'https://himalayas.app/jobs/api/search', params, cache_hours)
                yield data
                if not data['jobs']:
                    break
    else:
        raise ValueError(f'Unknown remote feed provider: {provider}')


def _candidates(client: httpx.Client, provider: str, cfg: dict):
    if provider in BOARD_PROVIDERS:
        yield from iter_jobs(client, provider, cfg)
        return
    for data in _responses(client, provider, cfg):
        for raw in data['jobs']:
            try:
                yield normalize(raw, provider, cfg)
            except (ValueError, TypeError, AttributeError):
                yield None


def run_discovery(cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else config.load_search_config()
    providers = cfg.get('remote_feeds', {}).get('providers', sorted(API_PROVIDERS))
    if unknown := set(providers) - PROVIDERS:
        raise ValueError(f'Unknown remote feed providers: {sorted(unknown)}')
    init_db()
    conn = get_connection()
    stats = {'new': 0, 'existing': 0, 'filtered': 0, 'errors': 0}
    now = datetime.now(UTC).isoformat()
    with httpx.Client(timeout=25, follow_redirects=True, headers={'User-Agent': 'ApplyPilot/0.3 (personal job discovery)'}) as client:
        for provider in providers:
            counts = dict.fromkeys(stats, 0)
            seen = set()
            try:
                for job in _candidates(client, provider, cfg):
                    if not job:
                        counts['filtered'] += 1
                        continue
                    if job['url'] in seen:
                        continue
                    seen.add(job['url'])
                    detail_at = now if job.get('detail_ready', True) else None
                    with conn:
                        cursor = conn.execute('''INSERT INTO jobs
                            (url,title,site,salary,location,description,full_description,application_url,strategy,discovered_at,detail_scraped_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO NOTHING''',
                            (job['url'],job['title'],job['site'],job['salary'],job['location'],job['description'],
                             job['full_description'],job['application_url'],'public_remote_api',now,detail_at))
                    counts['new' if cursor.rowcount else 'existing'] += 1
            except (httpx.HTTPError, ValueError, TypeError, OSError) as exc:
                counts['errors'] += 1
                log.error('%s feed failed: %s', provider, exc)
            for key in stats:
                stats[key] += counts[key]
            log.info('%s: %d new | %d existing | %d filtered | %d errors', provider.title(),
                     counts['new'], counts['existing'], counts['filtered'], counts['errors'])
    return stats
