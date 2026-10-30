"""Preview/submission contracts, selection, and CLI behavior."""

import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from applypilot import database
from applypilot.apply import dashboard, launcher, prompt
from applypilot.apply.agents import build_command, result_status
from applypilot.apply.cursor_hook import allowed, install_hook
from applypilot.cli import app


def test_preview_prompt_has_no_submission_steps(jobs):
    job = jobs()
    instructions = prompt.build_prompt(job, "Synthetic fixture", dry_run=True)
    assert "RESULT:PREVIEWED" in instructions
    assert "RESULT:APPLIED" not in instructions
    assert "send_email with subject" not in instructions
    assert "11. After submit:" not in instructions
    assert "Click Submit if needed" not in instructions
    assert "Do NOT click the final Submit/Apply button" in instructions


def test_workers_have_separate_uploads(jobs, tmp_path):
    first = jobs()
    second = jobs("http://second.test/")
    first_text = prompt.build_prompt(first, "first", upload_dir=tmp_path / "worker-0" / "uploads")
    second_text = prompt.build_prompt(second, "second", upload_dir=tmp_path / "worker-1" / "uploads")
    assert str(tmp_path / "worker-0" / "uploads") in first_text
    assert str(tmp_path / "worker-1" / "uploads") in second_text


def test_preview_preserves_attempts_and_is_submit_eligible(jobs):
    job = jobs()
    conn = database.get_connection()
    conn.execute("UPDATE jobs SET apply_attempts = 2")
    conn.commit()
    assert launcher.acquire_job(dry_run=True)["url"] == job["url"]
    launcher.mark_result(job["url"], "previewed", duration_ms=123)
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["applied_at"] is None
    assert row["apply_attempts"] == 2
    assert row["apply_error"] is None
    assert launcher.acquire_job(dry_run=True) is None
    assert launcher.acquire_job(dry_run=False)["url"] == job["url"]
    launcher.mark_result(job["url"], "applied")
    assert launcher.acquire_job(target_url=job["url"]) is None
    assert database.get_stats()["applied"] == 1


def test_failed_submission_not_automatically_retried(jobs):
    jobs(status="failed")
    assert launcher.acquire_job(dry_run=False) is None


def test_reset_failed_does_not_reset_previews(jobs):
    jobs(status="previewed")
    assert launcher.reset_failed() == 0
    assert database.get_stats()["previewed"] == 1


def test_atomic_acquisition_across_workers(jobs):
    jobs()

    def acquire(worker):
        try:
            return launcher.acquire_job(worker_id=worker, dry_run=True)
        finally:
            database.close_connection()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(acquire, [0, 1]))
    assert sum(result is not None for result in results) == 1


@pytest.mark.parametrize(
    ("text", "dry_run", "status"),
    [
        ("RESULT:PREVIEWED", True, "previewed"),
        ("**RESULT:PREVIEWED**", True, "previewed"),
        ("`RESULT:APPLIED`", False, "applied"),
        ("RESULT:APPLIED", False, "applied"),
        ("RESULT:APPLIED", True, "failed:unexpected_submission_in_preview"),
        ("RESULT:PREVIEWED", False, "failed:submission_not_confirmed"),
        ("RESULT:FAILED:submission_uncertain", False, "failed:submission_uncertain"),
        ("quoted RESULT:APPLIED example", False, "failed:invalid_result_line"),
        ("RESULT:APPLIED\nRESULT:APPLIED", False, "failed:invalid_result_line"),
    ],
)
def test_terminal_result_contract(text, dry_run, status):
    assert result_status(text, dry_run) == status


@pytest.mark.parametrize(
    ("name", "preview", "expected"),
    [
        ("search_emails", True, True),
        ("read_email", True, True),
        ("send_email", True, False),
        ("send_email", False, True),
        ("delete_email", False, False),
        ("draft_email", False, False),
        ("create_filter", False, False),
        ("unknown_future_tool", False, False),
    ],
)
def test_gmail_policy(name, preview, expected):
    assert allowed({"mcp_server_name": "gmail", "tool_name": name}, preview) is expected


def test_hook_installed_with_failure_blocking(tmp_path):
    install_hook(tmp_path, True)
    hooks = json.loads((tmp_path / ".cursor" / "hooks.json").read_text())
    hook = hooks["hooks"]["beforeMCPExecution"][0]
    assert hook["failClosed"] is True
    assert "preview" in hook["command"]
    assert not allowed({"mcp_server_name": "unexpected", "tool_name": "send_email"}, False)


def test_claude_email_denied_in_preview(tmp_path):
    command = build_command("claude", "haiku", tmp_path / "mcp.json", True)
    assert "mcp__gmail__send_email" in command[command.index("--disallowedTools") + 1]
    command = build_command("cursor", "test-model", tmp_path / "mcp.json", True)
    assert command[0].endswith("python")
    assert "--dry-run" in command


@pytest.mark.parametrize(
    ("extra", "agent", "preview", "model"),
    [
        ([], "cursor", True, "composer-2.5"),
        (["--submit"], "cursor", False, "composer-2.5"),
        (["--agent", "claude"], "claude", False, "haiku"),
        (["--model", "chosen"], "cursor", True, "chosen"),
    ],
)
def test_cli_resolves_agent_model_and_mode(monkeypatch, jobs, extra, agent, preview, model):
    jobs()
    monkeypatch.setenv("CURSOR_API_KEY", "synthetic-placeholder")
    runner = MagicMock()
    monkeypatch.setattr(launcher, "main", runner)
    response = CliRunner().invoke(app, ["apply", *extra])
    assert response.exit_code == 0, response.output
    arguments = runner.call_args.kwargs
    assert arguments["agent"] == agent
    assert arguments["dry_run"] is preview
    assert arguments["model"] == model


def test_conflicting_submission_flags_rejected(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "synthetic-placeholder")
    response = CliRunner().invoke(app, ["apply", "--submit", "--dry-run"])
    assert response.exit_code == 2
    assert "cannot be combined" in response.output


def test_skip_restores_preview_status(monkeypatch, jobs):
    job = jobs(status="previewed")
    dashboard.init_worker(0)
    launcher._stop_event.clear()
    monkeypatch.setattr(launcher, "launch_chrome", lambda *args, **kwargs: None)

    def skipped(*args, **kwargs):
        launcher._stop_event.set()
        return "skipped", 1

    monkeypatch.setattr(launcher, "run_job", skipped)
    launcher.worker_loop(target_url=job["url"])
    assert database.get_stats()["previewed"] == 1
    assert database.get_connection().execute("SELECT agent_id FROM jobs").fetchone()[0] is None
    launcher._stop_event.clear()
