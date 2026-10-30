"""Plain progress messages built from job records and local counters."""

import logging
import re
import threading

from applypilot.providers import redact

log = logging.getLogger(__name__)


def configure_logging() -> None:
    """Keep transport chatter quiet while preserving warnings and errors."""
    for name in ('httpx', 'httpcore'):
        logging.getLogger(name).setLevel(logging.WARNING)


def model_details(provider: str, model: str) -> str:
    """Describe the model selection used by the request adapters."""
    mode = ' | standard (fast=false)' if provider == 'cursor' and model == 'composer-2.5' else ''
    return f'{provider} | {model}{mode}'


def job_details(job: dict) -> str:
    """Use existing scraped fields without generating descriptions or inferring facts."""
    fields = [job.get('title') or 'Untitled job']
    if job.get('company'):
        fields.append(f"company={job['company']}")
    if job.get('location'):
        fields.append(job['location'])
    if job.get('site'):
        fields.append(f"source={job['site']}")
    if job.get('fit_score') is not None:
        fields.append(f"fit={job['fit_score']}/10")
    return redact(' | '.join(re.sub(r'[\x00-\x1f\x7f]', ' ', str(value)) for value in fields))


def log_saved_counts(stats: dict) -> None:
    """Separate persisted totals from counts for the current run."""
    log.info('Saved totals: jobs=%d | scored=%d | resumes=%d | cover_letters=%d | applied=%d | previewed=%d',
             stats['total'], stats['scored'], stats['tailored'], stats['with_cover_letter'],
             stats['applied'], stats['previewed'])


class BatchProgress:
    """Track successful and failed records in one preparation stage."""

    def __init__(self, stage: str, success_label: str, total: int) -> None:
        self.stage = stage
        self.success_label = success_label
        self.total = total
        self.processed = 0
        self.succeeded = 0
        self.failed = 0
        self._log('START')

    def start_job(self, job: dict) -> None:
        """Identify the job before its potentially slow AI request."""
        log.info('%s [%d/%d] START | %s', self.stage, self.processed + 1, self.total, job_details(job))

    def record(self, job: dict, success: bool, detail: str = '') -> None:
        """Report a result after its files and database record have been saved."""
        self.processed += 1
        self.succeeded += int(success)
        self.failed += int(not success)
        self._log(f"{'OK' if success else 'FAILED'} | {job_details(job)}" + (f' | {detail}' if detail else ''))

    def _log(self, detail: str) -> None:
        log.info('%s: processed=%d/%d | %s=%d | failed=%d | %s',
                 self.stage, self.processed, self.total, self.success_label, self.succeeded, self.failed, detail)


class ApplicationProgress:
    """Share submission and preview counters across browser workers."""

    def __init__(self) -> None:
        self.applied = 0
        self.previewed = 0
        self.failed = 0
        self._lock = threading.Lock()
        self._log('START')

    def record(self, result: str, job: dict, worker_id: int) -> None:
        """Increment only the corresponding persisted outcome, under a shared lock."""
        with self._lock:
            if result == 'applied':
                self.applied += 1
            elif result == 'previewed':
                self.previewed += 1
            elif result != 'skipped':
                self.failed += 1
            self._log(f'[W{worker_id}] {result.upper()} | {job_details(job)}')

    def _log(self, detail: str) -> None:
        log.info('Applications this run: applied=%d | previewed=%d | failed=%d | %s',
                 self.applied, self.previewed, self.failed, detail)
