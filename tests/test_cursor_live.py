"""Opt-in Cursor SDK tests against a local form, never employer applications."""

import json
import os
from pathlib import Path

import pytest
from dotenv import dotenv_values
from playwright.sync_api import sync_playwright

from applypilot import config, database
from applypilot.apply import dashboard, launcher

pytestmark = pytest.mark.skipif(os.environ.get("APPLYPILOT_LIVE_CURSOR") != "1", reason="Opt-in paid Cursor test")


def test_real_cursor_scoring_preview_and_submission(monkeypatch, jobs, application_site, browser_worker):
    """Prove real SDK text output, MCP tools, preview state, upload, and one submission."""
    settings = dotenv_values(Path.home() / ".applypilot" / ".env")
    if not settings.get("CURSOR_API_KEY"):
        pytest.skip("Run applypilot configure-cursor first")
    for name in ("CURSOR_API_KEY", "CURSOR_MODEL", "LLM_MODEL", "LLM_PROVIDER", "APPLY_AGENT"):
        if settings.get(name):
            monkeypatch.setenv(name, settings[name])
    monkeypatch.setenv("LLM_PROVIDER", "cursor")
    url, submissions = application_site
    job = jobs(url)
    from applypilot.scoring.scorer import score_job

    score = score_job("Synthetic test engineer with Python and Playwright experience.", job)
    assert 1 <= score["score"] <= 10

    original_mcp = launcher._make_mcp_config

    def local_mcp(port):
        servers = original_mcp(port)
        del servers["mcpServers"]["gmail"]
        return servers

    monkeypatch.setattr(launcher, "_make_mcp_config", local_mcp)
    captured = []
    original_capture = launcher._capture_review

    def capture(port, destination):
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = browser.contexts[0].pages[-1]
            captured.append(
                {
                    "name": page.get_by_label("Full name").input_value(),
                    "email": page.get_by_label("Email", exact=True).input_value(),
                    "resume": page.get_by_label("Resume PDF").evaluate("input => input.files.length"),
                }
            )
            browser.close()
        original_capture(port, destination)

    monkeypatch.setattr(launcher, "_capture_review", capture)
    from applypilot.providers import agent_model

    model = agent_model("cursor")
    dashboard.init_worker(0)
    launcher._stop_event.clear()
    assert launcher.worker_loop(dry_run=True, agent="cursor", model=model) == (0, 0), _logs()
    assert submissions == []
    assert database.get_stats()["previewed"] == 1, _logs()
    assert captured[0] == {"name": "Synthetic Test Applicant", "email": "applicant@example.test", "resume": 1}
    assert list(config.LOG_DIR.glob("*/review.png"))
    assert launcher.worker_loop(dry_run=False, agent="cursor", model=model) == (1, 0), _logs()
    assert len(submissions) == 1
    assert b"application/pdf" in submissions[0]
    assert database.get_stats()["applied"] == 1
    print(f"Live Cursor validation artifacts: {config.LOG_DIR}")


def _logs():
    summaries = [json.loads(path.read_text()) for path in config.LOG_DIR.glob("*/summary.json")]
    messages = [path.read_text()[-3000:] for path in config.LOG_DIR.glob("*/agent.log")]
    return {"summaries": summaries, "messages": messages}
