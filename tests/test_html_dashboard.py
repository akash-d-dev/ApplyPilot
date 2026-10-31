"""Application outcomes are visible independently of scraped application URLs."""

from pathlib import Path

from playwright.sync_api import sync_playwright

from applypilot import database
from applypilot.view import generate_dashboard


def test_dashboard_shows_outcomes_and_filters_without_changing_jobs(jobs, tmp_path):
    for status in ('applied', 'previewed', 'failed', 'in_progress', 'manual', None):
        jobs(f'https://fixture.test/{status}', status=status)
    conn = database.get_connection()
    conn.execute("UPDATE jobs SET applied_at='2026-10-31T01:00:00+00:00' WHERE apply_status='applied'")
    conn.execute("UPDATE jobs SET apply_error='login_issue <script>bad()</script>' WHERE apply_status='failed'")
    conn.execute("UPDATE jobs SET application_url=NULL WHERE apply_status='applied'")
    conn.commit()
    before = [tuple(row) for row in conn.execute('SELECT * FROM jobs ORDER BY url')]
    path = generate_dashboard(str(tmp_path / 'dashboard.html'))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(Path(path).as_uri())
            applied = page.locator('.job-card[data-status="applied"]')
            assert applied.locator('.application-status').inner_text() == 'Applied'
            assert applied.locator('time').get_attribute('datetime') == '2026-10-31T01:00:00+00:00'
            assert applied.get_by_role('link', name='Open listing').get_attribute('href') == 'https://fixture.test/applied'
            failed = page.locator('.job-card[data-status="failed"]')
            assert 'login_issue <script>bad()</script>' in failed.locator('.apply-error').inner_text()
            assert failed.locator('script').count() == 0
            assert page.locator('.job-card[data-status="previewed"] .application-status').inner_text() == 'Previewed'
            assert page.locator('.job-card[data-status="in_progress"] .application-status').inner_text() == 'In progress'
            assert page.locator('.job-card[data-status="not_attempted"] .application-status').inner_text() == 'Not attempted'
            page.get_by_label('Application status').select_option('failed')
            assert page.locator('.job-card:visible').count() == 1
            assert page.locator('.score-header:visible .score-group-count').inner_text() == '(1 job)'
            page.get_by_role('button', name='9+ Perfect').click()
            assert page.locator('.job-card:visible').count() == 1
            page.get_by_label('Application status').select_option('all')
            assert page.locator('.job-card:visible').count() == 6
            assert not errors
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        finally:
            browser.close()
    assert before == [tuple(row) for row in conn.execute('SELECT * FROM jobs ORDER BY url')]
