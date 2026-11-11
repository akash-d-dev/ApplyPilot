"""Deterministic discovery preferences, also used by the application queue."""

import re

DEFAULT_TITLE_KEYWORDS = (
    'backend', 'back-end', 'back end', 'python', 'node', 'full stack', 'full-stack', 'software engineer',
)
_REMOTE_HINTS = ('remote', 'anywhere', 'work from home', 'wfh', 'distributed', 'worldwide')


def _term_pattern(term: str) -> str:
    words = [re.escape(word) for word in term.split() if word.strip()]
    if len(words) <= 1:
        return words[0] if words else ''
    # "staff engineer" also matches "Staff Software Engineer".
    return r'(?:\s+\S+){0,2}\s+'.join(words)


def excluded_title_pattern(search_cfg: dict) -> str:
    terms = list(search_cfg.get('exclude_titles', []))
    if search_cfg.get('preferences', {}).get('exclude_internships'):
        terms.extend(['intern', 'interns', 'internship', 'internships'])
    patterns = [_term_pattern(term) for term in terms if isinstance(term, str) and term.strip()]
    patterns = [pattern for pattern in patterns if pattern]
    return r'(?i)(?<!\w)(?:' + '|'.join(patterns) + r')(?!\w)' if patterns else ''


def title_excluded(title: str | None, search_cfg: dict) -> bool:
    pattern = excluded_title_pattern(search_cfg)
    return bool(pattern and re.search(pattern, title or ''))


def title_has_keyword(title: str | None, search_cfg: dict) -> bool:
    keywords = (search_cfg.get('remote_feeds') or {}).get('title_keywords') or DEFAULT_TITLE_KEYWORDS
    folded = (title or '').casefold()
    return any(str(keyword).casefold() in folded for keyword in keywords)


def target_country(search_cfg: dict) -> str:
    location = search_cfg.get('location') or {}
    return location.get('country') or search_cfg.get('country') or ''


def remote_only(search_cfg: dict) -> bool:
    location = search_cfg.get('location') or {}
    return bool((search_cfg.get('preferences') or {}).get('remote_only') or location.get('remote_only'))


def country_allowed(location: str, country: str) -> bool:
    if re.search(rf'\b(?:except|excluding|not available in)\b[^.;]*\b{re.escape(country)}\b', location, re.IGNORECASE):
        return False
    regions = '|apac|asia' if country.casefold() == 'india' else ''
    if re.search(rf'\b(?:{re.escape(country)}|worldwide|anywhere|global{regions})\b', location, re.IGNORECASE):
        return True
    # "Remote" with no named country stays eligible. A named other country does not.
    unnamed = re.sub(
        r'\b(?:remote|work from home|wfh|distributed|hybrid|onsite|on-site)\b', ' ', location, flags=re.IGNORECASE)
    unnamed = re.sub(r'[^a-z]+', ' ', unnamed, flags=re.IGNORECASE)
    return not unnamed.strip()


def _accept_reject(search_cfg: dict) -> tuple[list[str], list[str]]:
    location = search_cfg.get('location') or {}
    accept = search_cfg.get('location_accept') or location.get('accept_patterns') or []
    reject = search_cfg.get('location_reject_non_remote') or location.get('reject_patterns') or []
    return list(accept), list(reject)


def location_eligible(location: str | None, search_cfg: dict) -> bool:
    """Remote-only searches need a remote marker and the configured country or an open geography."""
    if remote_only(search_cfg):
        if not location or not any(hint in location.casefold() for hint in _REMOTE_HINTS):
            return False
        country = target_country(search_cfg)
        return country_allowed(location, country) if country else True

    if not location:
        return True
    folded = location.casefold()
    if any(hint in folded for hint in _REMOTE_HINTS):
        return True
    accept, reject = _accept_reject(search_cfg)
    if any(item.casefold() in folded for item in reject):
        return False
    return any(item.casefold() in folded for item in accept)


def discovery_eligible(title: str | None, location: str | None, search_cfg: dict,
                       employment: str = '') -> bool:
    if title_excluded(title, search_cfg):
        return False
    if (search_cfg.get('preferences') or {}).get('exclude_internships') and re.search(
            r'\bintern(?:ship)?s?\b', employment or '', re.IGNORECASE):
        return False
    return location_eligible(location, search_cfg)
