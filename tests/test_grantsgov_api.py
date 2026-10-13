"""Tests for Grants.gov API integration."""
import pytest
from core.structured_sources import grantsgov_paginated_search, collect_structured_source


def test_grantsgov_search_success(monkeypatch):
    monkeypatch.setattr(
        "core.structured_sources.post_json",
        lambda *a, **k: (
            {"data": {"hitCount": 1, "oppHits": [
                {"title": "Fake Grant", "number": "123", "agencyName": "Test",
                 "openDate": "2026-01-01", "closeDate": "2026-12-31",
                 "oppStatus": "posted", "id": "999"}
            ]}},
            "https://api.grants.gov/v1/api/search2",
        ),
    )

    result = grantsgov_paginated_search("research")
    assert result["pagination_complete"] is True
    assert len(result["hits"]) == 1
    assert result["hits"][0]["title"] == "Fake Grant"
    assert result["total_expected"] == 1


def test_grantsgov_search_zero_results(monkeypatch):
    monkeypatch.setattr(
        "core.structured_sources.post_json",
        lambda *a, **k: ({"data": {"hitCount": 0, "oppHits": []}}, "url"),
    )

    result = grantsgov_paginated_search("weird_keyword_nobody_searches")
    assert len(result["hits"]) == 0
    assert result["pagination_complete"] is True


def test_grantsgov_search_timeout(monkeypatch):
    def fake_post(*a, **k):
        raise TimeoutError("timeout")
    monkeypatch.setattr("core.structured_sources.post_json", fake_post)

    result = grantsgov_paginated_search("research")
    assert len(result["hits"]) == 0
    assert result["pagination_complete"] is False
    assert len(result["errors"]) == 1


def test_collect_structured_source_integration(monkeypatch):
    """collect_structured_source returns None for Grants.gov (handled natively)."""
    result = collect_structured_source("https://api.grants.gov/v1/api/search2")
    assert result is None

def test_grantsgov_structured_metadata_preserves_award_range_and_eligibility():
    from agents.funding_agent import FundingDocument, analyze_document
    from agents.funding_collector import _apply_grantsgov_structured_metadata

    document = FundingDocument(
        organization="Grants.gov",
        title="Security, Privacy, and Trust in Cyberspace (NSF SaTC)",
        source="https://www.grants.gov/search-results-detail/357554",
        source_type="Official API",
        content="Title: Security, Privacy, and Trust in Cyberspace (NSF SaTC). Deadline: 2027-01-25.",
        year=2026,
    )
    analysis = analyze_document(document)
    details = {
        "synopsis": {
            "awardFloor": "50000",
            "awardFloorFormatted": "50,000",
            "awardCeiling": "1200000",
            "awardCeilingFormatted": "1,200,000",
            "estimatedFunding": "60000000",
            "estimatedFundingFormatted": "60,000,000",
            "applicantEligibilityDesc": (
                "<p>Proposals may only be submitted by <b>eligible organizations</b> "
                "located in the U.S.</p>"
            ),
        }
    }

    _apply_grantsgov_structured_metadata(analysis, details, {"id": "357554"})

    assert "50,000" in analysis.funding_amount
    assert "1,200,000" in analysis.funding_amount
    assert "per award" in analysis.funding_amount
    assert "60,000,000" in analysis.evidence["estimated_program_funding"]
    assert "aggregate program metadata" in analysis.evidence["estimated_program_funding"]
    assert "$60,000,000" not in analysis.funding_amount
    assert analysis.eligibility == "Proposals may only be submitted by eligible organizations located in the U.S."
    assert "<p>" not in analysis.evidence["eligibility"]
    assert "designation-specific limits" in analysis.evidence["funding_amount"]


