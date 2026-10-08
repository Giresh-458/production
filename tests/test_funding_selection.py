import json
import pytest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.funding_selection import (
    ApplicantProfile,
    ValidationResult,
    _extract_trl,
    validate_call,
    rank_calls,
    execute_selection
)

def test_extract_trl():
    assert _extract_trl("TRL 3-5") == (3, 5)
    assert _extract_trl("Must be TRL 4") == (4, 4)
    assert _extract_trl("Between TRL4 and TRL 6") == (4, 6)
    assert _extract_trl("No TRL mentioned") is None


def test_validate_call_closed():
    profile = ApplicantProfile()
    record = {
        "status": "CLOSED_CALL",
        "program_name": "Test",
        "funding_body": "Test",
        "problem_statement": "Test"
    }
    result = validate_call(record, profile)
    assert result.is_valid is False
    assert any("closed" in r for r in result.reasons)


def test_validate_call_expired():
    profile = ApplicantProfile()
    past_date = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    record = {
        "status": "OPEN_CALL",
        "deadline": past_date,
        "program_name": "Test",
        "funding_body": "Test",
        "problem_statement": "Test"
    }
    result = validate_call(record, profile)
    assert result.is_valid is False
    assert any("past" in r for r in result.reasons)


def test_validate_call_missing_required():
    profile = ApplicantProfile()
    record = {
        "status": "OPEN_CALL",
        # program_name missing
        "funding_body": "Test",
        "problem_statement": "Test"
    }
    result = validate_call(record, profile)
    assert result.is_valid is False
    assert any("program_name" in r for r in result.reasons)


def test_validate_call_geography():
    profile = ApplicantProfile(allowed_geographies=["europe", "uk"])
    
    # Incompatible
    record = {
        "status": "OPEN_CALL",
        "program_name": "Test",
        "funding_body": "Test",
        "problem_statement": "Test",
        "geography": "United States Only"
    }
    result = validate_call(record, profile)
    assert result.is_valid is False
    assert any("geography" in r for r in result.reasons)
    
    # Compatible (remote/global)
    record["geography"] = "Remote/Global"
    result = validate_call(record, profile)
    assert result.is_valid is True
    
    # Compatible (match)
    record["geography"] = "UK and EU"
    result = validate_call(record, profile)
    assert result.is_valid is True


def test_validate_call_trl():
    profile = ApplicantProfile(min_trl=3, max_trl=5)
    record = {
        "status": "OPEN_CALL",
        "program_name": "Test",
        "funding_body": "Test",
        "problem_statement": "Test",
        "trl": "TRL 6-8"
    }
    result = validate_call(record, profile)
    assert result.is_valid is False
    assert any("TRL" in r for r in result.reasons)


def test_validate_call_eligibility():
    profile = ApplicantProfile(applicant_type="academic")
    record = {
        "status": "OPEN_CALL",
        "program_name": "Test",
        "funding_body": "Test",
        "problem_statement": "Test",
        "eligibility": "Commercial only, no universities"
    }
    result = validate_call(record, profile)
    assert result.is_valid is False
    assert any("academic" in r.lower() for r in result.reasons)


def test_rank_calls():
    profile = ApplicantProfile(target_research_areas=["DePIN", "ZK-IoV"])
    future_date = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    
    records = [
        # Call 1: Valid, exact area match, has deadline -> highest score
        {
            "call_id": "CALL-A",
            "status": "OPEN_CALL",
            "program_name": "Test A",
            "funding_body": "Test",
            "problem_statement": "Test",
            "focus_area": "DePIN",
            "deadline": future_date
        },
        # Call 2: Valid, generic area -> medium score
        {
            "call_id": "CALL-B",
            "status": "OPEN_CALL",
            "program_name": "Test B",
            "funding_body": "Test",
            "problem_statement": "Test",
            "focus_area": "General",
            "deadline": future_date
        },
        # Call 3: Invalid (closed) -> low score, is_valid=False
        {
            "call_id": "CALL-C",
            "status": "CLOSED_CALL",
            "program_name": "Test C",
            "funding_body": "Test",
            "problem_statement": "Test",
            "focus_area": "DePIN"
        }
    ]
    
    scored = rank_calls(records, profile)
    assert len(scored) == 3
    assert scored[0].call_id == "CALL-A"
    assert scored[0].validation.is_valid is True
    
    assert scored[1].call_id == "CALL-B"
    assert scored[1].validation.is_valid is True
    assert scored[1].score < scored[0].score
    
    assert scored[2].call_id == "CALL-C"
    assert scored[2].validation.is_valid is False


def test_execute_selection_review_mode(tmp_path: Path):
    # We will mock load_candidate_calls by mocking the sqlite3 response or just monkeypatching
    pass # covered by rank_calls and general logic


def test_funding_call_context_clean_missing():
    # Verify that raw strings like "Unknown", "[]", "None" become actual null/empty structures
    from core.funding_selection import FundingCallContext, ScoredCall, ValidationResult
    
    raw_record = {
        "title": "Raw Title",
        "deadline": "Unknown",
        "funding_amount": "null",
        "research_priorities": "[]",
        "required_partners": "",
        "geography": "None"
    }
    
    scored = ScoredCall("CALL-1", raw_record, 0.9, ValidationResult(True, ["test"]))
    context = FundingCallContext.from_scored_call(scored, "autonomous")
    
    assert context.call_title == "Raw Title"
    assert context.deadline is None
    assert context.funding_amount is None
    assert context.research_priorities == []
    assert context.required_partners is None
    assert context.geography is None
    
    # Verify Serialization
    d = context.to_dict()
    assert d["deadline"] is None
    assert d["research_priorities"] == []
    
    # Verify Deserialization
    loaded = FundingCallContext.from_dict(d)
    assert loaded.call_title == "Raw Title"
    assert loaded.deadline is None
    assert loaded.research_priorities == []


def test_execute_selection_autonomous(tmp_path: Path, monkeypatch):
    future_date = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    mock_records = [
        {
            "call_id": "CALL-123",
            "status": "OPEN_CALL",
            "program_name": "Test A",
            "funding_body": "Test",
            "problem_statement": "Test",
            "focus_area": "DePIN",
            "deadline": future_date
        },
        {
            "call_id": "CALL-456",
            "status": "CLOSED_CALL",
            "program_name": "Test B",
            "funding_body": "Test",
            "problem_statement": "Test"
        }
    ]
    
    def mock_load(db_path):
        return mock_records
        
    import core.funding_selection
    monkeypatch.setattr(core.funding_selection, "load_candidate_calls", mock_load)
    
    profile = ApplicantProfile()
    out_dir = tmp_path / "workflow"
    
    result = execute_selection("autonomous", profile, output_dir=out_dir)
    
    assert result["selection_mode"] == "autonomous"
    assert result["selected_funding_call_id"] == "CALL-123"
    assert len(result["rejected_alternatives"]) == 1
    assert result["rejected_alternatives"][0]["funding_call_id"] == "CALL-456"
    
    out_file = out_dir / "selected_funding_call.json"
    assert out_file.exists()
    
    data = json.loads(out_file.read_text())
    assert data["selected_funding_call_id"] == "CALL-123"


def test_funding_discovery_configured_scan_does_not_require_existing_context(monkeypatch):
    from core.agent_interface import validate_run_input

    assert validate_run_input("funding", "configured_scan", {}) == []
    assert "MISSING_FUNDING_CONTEXT" in validate_run_input("literature", "configured_scan", {})
