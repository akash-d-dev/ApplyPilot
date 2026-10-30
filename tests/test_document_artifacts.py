"""Exercise preparation with colliding job titles and missing direct URLs."""

from pathlib import Path
from unittest.mock import MagicMock

from playwright.sync_api import sync_playwright
from typer.testing import CliRunner

from applypilot import config, database
from applypilot.apply import launcher
from applypilot.cli import app
from applypilot.scoring import cover_letter, tailor
from applypilot.scoring.pdf import convert_to_pdf


def test_same_title_jobs_keep_separate_documents(monkeypatch, jobs):
    jobs('https://jobs.test/first')
    jobs('https://jobs.test/second')
    conn = database.get_connection()
    conn.execute('UPDATE jobs SET tailored_resume_path=NULL')
    conn.commit()
    config.RESUME_PATH.write_text('Synthetic Test Applicant\nBackend Engineer\nSUMMARY\nFixture only.')
    for module, directory in ((tailor, 'TAILORED_DIR'), (cover_letter, 'COVER_LETTER_DIR')):
        monkeypatch.setattr(module, 'RESUME_PATH', config.RESUME_PATH)
        monkeypatch.setattr(module, directory, getattr(config, directory))
    monkeypatch.setattr(
        tailor, 'tailor_resume',
        lambda resume, job, profile, **kw: (
            resume + '\n' + job['url'], {'status': 'approved', 'attempts': 1},
        ),
    )
    monkeypatch.setattr(
        cover_letter, 'generate_cover_letter',
        lambda resume, job, profile, **kw: 'Dear Hiring Manager,\nFixture for ' + job['url'],
    )
    assert tailor.run_tailoring()['approved'] == 2
    assert cover_letter.run_cover_letters()['generated'] == 2
    rows = conn.execute('SELECT * FROM jobs').fetchall()
    for column in ('tailored_resume_path', 'cover_letter_path'):
        assert len({row[column] for row in rows}) == 2
        for row in rows:
            path = Path(row[column])
            assert row['url'] in path.read_text()
            assert path.with_suffix('.pdf').is_file()
    for row in rows:
        path = Path(row['tailored_resume_path'])
        assert row['url'] in path.with_name(path.stem + '_JOB.txt').read_text()
        assert path.with_name(path.stem + '_REPORT.json').is_file()


def test_status_and_queue_allow_listing_url_fallback(monkeypatch, jobs):
    job = jobs()
    conn = database.get_connection()
    conn.execute('UPDATE jobs SET application_url=NULL')
    conn.commit()
    assert database.get_stats()['ready_to_apply'] == 1
    assert database.get_jobs_by_stage(stage='pending_apply')[0]['url'] == job['url']
    output = CliRunner().invoke(app, ['status'])
    assert output.exit_code == 0, output.output
    assert any('Ready to apply' in line and '1' in line for line in output.output.splitlines())
    runner = MagicMock()
    monkeypatch.setenv('CURSOR_API_KEY', 'synthetic-placeholder')
    monkeypatch.setattr(launcher, 'main', runner)
    output = CliRunner().invoke(app, ['apply', '--agent', 'cursor', '--limit', '1'])
    assert output.exit_code == 0, output.output
    assert runner.call_args.kwargs['dry_run'] is True
    assert launcher.acquire_job(dry_run=True)['url'] == job['url']


def test_cover_pdf_layout_preserves_every_paragraph_and_signature(tmp_path):
    paragraphs = [
        'Dear Hiring Manager,',
        'I build Python APIs and <reliable> services & integrations.',
        'My experience matches this remote backend role.',
        'I would welcome a conversation about the team.',
        'Regards,\nSynthetic Test Applicant',
    ]
    path = tmp_path / 'Fixture_Backend_123_CL.txt'
    path.write_text('\n\n'.join(paragraphs))
    html = convert_to_pdf(path, html_only=True).read_text()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html)
            assert page.locator('p').all_text_contents() == paragraphs
            assert page.locator('body').evaluate('(el) => getComputedStyle(el).textAlign') == 'left'
        finally:
            browser.close()
    assert convert_to_pdf(path).read_bytes().startswith(b'%PDF-')
