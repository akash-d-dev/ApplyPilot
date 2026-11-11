"""RemoteOK, We Work Remotely, Hirist, and Wellfound obey the same discovery filters."""

import json

import httpx
import pandas as pd
import pytest

from applypilot import config, database
from applypilot.discovery import remote_feeds
from applypilot.discovery.filters import discovery_eligible, title_excluded


@pytest.fixture
def search_cfg(monkeypatch):
    cfg = {
        'country': 'India', 'location': {'country': 'India'},
        'preferences': {'exclude_internships': True},
        'remote_feeds': {'providers': ['himalayas'], 'queries': ['backend'], 'max_pages': 1},
    }
    monkeypatch.setattr(config, 'load_search_config', lambda: cfg)
    return cfg


def test_staff_title_gap_and_remote_india_rules(search_cfg):
    search_cfg['exclude_titles'] = ['staff engineer', 'principal']
    search_cfg['preferences']['remote_only'] = True
    assert title_excluded('Staff Software Engineer', search_cfg)
    assert title_excluded('Principal Engineer', search_cfg)
    assert not title_excluded('Internal Tools Engineer', search_cfg)
    assert discovery_eligible('Backend Developer', 'India (Remote)', search_cfg)
    assert discovery_eligible('Backend Developer', 'Remote', search_cfg)
    assert not discovery_eligible('Backend Developer', 'Bengaluru, Karnataka, India', search_cfg)
    assert not discovery_eligible('Backend Developer', 'United States (Remote)', search_cfg)
    assert not discovery_eligible('Backend Developer', 'Worldwide except India', search_cfg)


