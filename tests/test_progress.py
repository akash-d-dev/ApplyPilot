"""User-visible progress reflects saved work without extra model requests."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from typer.testing import CliRunner

from applypilot import config, database, llm
from applypilot.apply import launcher
from applypilot.cli import app
from applypilot.scoring import scorer


def test_cli_shows_model_counts_and_job_details_without_http_noise(monkeypatch, jobs, caplog):
    first = jobs()
    jobs('http://second.test/')
    conn = database.get_connection()
    conn.execute('UPDATE jobs SET fit_score=NULL, tailored_resume_path=NULL')
    conn.commit()
    config.RESUME_PATH.write_text('Synthetic resume')
    monkeypatch.setattr(scorer, 'RESUME_PATH', config.RESUME_PATH)
    monkeypatch.setenv('CURSOR_API_KEY', 'synthetic-placeholder')
    monkeypatch.setattr(llm, '_instance', SimpleNamespace(model='composer-2.5'))

    def score(resume, job):
        logging.getLogger('httpx').info('HTTP Request: noisy transport event')
        logging.getLogger('httpx').warning('Transport warning remains visible')
        return {'score': 8, 'keywords': 'Python', 'reasoning': 'Fixture match'}

    scoring = MagicMock(side_effect=score)
    monkeypatch.setattr(scorer, 'score_job', scoring)
    with caplog.at_level(logging.INFO):
        result = CliRunner().invoke(app, ['run', 'score'])
    assert result.exit_code == 0, result.output
    assert 'composer-2.5' in caplog.text and 'fast=false' in caplog.text
    assert 'applied=0' in caplog.text
    assert 'scored=0' in caplog.text and 'scored=2' in caplog.text
    assert first['title'] in caplog.text
    assert 'source=Fixture' in caplog.text
    assert 'fit=8/10' in caplog.text
    assert 'HTTP Request:' not in caplog.text
    assert 'Transport warning remains visible' in caplog.text
    assert scoring.call_count == 2


def test_score_progress_is_saved_before_starting_next_job(monkeypatch, jobs):
    jobs()
    jobs('http://second.test/')
    conn = database.get_connection()
    conn.execute('UPDATE jobs SET fit_score=NULL')
    conn.commit()
    config.RESUME_PATH.write_text('Synthetic resume')
    monkeypatch.setattr(scorer, 'RESUME_PATH', config.RESUME_PATH)
    calls = 0

    def score(resume, job):
        nonlocal calls
        calls += 1
        if calls == 2:
            assert conn.execute('SELECT COUNT(*) FROM jobs WHERE scored_at IS NOT NULL').fetchone()[0] == 1
            raise KeyboardInterrupt
        return {'score': 8, 'keywords': 'Python', 'reasoning': 'Fixture match'}

    monkeypatch.setattr(scorer, 'score_job', score)
    import pytest
    with pytest.raises(KeyboardInterrupt):
        scorer.run_scoring()
    assert conn.execute('SELECT COUNT(*) FROM jobs WHERE scored_at IS NOT NULL').fetchone()[0] == 1


def test_parallel_apply_counts_start_at_zero_and_increment_after_success(monkeypatch, jobs, caplog):
    jobs()
    jobs('http://second.test/')
    previous = jobs('http://previous.test/')
    launcher.mark_result(previous['url'], 'applied')
    monkeypatch.setattr(launcher, 'launch_chrome', lambda *args, **kwargs: None)
    agent = MagicMock(return_value=('applied', 5))
    monkeypatch.setattr(launcher, 'run_job', agent)
    with caplog.at_level(logging.INFO):
        launcher.main(workers=2, limit=2, agent='claude')
    assert 'applied=0' in caplog.text
    assert 'applied=1' in caplog.text
    assert 'applied=2' in caplog.text
    assert 'previewed=0' in caplog.text
    assert 'Test Engineer' in caplog.text
    assert database.get_stats()['applied'] == 3
    assert agent.call_count == 2


def test_resume_and_cover_counts_follow_saved_files(monkeypatch, jobs, caplog):
    from applypilot.scoring import cover_letter, tailor

    jobs()
    jobs('http://second.test/')
    conn = database.get_connection()
    conn.execute('UPDATE jobs SET tailored_resume_path=NULL')
    conn.commit()
    config.RESUME_PATH.write_text('Synthetic Test Applicant\nTest Engineer\napplicant@example.test\nSUMMARY\nFixture only.')
    monkeypatch.setattr(tailor, 'RESUME_PATH', config.RESUME_PATH)
    monkeypatch.setattr(tailor, 'TAILORED_DIR', config.TAILORED_DIR)
    generated = MagicMock(side_effect=[
        (config.RESUME_PATH.read_text(), {'status': 'approved', 'attempts': 1}),
        RuntimeError('Fixture generation failure'),
    ])
    monkeypatch.setattr(tailor, 'tailor_resume', generated)
    with caplog.at_level(logging.INFO):
        result = tailor.run_tailoring()
    assert result['approved'] == 1 and result['errors'] == 1
    assert 'resumes_saved=0' in caplog.text and 'resumes_saved=1' in caplog.text
    assert 'failed=1' in caplog.text
    assert generated.call_count == 2
    assert conn.execute('SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL').fetchone()[0] == 1

    monkeypatch.setattr(cover_letter, 'RESUME_PATH', config.RESUME_PATH)
    monkeypatch.setattr(cover_letter, 'COVER_LETTER_DIR', config.COVER_LETTER_DIR)
    letter = MagicMock(return_value='Dear Hiring Manager,\nSynthetic fixture only.\nSynthetic Test Applicant')
    monkeypatch.setattr(cover_letter, 'generate_cover_letter', letter)
    with caplog.at_level(logging.INFO):
        result = cover_letter.run_cover_letters()
    assert result['generated'] == 1
    assert 'letters_saved=0' in caplog.text and 'letters_saved=1' in caplog.text
    assert letter.call_count == 1
    assert conn.execute('SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL').fetchone()[0] == 1
