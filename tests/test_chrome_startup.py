"""Real Chrome startup with an existing manually opened worker profile."""

import socket
import subprocess
import time

import httpx
import pytest

from applypilot import config, database
from applypilot.apply import chrome, launcher


def test_manual_profile_owner_is_reported_before_agent_starts(monkeypatch, tmp_path):
    profile = tmp_path / 'manual-profile'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    manual = subprocess.Popen([
        config.get_chrome_path(), '--headless=new', f'--user-data-dir={profile}',
        '--no-first-run', '--no-default-browser-check', 'about:blank',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        for _ in range(100):
            if (profile / 'SingletonSocket').is_symlink():
                break
            time.sleep(0.1)
        assert manual.poll() is None
        monkeypatch.setattr(chrome, 'setup_worker_profile', lambda worker: profile)
        with pytest.raises(RuntimeError, match='Close the manually opened'):
            chrome.launch_chrome(0, port=port, headless=True)
        assert manual.poll() is None
    finally:
        chrome._kill_process_tree(manual.pid)
        manual.wait(timeout=10)
        chrome.cleanup_worker(0, chrome._chrome_procs.get(0))


def test_ready_browser_exposes_debugging_endpoint(monkeypatch, tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(chrome, 'setup_worker_profile', lambda worker: tmp_path / 'ready-profile')
    process = chrome.launch_chrome(0, port=port, headless=True)
    try:
        response = httpx.get(f'http://127.0.0.1:{port}/json/version')
        assert response.json()['webSocketDebuggerUrl'].startswith('ws://')
        assert process.poll() is None
    finally:
        chrome.cleanup_worker(0, process)


def test_startup_failure_stops_batch_without_job_failure(monkeypatch, jobs):
    first = jobs()
    jobs('http://second.test/')
    def fail(*args, **kwargs):
        raise chrome.ChromeStartupError('Close the manually opened worker Chrome.')
    monkeypatch.setattr(launcher, 'launch_chrome', fail)
    monkeypatch.setattr(launcher, 'run_job', lambda *a, **kw: pytest.fail('Agent must not start'))
    assert launcher.worker_loop(limit=2, agent='cursor') == (0, 0)
    rows = database.get_connection().execute('SELECT * FROM jobs').fetchall()
    assert len(rows) == 2
    assert all(row['apply_status'] is None for row in rows)
    assert all(not row['apply_attempts'] for row in rows)
    assert database.get_connection().execute('SELECT apply_status FROM jobs WHERE url=?',(first['url'],)).fetchone()[0] is None