def test_purge_drops_unapplied_mismatches_and_keeps_history(search_cfg):
    search_cfg['preferences']['remote_only'] = True
    search_cfg['exclude_titles'] = ['staff engineer', 'director']
    conn = database.init_db()
    rows = [
        ('https://jobs.test/onsite', 'Backend Developer', 'Bengaluru, Karnataka, India', None, None),
        ('https://jobs.test/staff', 'Staff Software Engineer', 'India (Remote)', None, None),
        ('https://jobs.test/keep', 'Backend Developer', 'India (Remote)', None, None),
        ('https://jobs.test/applied', 'Director of Engineering', 'New York', 'applied', '2026-01-01'),
    ]
    conn.executemany(
        "INSERT INTO jobs (url, title, location, apply_status, applied_at) VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    assert database.purge_ineligible_discoveries() == 2
    left = {row['url'] for row in conn.execute('SELECT url FROM jobs')}
    assert left == {'https://jobs.test/keep', 'https://jobs.test/applied'}


def test_jobspy_remote_only_keeps_flagged_india_jobs(search_cfg):
    from applypilot.discovery import jobspy

    search_cfg['preferences']['remote_only'] = True
    frame = pd.DataFrame([
        {'job_url': 'https://jobs.test/city', 'title': 'Backend Developer',
         'location': 'Bengaluru, India', 'is_remote': False},
        {'job_url': 'https://jobs.test/remote', 'title': 'Backend Developer',
         'location': 'Bengaluru, India', 'is_remote': True},
    ])
    conn = database.init_db()
    assert jobspy.store_jobspy_results(conn, frame, 'fixture', search_cfg) == (1, 0)
    row = conn.execute("SELECT location FROM jobs").fetchone()
    assert row['location'] == 'Bengaluru, India (Remote)'


def _wellfound_page() -> str:
    job = {
        '__typename': 'JobListingSearchResult', 'id': '10', 'title': 'Backend Developer',
        'slug': 'backend-developer', 'remote': True, 'remoteConfig': {'kind': 'REMOTE'},
        'acceptedRemoteLocationNames': ['India'], 'description': 'Python services', 'jobType': 'full-time',
    }
    rejected = {**job, 'id': '11', 'slug': 'us-backend', 'acceptedRemoteLocationNames': ['United States']}
    apollo = {
        'JobListingSearchResult:10': job,
        'JobListingSearchResult:11': rejected,
        'StartupResult:1': {
            '__typename': 'StartupResult', 'id': '1', 'name': 'Acme',
            'highlightedJobListings': [{'__ref': 'JobListingSearchResult:10'}, {'__ref': 'JobListingSearchResult:11'}],
        },
    }
    payload = {'props': {'pageProps': {'apolloState': {'data': apollo}}}}
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + '</script>'


def test_new_boards_store_only_eligible_jobs(search_cfg, monkeypatch):
    search_cfg['preferences']['remote_only'] = True
    search_cfg['exclude_titles'] = ['staff engineer']
    search_cfg['remote_feeds'].update({
        'providers': ['remoteok', 'weworkremotely', 'hirist', 'wellfound'],
        'max_pages': 1,
        'weworkremotely_feeds': ['https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss'],
        'hirist_categories': [1],
        'wellfound_roles': ['backend-engineer'],
    })
    remoteok = [
        {'position': 'Backend Developer', 'company': 'Acme', 'location': '',
         'url': 'https://remoteok.com/remote-jobs/1', 'apply_url': 'https://acme.test/apply',
         'description': '<p>Python API</p>'},
        {'position': 'Backend Intern', 'company': 'Acme', 'location': '',
         'url': 'https://remoteok.com/remote-jobs/2', 'description': '<p>Internship</p>'},
        {'position': 'Backend Developer', 'company': 'Acme', 'location': 'United States',
         'url': 'https://remoteok.com/remote-jobs/3', 'description': '<p>US only</p>'},
    ]
    wwr = '''<?xml version="1.0"?><rss><channel>
      <item><title>Acme: Backend Developer</title><region>Anywhere in the World</region><country></country>
        <type>Full-Time</type><link>https://weworkremotely.com/remote-jobs/acme-backend</link>
        <description>&lt;p&gt;Python services&lt;/p&gt;</description></item>
      <item><title>Elsewhere: Backend Developer</title><region>Anywhere in the World</region><country>Mexico</country>
        <type>Full-Time</type><link>https://weworkremotely.com/remote-jobs/mexico</link>
        <description>&lt;p&gt;Python&lt;/p&gt;</description></item>
    </channel></rss>'''
    hirist = {'data': [
        {'id': 1, 'jobdesignation': 'Backend Developer', 'workFromHome': 1,
         'location': [{'name': 'Bengaluru'}], 'companyData': {'companyName': 'Acme'},
         'tags': [{'name': 'Python'}], 'min': 3, 'max': 5},
        {'id': 2, 'jobdesignation': 'Backend Developer', 'workFromHome': 0,
         'location': [{'name': 'Bengaluru'}], 'companyData': {'companyName': 'Onsite'}},
        {'id': 3, 'jobdesignation': 'Staff Software Engineer', 'workFromHome': 1,
         'location': [{'name': 'Hyderabad'}], 'companyData': {'companyName': 'Staff Co'}},
    ]}
    pages = {
        'remoteok.com': httpx.Response(200, json=remoteok),
        'weworkremotely.com': httpx.Response(200, text=wwr),
        'gladiator.hirist.tech': httpx.Response(200, json=hirist),
        'wellfound.com': httpx.Response(200, text=_wellfound_page()),
    }

    def handle(request):
        return pages[request.url.host]

    client = httpx.Client
    monkeypatch.setattr(remote_feeds.httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(handle), **kw))
    result = remote_feeds.run_discovery(search_cfg)
    assert result == {'new': 4, 'existing': 0, 'filtered': 6, 'errors': 0}
    conn = database.get_connection()
    sites = {row['site'] for row in conn.execute('SELECT site FROM jobs')}
    assert sites == {'RemoteOK', 'WeWorkRemotely', 'Hirist', 'Wellfound'}
    hirist_row = conn.execute("SELECT * FROM jobs WHERE site = 'Hirist'").fetchone()
    assert hirist_row['url'] == 'https://www.hirist.tech/j/1'
    assert hirist_row['detail_scraped_at'] is None
    assert 'Acme' in hirist_row['full_description']
    remoteok_row = conn.execute("SELECT application_url, location FROM jobs WHERE site = 'RemoteOK'").fetchone()
    assert remoteok_row['application_url'] == 'https://acme.test/apply'
    assert remoteok_row['location'] == 'Worldwide (Remote)'
    wellfound_row = conn.execute("SELECT url, full_description FROM jobs WHERE site = 'Wellfound'").fetchone()
    assert wellfound_row['url'] == 'https://wellfound.com/jobs/10-backend-developer'
    assert 'Employer: Acme' in wellfound_row['full_description']
