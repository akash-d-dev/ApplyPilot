"""Applicant preferences reach discovery, scoring, and browser instructions."""

import json
from unittest.mock import MagicMock

from applypilot import config, pipeline
from applypilot.apply import prompt


def set_preferences(applicant):
    applicant['personal']['country'] = 'India'
    applicant['job_preferences'] = {
        'remote_only': True, 'work_location_country': 'India',
        'allowed_employment_types': ['full_time', 'part_time', 'contract'],
        'preserve_experience_types': True,
    }
    applicant['compensation'].update(negotiable=True, salary_expectation='')
    applicant['experience'].update(full_time_months=6, internship_months=30)
    applicant['availability'].update(notice_period='1 month', hours_per_week='30-40',
                                     working_hours='Flexible shifts; weekdays preferred')
    config.PROFILE_PATH.write_text(json.dumps(applicant))


def test_remote_india_contract_prompt_respects_preferences(applicant, jobs):
    job = jobs()
    set_preferences(applicant)
    instructions = prompt.build_prompt(job, 'Real resume', dry_run=True)
    assert 'ONLY remote' in instructions
    assert 'India' in instructions
    assert 'part_time' in instructions and 'contract' in instructions
    assert 'You are applying for FULL-TIME salaried positions only' not in instructions
    assert 'Minimum $110K' not in instructions
    assert 'no salary floor' in instructions.lower()
    assert 'answer YES' not in instructions
    assert '6 months full-time' in instructions and '30 months internship' in instructions
    assert '30-40' in instructions and '1 month' in instructions


def test_discovery_only_contacts_selected_sources(monkeypatch):
    from applypilot.discovery import jobspy, smartextract, workday
    monkeypatch.setattr(config, 'load_search_config', lambda: {'discovery': {'sources': ['jobspy']}})
    run_jobspy, run_workday, run_smart = MagicMock(return_value={}), MagicMock(), MagicMock()
    monkeypatch.setattr(jobspy, 'run_discovery', run_jobspy)
    monkeypatch.setattr(workday, 'run_workday_discovery', run_workday)
    monkeypatch.setattr(smartextract, 'run_smart_extract', run_smart)
    pipeline._run_discover()
    run_jobspy.assert_called_once()
    run_workday.assert_not_called()
    run_smart.assert_not_called()
