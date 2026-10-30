"""Real local browser and database flow, with deterministic agent decisions."""

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

from applypilot import config, database
from applypilot.apply import agents, dashboard, launcher


def test_fill_then_explicit_submit_once(monkeypatch, jobs, application_site, tmp_path):
    url, submissions = application_site
    job = jobs(url)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(launcher, "BASE_CDP_PORT", port)
    driver = Path(__file__).parent / "fixtures" / "browser_agent.py"

    def launch(worker_id, port, headless):
        process = subprocess.Popen(
            [
                config.get_chrome_path(),
                "--headless=new",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={tmp_path / 'browser'}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/json/version").is_success:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        return process

    def command(agent, model, mcp_path, dry_run):
        result = [
            sys.executable,
            str(driver),
            "--port",
            str(port),
            "--url",
            url,
            "--resume",
            job["tailored_resume_path"],
        ]
        return result + ([] if dry_run else ["--submit"])

    monkeypatch.setattr(launcher, "launch_chrome", launch)
    monkeypatch.setattr(agents, "build_command", command)
    dashboard.init_worker(0)
    launcher._stop_event.clear()
    assert launcher.worker_loop(dry_run=True, agent="cursor") == (0, 0)
    assert submissions == []
    assert database.get_stats()["previewed"] == 1
    row = database.get_connection().execute("SELECT * FROM jobs").fetchone()
    assert row["applied_at"] is None
    assert row["apply_attempts"] == 0
    artifacts = list(config.LOG_DIR.glob("*/review.png"))
    assert len(artifacts) == 1 and artifacts[0].stat().st_size > 1000
    summaries = list(config.LOG_DIR.glob("*/summary.json"))
    assert len(summaries) == 1
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(artifacts[0].as_uri())
        assert page.locator("img").evaluate("image => image.naturalWidth") > 500
        browser.close()

    assert launcher.worker_loop(dry_run=False, agent="cursor") == (1, 0)
    assert len(submissions) == 1
    assert b"Synthetic Test Applicant" in submissions[0]
    assert b"applicant@example.test" in submissions[0]
    assert b"application/pdf" in submissions[0]
    assert database.get_stats()["applied"] == 1
    assert launcher.worker_loop(dry_run=False, agent="cursor") == (0, 0)
    assert len(submissions) == 1
