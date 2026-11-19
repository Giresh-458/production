"""Regression tests for route-specific publisher status/currentness evidence."""
from types import SimpleNamespace

import agents.funding_collector as collector
from agents.funding_agent import determine_status


PROPOSAL_URL = "https://www.idapthub.org/applications/call_for_proposals"
FELLOWSHIP_URL = "https://www.idapthub.org/applications/chanakya_fellowships"


def _source():
    return SimpleNamespace(
        name="I-DAPT Hub Cyber-Physical Systems Calls",
        collection={
            "status_verification_urls": [PROPOSAL_URL],
            "status_verification_url_map": {PROPOSAL_URL: [PROPOSAL_URL]},
            "discovery": {"currentness_verification_urls": [FELLOWSHIP_URL]},
        },
    )


def test_proposal_closure_notice_does_not_leak_to_chanakya_fellowships(monkeypatch):
    calls = []

    def fetch_text(url, **kwargs):
        calls.append(url)
        return (
            "I-DAPT-Hub Foundation is currently not accepting any fresh applications "
            "for Research Projects and Post-Doctoral Chanakya Fellowships.",
            url,
        )

    monkeypatch.setattr("core.http_client.fetch_text", fetch_text)
    context = collector._fetch_status_verification_context(
        _source(), SimpleNamespace(source=FELLOWSHIP_URL), warnings=[]
    )
    assert context == ""
    assert calls == []


def test_proposal_closure_notice_is_attached_to_its_own_route(monkeypatch):
    def fetch_text(url, **kwargs):
        return (
            "I-DAPT-Hub Foundation is currently not accepting any fresh applications "
            "for Research Projects and Post-Doctoral Chanakya Fellowships.",
            url,
        )

    monkeypatch.setattr("core.http_client.fetch_text", fetch_text)
    context = collector._fetch_status_verification_context(
        _source(), SimpleNamespace(source=PROPOSAL_URL), warnings=[]
    )
    assert "not accepting any fresh applications" in context


def test_chanakya_official_anytime_submission_phrase_verifies_currentness(monkeypatch):
    body = "Applications are evaluated Regular Interval. Submit your proposal anytime!"
    monkeypatch.setattr(
        "core.http_client.fetch_text",
        lambda url, **kwargs: (body, url),
    )
    context = collector._fetch_currentness_verification_context(
        _source(), SimpleNamespace(source=FELLOWSHIP_URL), warnings=[]
    )
    assert "CURRENT APPLICATION EVIDENCE CONFIRMED" in context
    assert "Submit your proposal anytime" in context


def test_chanakya_anytime_submission_is_recognized_as_open_call():
    content = (
        "I-DAPT Chanakya Fellowship Program. "
        "Applications are evaluated Regular Interval. Submit your proposal anytime!"
    )
    assert determine_status(
        None, None, None, content, "I-DAPT Chanakya Fellowship Program"
    ) == "OPEN_CALL"
