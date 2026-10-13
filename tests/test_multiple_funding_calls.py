from datetime import UTC, datetime, timedelta
import pytest
from run_pipeline import get_all_open_funding_calls

def test_multiple_open_calls():
    now = datetime.now(UTC)
    records = {
        "outputs": [
            {
                "title": "Call A",
                "program_name": "Call A",
                "status": "OPEN_CALL",
                "deadline": (now + timedelta(days=14)).date().isoformat(),
                "application_url": "https://a.org",
                "call_id": "CALL-A"
            },
            {
                "title": "Call B",
                "program_name": "Call B",
                "status": "OPEN_CALL",
                "deadline": (now + timedelta(days=20)).date().isoformat(),
                "application_url": "https://b.org",
                "call_id": "CALL-B"
            },
            {
                "title": "Call C",
                "program_name": "Call C",
                "status": "OPEN_CALL",
                "deadline": (now + timedelta(days=30)).date().isoformat(),
                "application_url": "https://c.org",
                "call_id": "CALL-C"
            },
        ]
    }
    
    selected, rejected = get_all_open_funding_calls(records)
    assert len(selected) == 3
    assert set([s["program_name"] for s in selected]) == {"Call A", "Call B", "Call C"}

def test_mixed_candidates():
    now = datetime.now(UTC)
    records = {
        "outputs": [
            {
                "title": "Call A",
                "program_name": "Call A",
                "status": "OPEN_CALL",
                "deadline": (now + timedelta(days=14)).date().isoformat(),
                "application_url": "https://a.org",
                "call_id": "CALL-A"
            },
            {
                "title": "Call B",
                "program_name": "Call B",
                "status": "CLOSED_CALL",
                "deadline": (now - timedelta(days=1)).date().isoformat(),
                "call_id": "CALL-B"
            },
            {
                "title": "Call C",
                "program_name": "Call C",
                "status": "OPEN_CALL",
                "deadline": (now + timedelta(days=30)).date().isoformat(),
                "application_url": "https://c.org",
                "call_id": "CALL-C"
            },
            {
                "title": "Call D",
                "program_name": "Call D",
                "status": "UPCOMING_CALL",
                "opening_date": (now + timedelta(days=5)).date().isoformat(),
                "deadline": (now + timedelta(days=60)).date().isoformat(),
                "call_id": "CALL-D"
            }
        ]
    }
    
    selected, rejected = get_all_open_funding_calls(records)
    assert len(selected) == 2
    assert set([s["program_name"] for s in selected]) == {"Call A", "Call C"}

def test_zero_valid_calls():
    records = {
        "outputs": []
    }
    selected, rejected = get_all_open_funding_calls(records)
    assert len(selected) == 0
    assert len(rejected) == 0

def test_one_valid_call():
    now = datetime.now(UTC)
    records = {
        "outputs": [
            {
                "title": "Call A",
                "program_name": "Call A",
                "status": "OPEN_CALL",
                "deadline": (now + timedelta(days=14)).date().isoformat(),
                "application_url": "https://a.org",
                "call_id": "CALL-A"
            }
        ]
    }
    
    selected, rejected = get_all_open_funding_calls(records)
    assert len(selected) == 1
    assert selected[0]["program_name"] == "Call A"

def test_duplicate_call():
    now = datetime.now(UTC)
    records = {
        "outputs": [
            {
                "title": "Call A",
                "program_name": "Call A",
                "status": "OPEN_CALL",
                "deadline": (now + timedelta(days=14)).date().isoformat(),
                "application_url": "https://a.org",
                "call_id": "CALL-A" # Explicit ID duplicate
            },
            {
                "title": "Call A duplicate",
                "program_name": "Call A",
                "status": "OPEN_CALL",
                "deadline": (now + timedelta(days=14)).date().isoformat(),
                "application_url": "https://a.org",
                "call_id": "CALL-A" # Same explicit ID
            },
            {
                "title": "Call B",
                "program_name": "Call B",
                "status": "OPEN_CALL",
                "deadline": (now + timedelta(days=14)).date().isoformat(),
                "application_url": "https://b.org",
                "source_url": "https://news1.org",
                # No call_id, should hash combination
            },
            {
                "title": "Call B",
                "program_name": "Call B",
                "status": "OPEN_CALL",
                "deadline": (now + timedelta(days=14)).date().isoformat(),
                "application_url": "https://b.org",
                "source_url": "https://news2.org",
                # Same title/app_url, diff source_url, should deduplicate
            }
        ]
    }
    
    selected, rejected = get_all_open_funding_calls(records)
    # We should have Call A and Call B
    assert len(selected) == 2
    assert set([s["call_id"] for s in selected]) == {"CALL-A", selected[1]["call_id"]}
    assert "duplicate of existing valid call" in str(rejected)

