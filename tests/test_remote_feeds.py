"""Remote APIs feed the real database and respect preferences without model calls."""

import time
from unittest.mock import MagicMock

import httpx
import pandas as pd
import pytest

from applypilot import config, database
from applypilot.apply import launcher
from applypilot.discovery import jobspy, remote_feeds
from applypilot.discovery.filters import title_excluded
from applypilot.scoring import scorer


@pytest.fixture
def search_cfg(monkeypatch):
    cfg = {'country': 'India', 'location': {'country': 'India'},
           'preferences': {'exclude_internships': True},
           'remote_feeds': {'providers': ['himalayas', 'remotive', 'jobicy'],
                            'queries': ['backend'], 'max_pages': 1}}
    monkeypatch.setattr(config, 'load_search_config', lambda: cfg)
    return cfg


def himalaya(**overrides):
    return {'guid': 'https://himalayas.app/companies/test/jobs/backend',
            'applicationLink': 'https://employer.test/apply?ref=original',
            'title': 'Backend Developer', 'companyName': 'Test Employer',
            'employmentType': 'Full Time', 'locationRestrictions': ['India'],
            'timezoneRestrictions': [5.5], 'description': '<p>Remote backend work.</p>',
            'expiryDate': time.time() + 86400, **overrides}


def test_api_discovery_and_cache_preserve_urls_salary_and_history(monkeypatch, search_cfg):
    rows = [himalaya(), himalaya(title='Backend Intern'),
            himalaya(locationRestrictions=['USA']), himalaya(timezoneRestrictions=[-8]),
            himalaya(expiryDate=time.time() - 1), himalaya(title='Sales Agent'),
            himalaya(title='Backend Developer', employmentType='Intern')]
    responses = {
        'himalayas.app': {'jobs': rows},
        'remotive.com': {'jobs': [{'url': 'https://remotive.com/jobs/1', 'title': 'Python Developer',
                                 'candidate_required_location': 'Worldwide', 'description': '<p>API role</p>'}]},
        'jobicy.com': {'jobs': [{'url': 'https://jobicy.com/jobs/2', 'jobTitle': 'Node.js Developer',
                               'jobGeo': 'India', 'jobDescription': '<p>Backend role</p>',
                               'salaryMin': 1000, 'salaryMax': 2000, 'salaryCurrency': 'USD',
                               'salaryPeriod': 'monthly'}]},
    }
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=responses[request.url.host])
    client = httpx.Client
    monkeypatch.setattr(remote_feeds.httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(handle), **kw))
    model = MagicMock(side_effect=AssertionError('Discovery must not call the model'))
    monkeypatch.setattr(scorer, 'get_client', model)
    result = remote_feeds.run_discovery(search_cfg)
    assert result == {'new': 3, 'existing': 0, 'filtered': 6, 'errors': 0}
    conn = database.get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE site='Himalayas'").fetchone()
    assert row['application_url'] == 'https://employer.test/apply?ref=original'
    assert row['salary'] is None
    assert 'Employer: Test Employer' in row['full_description']
    assert row['detail_scraped_at'] and '<p>' not in row['full_description']
    conn.execute("UPDATE jobs SET applied_at='2026-11-01',apply_status='applied' WHERE site='Himalayas'")
    conn.commit()
    assert remote_feeds.run_discovery(search_cfg)['existing'] == 3
    assert len(requests) == 3
    assert conn.execute("SELECT applied_at FROM jobs WHERE site='Himalayas'").fetchone()[0] == '2026-11-01'
    assert conn.execute("SELECT salary FROM jobs WHERE site='Jobicy'").fetchone()[0] == 'USD 1000-2000/monthly'
    model.assert_not_called()


def test_provider_failure_keeps_successful_source(monkeypatch, search_cfg):
    def handle(request):
        return httpx.Response(503) if request.url.host == 'remotive.com' else httpx.Response(200, json={'jobs': []})
    client = httpx.Client
    monkeypatch.setattr(remote_feeds.httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(handle), **kw))
    assert remote_feeds.run_discovery(search_cfg)['errors'] == 1


@pytest.mark.parametrize('title,excluded', [('Backend Intern', True), ('Python Internship', True),
    ('Paid Internships', True), ('Internal Tools Engineer', False), ('International Backend Engineer', False)])
def test_internship_title_boundaries(title, excluded, search_cfg):
    assert title_excluded(title, search_cfg) is excluded


def test_jobspy_storage_and_existing_queue_exclude_internships(search_cfg, jobs, monkeypatch):
    job = jobs()
    conn = database.get_connection()
    conn.execute("UPDATE jobs SET title='Backend Intern'")
    conn.commit()
    assert database.get_stats()['ready_to_apply'] == 0
    assert launcher.acquire_job() is None
    frame = pd.DataFrame([{'job_url': 'https://jobs.test/intern', 'title': 'Python Internship'},
                          {'job_url': 'https://jobs.test/intern-type', 'title': 'Backend Developer', 'job_type': 'internship'},
                          {'job_url': 'https://jobs.test/internal', 'title': 'Internal Tools Engineer'}])
    assert jobspy.store_jobspy_results(conn, frame, 'fixture', search_cfg) == (1, 0)
    client = MagicMock()
    monkeypatch.setattr(scorer, 'get_client', client)
    assert scorer.score_job('resume', {**job, 'title': 'Backend Intern'})['score'] == 1
    client.assert_not_called()


@pytest.mark.parametrize('location', ['USA', 'Europe', 'Worldwide except India'])
def test_non_india_geographies_are_rejected(search_cfg, location):
    assert remote_feeds.normalize({'title': 'Python Developer', 'url': 'https://remotive.com/jobs/1',
        'candidate_required_location': location}, 'remotive', search_cfg) is None
