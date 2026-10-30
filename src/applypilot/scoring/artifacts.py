"""Stable document names scoped to the discovered job's primary key."""

import hashlib
import re


def job_document_prefix(job: dict) -> str:
    """Keep names readable while separating jobs with identical titles."""
    site = re.sub(r'[^\w\s-]', '', job.get('site') or 'job')[:20].strip().replace(' ', '_')
    title = re.sub(r'[^\w\s-]', '', job.get('title') or 'role')[:50].strip().replace(' ', '_')
    identity = hashlib.sha256(job['url'].encode('utf-8')).hexdigest()[:16]
    return f'{site}_{title}_{identity}'