def test_rejects_request_for_information_even_when_mislabeled_open_call():
    now = datetime.now(UTC)
    title = (
        "National Defense Education Program STEM Consortia "
        "Request for Information (RFI)"
    )
    records = {
        "outputs": [
            {
                "title": title,
                "program_name": title,
                "status": "OPEN_CALL",
                "deadline": (now + timedelta(days=30)).date().isoformat(),
                "funding_amount": "$50,000",
                "eligibility": "Qualified respondents",
                "application_url": "https://example.org/rfi",
                "source_url": "https://www.grants.gov/search-results-detail/123456",
                "evidence": {"source_title": title},
                "call_id": "CALL-RFI-NOT-APPLICATION",
            }
        ]
    }

    selected, rejected = get_all_open_funding_calls(records)

    assert selected == []
    assert len(rejected) == 1
    assert "informational notice is not an actionable funding application" in rejected[0]["reasons"]



def test_does_not_treat_inferred_open_status_as_independent_evidence():
    now = datetime.now(UTC)
    records = {
        "outputs": [{
            "title": "Research Innovation Challenge 2027",
            "program_name": "Research Innovation Challenge 2027",
            "status": "OPEN_CALL",
            "deadline": (now + timedelta(days=30)).date().isoformat(),
            "funding_amount": "$25,000",
            "eligibility": "Universities and research organizations",
            "source_url": "https://example.org/calls/research-innovation",
            "evidence": {
                "deadline": "Applications close 2027-01-30",
                "funding_amount": "Awards up to $25,000",
                "eligibility": "Universities and research organizations",
            },
        }]
    }

    selected, rejected = get_all_open_funding_calls(records)

    assert selected == []
    assert len(rejected) == 1
    assert "missing independent evidence that applications are currently open" in rejected[0]["reasons"]


def test_grantsgov_posted_source_status_is_independent_open_evidence():
    now = datetime.now(UTC)
    records = {
        "outputs": [{
            "title": "Research Innovation Challenge 2027",
            "program_name": "Research Innovation Challenge 2027",
            "status": "OPEN_CALL",
            "deadline": (now + timedelta(days=30)).date().isoformat(),
            "funding_amount": "$25,000",
            "eligibility": "Eligible research organizations",
            "source_url": "https://www.grants.gov/search-results-detail/123456",
            "evidence": {
                "source_status": "posted",
                "source_status_provenance": "Grants.gov API oppStatus field",
                "funding_amount": "Structured award metadata",
            },
        }]
    }

    selected, rejected = get_all_open_funding_calls(records)

    assert len(selected) == 1
    assert selected[0]["program_name"] == "Research Innovation Challenge 2027"
    assert rejected == []


def test_forecasted_source_status_cannot_pass_as_open_call():
    now = datetime.now(UTC)
    records = {
        "outputs": [{
            "title": "Research Innovation Challenge 2027",
            "program_name": "Research Innovation Challenge 2027",
            "status": "OPEN_CALL",
            "deadline": (now + timedelta(days=30)).date().isoformat(),
            "funding_amount": "$25,000",
            "eligibility": "Eligible research organizations",
            "application_url": "https://example.org/apply",
            "source_url": "https://example.org/calls/research-innovation",
            "evidence": {
                "source_status": "forecasted",
                "funding_amount": "Funding up to $25,000",
            },
        }]
    }

    selected, rejected = get_all_open_funding_calls(records)

    assert selected == []
    assert len(rejected) == 1
    assert "source status is forecasted, not currently open" in rejected[0]["reasons"]
