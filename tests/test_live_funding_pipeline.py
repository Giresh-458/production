from datetime import UTC, datetime, timedelta

from agents.funding_agent import determine_status, extract_call_dates
from run_pipeline import get_all_open_funding_calls


def test_live_date_extraction_marks_current_call_open():
    text = "Road to Devcon 8 India University Program. Opens: Apr 20, 2026. Closes: Sep 30, 2029."
    opening, deadline = extract_call_dates(text)
    assert opening == "2026-04-20"
    assert deadline == "2029-09-30"
    assert determine_status(None, opening, deadline, text) == "OPEN_CALL"


def test_select_open_funding_call_rejects_closed_and_future_calls():
    now = datetime.now(UTC)
    open_deadline = (now + timedelta(days=14)).date().isoformat()
    future_open = (now + timedelta(days=5)).date().isoformat()
    records = {
        "outputs": [
            {
                "title": "Open Call",
                "program_name": "Open Call",
                "status": "OPEN_CALL",
                "deadline": open_deadline,
                "domain_relevance": [{"domain": "RWA", "score": 0.8}],
                "application_url": "https://example.org/apply",
            },
            {
                "title": "Closed Call",
                "program_name": "Closed Call",
                "status": "CLOSED_CALL",
                "deadline": (now - timedelta(days=1)).date().isoformat(),
                "domain_relevance": [{"domain": "RWA", "score": 0.9}],
            },
            {
                "title": "Future Call",
                "program_name": "Future Call",
                "status": "UPCOMING_CALL",
                "opening_date": future_open,
                "deadline": (now + timedelta(days=30)).date().isoformat(),
                "domain_relevance": [{"domain": "RWA", "score": 1.0}],
            },
        ]
    }
    selected, rejected = get_all_open_funding_calls(records)
    assert len(selected) == 1
    assert selected[0]["program_name"] == "Open Call"
    assert any(item["title"] == "Closed Call" for item in rejected)
    assert any(item["title"] == "Future Call" for item in rejected)


def test_date_parser_handles_real_world_ordinal_and_day_month_formats():
    from agents.funding_agent import parse_date_string
    assert parse_date_string("30 September 2026") .date().isoformat() == "2026-09-30"
    assert parse_date_string("September 30th, 2026").date().isoformat() == "2026-09-30"
    assert parse_date_string("2026-09-30T23:59:00+05:30").date().isoformat() == "2026-09-30"
    assert parse_date_string("30/09/2026").date().isoformat() == "2026-09-30"


def test_extract_call_dates_handles_prose_deadline_and_opening():
    from agents.funding_agent import extract_call_dates
    opening, deadline = extract_call_dates(
        "Applications open on 20 April 2026 and close at 23:59 IST on 30 September 2026."
    )
    assert opening == "2026-04-20"
    assert deadline == "2026-09-30"


def test_status_does_not_call_generic_program_page_open():
    from agents.funding_agent import determine_status
    assert determine_status(None, None, None, "Our program supports researchers. Apply for membership at any time.") == "UNKNOWN"


def test_status_requires_stronger_call_evidence_for_date_less_pages():
    from agents.funding_agent import determine_status
    assert determine_status(None, None, None, "Call for proposals. Applications are now open. Apply now.") == "OPEN_CALL"


def test_open_call_with_future_deadline_and_application_url_is_current_call():
    from agents.funding_agent import FundingDocument, analyze_document
    future = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
    doc = FundingDocument(
        organization="Test Foundation",
        title="Research Innovation Challenge 2026",
        source="https://example.org/calls/research-innovation",
        source_type="html",
        content=(
            f"Research Innovation Challenge 2026. Deadline: {future}. "
            "Eligible applicants: universities. Apply here: https://example.org/calls/research-innovation/apply."
        ),
        year=2026,
    )
    result = analyze_document(doc)
    assert result.status == "OPEN_CALL"
    assert result.deadline == future
    assert result.application_url


def test_literature_unscoped_mode_uses_funding_mission_terms():
    from agents.literature_agent import get_keywords, build_query
    from core.funding_context import current_funding_queries
    token = current_funding_queries.set(["privacy preserving clinical data interoperability"])
    try:
        terms = get_keywords(None)
        query = build_query(None)
    finally:
        current_funding_queries.reset(token)
    assert "privacy" in terms
    assert "clinical" in terms
    assert "privacy" in query.lower()
