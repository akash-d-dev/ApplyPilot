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


@pytest.mark.parametrize('dry_run', [True, False])
def test_login_prefers_google_then_email_without_linkedin_provider(jobs, dry_run):
    instructions = prompt.build_prompt(jobs(), 'Synthetic fixture', dry_run=dry_run)
    login = instructions.split('5. Login wall?', 1)[1].split('6. Upload resume.', 1)[0]
    assert login.index('Reuse the existing website session') < login.index('Continue with Google')
    assert 'Do not choose Continue with LinkedIn' in login
    assert 'FIRST provider choice: Continue with LinkedIn' not in login
    assert login.index('Continue with Google') < login.index('Email-code fallback')
    assert login.index('Email-code fallback') < login.index('Password fallback')
    assert 'accounts.google.com' in login
    assert 'exactly matches applicant@example.test' in login
    assert 'Never enter a Google password' in login
    assert 'new OAuth permissions' in login
    assert 'never use an old code' in login
    assert 'Account creation is not enabled' in login
    assert 'No password is configured' in login
    assert 'Do not automate other SSO providers' in login
    assert 'Try sign up' not in login
    assert 'Do NOT try to sign in to Google/Microsoft/SSO' not in login


@pytest.mark.parametrize('dry_run', [True, False])
def test_portal_signup_is_opt_in_and_credentials_are_redacted(monkeypatch, jobs, dry_run):
    from applypilot.providers import redact
    monkeypatch.setenv('JOB_PORTAL_SIGNUP', '1')
    monkeypatch.setenv('JOB_PORTAL_PASSWORD', 'synthetic-portal-password')
    instructions = prompt.build_prompt(jobs(), 'Synthetic fixture', dry_run=dry_run)
    assert 'Job-portal account creation' in instructions
    assert 'synthetic-portal-password' in instructions
    assert 'never use an old code' in instructions
    assert 'Never create a LinkedIn or Google account' in instructions
    assert 'posting, messaging, contacts' in instructions
    assert 'synthetic-portal-password' not in redact(instructions)
    if dry_run:
        assert 'Do NOT click the final Submit/Apply button' in instructions


def test_workers_have_separate_uploads(jobs, tmp_path):
    first = jobs()
    second = jobs("http://second.test/")
    first_text = prompt.build_prompt(first, "first", upload_dir=tmp_path / "worker-0" / "uploads")
    second_text = prompt.build_prompt(second, "second", upload_dir=tmp_path / "worker-1" / "uploads")
    assert str(tmp_path / "worker-0" / "uploads") in first_text
    assert str(tmp_path / "worker-1" / "uploads") in second_text


@pytest.mark.parametrize('dry_run', [True, False])
def test_recent_site_resume_reuse_overrides_mandatory_upload(jobs, dry_run):
    instructions = prompt.build_prompt(jobs(), 'Synthetic fixture', dry_run=dry_run)
    assert 'less than 24 hours ago' in instructions
    assert 'do not delete or re-upload it' in instructions
    assert 'its age is unknown, it is 24 hours old or older' in instructions
    assert "not the local PDF's modification time" in instructions
    assert 'Current reference time:' in instructions
    assert "Apply step 6's resume reuse rule first" in instructions
    assert 'ALWAYS upload fresh' not in instructions


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


def test_failed_prepared_jobs_are_not_reported_ready(jobs):
    for index in range(6):
        jobs(f'http://fixture.test/{index}', status='failed')
    stats = database.get_stats()
    assert stats['ready_to_apply'] == 0
    assert stats['prepared_unsubmitted'] == 6
    assert stats['failed_prepared'] == 6
    assert launcher.acquire_job(dry_run=False) is None


def test_ready_count_matches_queue_filters(jobs, monkeypatch):
    jobs('http://ready.test/')
    jobs('http://previewed.test/', status='previewed')
    jobs('http://failed.test/', status='failed')
    jobs('http://in-progress.test/', status='in_progress')
    jobs('http://exhausted.test/')
    jobs('http://low-score.test/')
    jobs('http://blocked.test/')
    conn = database.get_connection()
    conn.execute("UPDATE jobs SET apply_attempts=99 WHERE url='http://exhausted.test/'")
    conn.execute("UPDATE jobs SET fit_score=4 WHERE url='http://low-score.test/'")
    conn.commit()
    monkeypatch.setattr('applypilot.config.load_blocked_sites', lambda: (set(), ['%blocked.test%']))
    assert database.get_stats()['ready_to_apply'] == 2
    assert len(database.get_jobs_by_stage(stage='pending_apply')) == 2
    selected = [launcher.acquire_job(dry_run=False), launcher.acquire_job(dry_run=False)]
    assert {row['url'] for row in selected} == {'http://ready.test/', 'http://previewed.test/'}
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