def test_grantsgov_structured_metadata_prefers_detail_values_and_preserves_fallbacks():
    from agents.funding_agent import FundingDocument, analyze_document
    from agents.funding_collector import _apply_grantsgov_structured_metadata

    document = FundingDocument(
        organization="Grants.gov",
        title="Example Research Opportunity",
        source="https://www.grants.gov/search-results-detail/123",
        source_type="Official API",
        content="Research grant of $7M. Applications close 2027-01-25.",
        year=2026,
    )
    analysis = analyze_document(document)
    original_deadline = analysis.deadline
    detail = {
        "synopsis": {
            "awardCeilingFormatted": "250,000",
            "applicantEligibilityDesc": "Universities in the United States.",
        }
    }
    hit = {
        "awardCeilingFormatted": "900,000",
        "applicantEligibilityDesc": "Any applicant worldwide.",
    }

    _apply_grantsgov_structured_metadata(analysis, detail, hit)

    assert analysis.funding_amount.startswith("Up to $250,000 per award")
    assert analysis.eligibility == "Universities in the United States."
    assert analysis.deadline == original_deadline

def test_grantsgov_metadata_replaces_serialized_eligibility_with_applicant_types():
    from agents.funding_agent import FundingDocument, analyze_document
    from agents.funding_collector import _apply_grantsgov_structured_metadata

    document = FundingDocument(
        organization="Grants.gov",
        title="National Defense Education Program (NDEP) STEM Consortia RFI",
        source="https://www.grants.gov/search-results-detail/334905",
        source_type="Official API",
        content="Title: National Defense Education Program STEM Consortia RFI.",
        year=2026,
    )
    analysis = analyze_document(document)
    malformed = (
        "Desc': 'N/A - Request for Information', "
        "'estSynopsisPostingDate': 'Nov 30, 2021', "
        "'applicantTypes': [{'description': 'Others (see text field)'}]"
    )
    analysis.eligibility = malformed
    analysis.evidence["eligibility"] = malformed

    details = {
        "applicantTypes": [
            {"id": "25", "description": "Others (see text field entitled Additional Information on Eligibility)"},
            {"id": "25", "description": "Others (see text field entitled Additional Information on Eligibility)"},
        ]
    }
    _apply_grantsgov_structured_metadata(analysis, details, {"id": "334905"})

    assert analysis.eligibility == (
        "Eligible applicant types (Grants.gov): Others (see text field entitled Additional Information on Eligibility)"
    )
    assert "Desc':" not in analysis.eligibility
    assert "eligibility" not in analysis.evidence or "Desc':" not in analysis.evidence["eligibility"]


def test_grantsgov_metadata_discards_serialized_eligibility_when_no_fallback_exists():
    from agents.funding_agent import FundingDocument, analyze_document
    from agents.funding_collector import _apply_grantsgov_structured_metadata

    document = FundingDocument(
        organization="Grants.gov",
        title="Example Research Notice",
        source="https://www.grants.gov/search-results-detail/456",
        source_type="Official API",
        content="Research notice for institutions.",
        year=2026,
    )
    analysis = analyze_document(document)
    malformed = "Desc': 'N/A', 'estSynopsisPostingDate': 'Nov 30, 2021'"
    analysis.eligibility = malformed
    analysis.evidence["eligibility"] = malformed

    _apply_grantsgov_structured_metadata(analysis, {}, {"id": "456"})

    assert analysis.eligibility is None
    assert "eligibility" not in analysis.evidence



@pytest.mark.parametrize(
    ("source_status", "expected_status"),
    [
        ("posted", "OPEN_CALL"),
        ("forecasted", "UPCOMING_CALL"),
        ("closed", "CLOSED_CALL"),
        ("archived", "CLOSED_CALL"),
    ],
)
def test_grantsgov_api_status_is_preserved_and_mapped(source_status, expected_status):
    from agents.funding_agent import FundingDocument, analyze_document
    from agents.funding_collector import _apply_grantsgov_structured_metadata

    document = FundingDocument(
        organization="Grants.gov",
        title="Research Innovation Challenge",
        source="https://www.grants.gov/search-results-detail/123456",
        source_type="Official API",
        content="Research grant. Deadline: 2027-01-30.",
        year=2026,
    )
    analysis = analyze_document(document)

    _apply_grantsgov_structured_metadata(
        analysis, details={}, hit={"id": "123456", "oppStatus": source_status}
    )

    assert analysis.status == expected_status
    assert analysis.evidence["source_status"] == source_status
    assert analysis.evidence["source_status_provenance"] == "Grants.gov API oppStatus field"
