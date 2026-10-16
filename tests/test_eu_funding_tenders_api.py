"""Regression coverage for the EU Funding & Tenders Search API adapter."""
import json

from core import structured_sources


def test_eu_search_api_paginates_and_filters_open_forthcoming_calls(monkeypatch):
    calls = []

    def fake_post(url, *, params, fields, headers, timeout, retries):
        calls.append({"url": url, "params": params, "fields": fields, "headers": headers})
        assert json.loads(fields["query"][1]) == {
            "bool": {
                "must": [
                    {"terms": {"type": ["1", "2", "8"]}},
                    {"terms": {"status": ["31094501", "31094502"]}},
                ]
            }
        }
        assert json.loads(fields["languages"][1]) == ["en"]
        page = params["pageNumber"]
        if page == 1:
            return {
                "totalResults": 2,
                "results": [{
                    "reference": "a",
                    "url": "https://ec.europa.eu/a",
                    "metadata": {"title": ["A"]},
                }],
            }, url
        return {
            "totalResults": 2,
            "results": [{
                "reference": "b",
                "url": "https://ec.europa.eu/b",
                "metadata": {"title": ["B"]},
            }],
        }, url

    monkeypatch.setattr(structured_sources, "post_multipart_json", fake_post)
    result = structured_sources.eu_funding_tenders_paginated_search(
        page_size=1,
        source_limits={"max_pages": 5, "max_documents": 10},
    )

    assert len(calls) == 2
    assert [call["params"]["pageNumber"] for call in calls] == [1, 2]
    assert all(call["params"]["apiKey"] == "SEDIA" for call in calls)
    assert all(call["params"]["sortBy"] == "deadlineDate" for call in calls)
    assert all(call["params"]["orderBy"] == "DESC" for call in calls)
    assert result["pages_fetched"] == 2
    assert result["pagination_complete"] is True
    assert result["total_expected"] == 2
    assert [item["reference"] for item in result["results"]] == ["a", "b"]
    assert result["errors"] == []


def test_eu_search_api_marks_page_cap_as_partial(monkeypatch):
    def fake_post(url, *, params, fields, headers, timeout, retries):
        page = params["pageNumber"]
        return {
            "totalResults": 100,
            "results": [{
                "reference": f"call-{page}",
                "url": f"https://ec.europa.eu/call-{page}",
                "metadata": {"title": [f"Call {page}"]},
            }],
        }, url

    monkeypatch.setattr(structured_sources, "post_multipart_json", fake_post)
    result = structured_sources.eu_funding_tenders_paginated_search(
        page_size=1,
        source_limits={"max_pages": 2},
    )

    assert result["pages_fetched"] == 2
    assert result["pagination_complete"] is False
    assert any("SOURCE_LIMIT_REACHED" in error for error in result["errors"])


def test_eu_collector_normalizes_current_call_fields_without_inventing_them(monkeypatch):
    from agents import funding_collector
    from agents.funding_agent import FundingSource
    from core.schemas import CollectionResult

    record = {
        "reference": "HORIZON-CL4-2026-TEST-01",
        "url": "https://ec.europa.eu/funding-tenders/opportunities/portal/screen/opportunities/topic-details/test",
        "metadata": {
            "title": ["Blockchain research infrastructure"],
            "status": ["Open for Submission"],
            "openingDate": ["2026-10-01"],
            "deadlineDate": ["2026-12-01"],
            "callIdentifier": ["HORIZON-CL4-2026-TEST-01"],
            "programme": ["Horizon Europe"],
            "eligibility": ["Eligible research organisations as stated in the topic conditions."],
            "description": ["Research call for secure decentralized infrastructure."],
        },
    }
    monkeypatch.setattr(
        funding_collector,
        "eu_funding_tenders_paginated_search",
        lambda **kwargs: {
            "results": [record],
            "pages_fetched": 1,
            "total_expected": 1,
            "pagination_complete": True,
            "errors": [],
        },
    )
    source = FundingSource(
        name="EU Open and Forthcoming Funding Calls",
        url="https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/calls-for-proposals",
        tier="tier_3",
        collection={"api": {"types": ["1", "2", "8"], "statuses": ["31094501", "31094502"]}},
        research_domains=["ESG"],
    )
    result = funding_collector._collect_eu_funding_tenders(
        source, CollectionResult(source_id=source.name, method="api"), warnings=[]
    )

    assert result.final_status == "VERIFIED"
    assert result.records_valid == 1
    _, opportunity = result.opportunities[0]
    assert opportunity.program_name == "Blockchain research infrastructure"
    assert opportunity.status == "OPEN_CALL"
    assert opportunity.opening_date == "2026-10-01"
    assert opportunity.deadline == "2026-12-01"
    assert opportunity.call_id == "HORIZON-CL4-2026-TEST-01"
    assert "Search API metadata" in opportunity.evidence["source_status_provenance"]



def test_eu_status_codes_match_portal_reference_values():
    from agents.funding_collector import _eu_status_to_internal

    # Portal reference values: forthcoming=31094501, open=31094502, closed=31094503.
    assert _eu_status_to_internal("31094501") == "UPCOMING_CALL"
    assert _eu_status_to_internal("31094502") == "OPEN_CALL"
    assert _eu_status_to_internal("31094503") == "CLOSED_CALL"
    assert _eu_status_to_internal("Open for Submission") == "OPEN_CALL"
    assert _eu_status_to_internal("Forthcoming") == "UPCOMING_CALL"



def test_eu_collector_deduplicates_repeated_detail_urls_with_different_references(monkeypatch):
    from agents import funding_collector
    from agents.funding_agent import FundingSource
    from core.schemas import CollectionResult

    duplicate_url = (
        "https://ec.europa.eu/funding-tenders/opportunities/portal/"
        "screen/opportunities/competitive-calls-cs/46786367"
    )
    common_metadata = {
        "title": ["Example live cascade call"],
        "status": ["31094502"],
        "deadlineDate": ["2026-12-01"],
        "callIdentifier": ["EXAMPLE-2026"],
        "description": ["Research infrastructure funding opportunity."],
    }
    records = [
        {"reference": "first-index-reference", "url": duplicate_url, "metadata": common_metadata},
        {"reference": "second-index-reference", "url": duplicate_url + "#details", "metadata": common_metadata},
    ]
    monkeypatch.setattr(
        funding_collector,
        "eu_funding_tenders_paginated_search",
        lambda **kwargs: {
            "results": records,
            "pages_fetched": 1,
            "total_expected": 2,
            "pagination_complete": True,
            "errors": [],
        },
    )
    source = FundingSource(
        name="EU Open and Forthcoming Funding Calls",
        url="https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/calls-for-proposals",
        tier="tier_3",
        collection={"api": {"types": ["1", "2", "8"], "statuses": ["31094501", "31094502"]}},
        research_domains=["RWA"],
    )

    result = funding_collector._collect_eu_funding_tenders(
        source, CollectionResult(source_id=source.name, method="api"), warnings=[]
    )

    assert result.records_discovered == 2
    assert result.duplicates_removed == 1
    assert result.records_valid == 1
    assert len(result.opportunities) == 1



def test_eu_effective_status_closes_open_or_forthcoming_calls_after_deadline():
    from datetime import date
    from agents.funding_collector import _eu_effective_status

    status, warning = _eu_effective_status(
        "31094502", "2026-10-15", today=date(2026, 10, 16)
    )
    assert status == "CLOSED_CALL"
    assert warning and "deadline 2026-10-15" in warning

    status, warning = _eu_effective_status(
        "31094501", "2026-10-15", today=date(2026, 10, 16)
    )
    assert status == "CLOSED_CALL"
    assert warning


def test_eu_effective_status_preserves_open_when_deadline_not_past_or_missing():
    from datetime import date
    from agents.funding_collector import _eu_effective_status

    assert _eu_effective_status(
        "31094502", "2026-12-01", today=date(2026, 10, 16)
    ) == ("OPEN_CALL", None)
    assert _eu_effective_status("31094502", None, today=date(2026, 10, 16)) == (
        "OPEN_CALL", None
    )


def test_eu_collector_extracts_eligibility_from_topic_conditions_html(monkeypatch):
    from agents import funding_collector
    from agents.funding_agent import FundingSource
    from core.schemas import CollectionResult

    record = {
        "reference": "eligibility-fallback",
        "url": "https://ec.europa.eu/funding-tenders/opportunities/portal/screen/opportunities/topic-details/TEST-2026",
        "metadata": {
            "title": ["Secure Data Infrastructure Pilot"],
            "status": ["31094502"],
            "deadlineDate": ["2026-12-15"],
            "topicConditions": [
                "<section><h3>Eligibility conditions</h3>"
                "<p>Eligible applicants are public research organisations and SMEs "
                "established in an EU Member State.</p></section>"
            ],
            "descriptionByte": ["<p>Research and innovation in secure data infrastructure.</p>"],
        },
    }
    monkeypatch.setattr(
        funding_collector,
        "eu_funding_tenders_paginated_search",
        lambda **kwargs: {
            "results": [record],
            "pages_fetched": 1,
            "total_expected": 1,
            "pagination_complete": True,
            "errors": [],
        },
    )
    source = FundingSource(
        name="EU Open and Forthcoming Funding Calls",
        url="https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/calls-for-proposals",
        tier="tier_3",
        collection={"api": {"types": ["1", "2", "8"], "statuses": ["31094501", "31094502"]}},
        research_domains=["RWA", "DID"],
    )

    result = funding_collector._collect_eu_funding_tenders(
        source, CollectionResult(source_id=source.name, method="api"), warnings=[]
    )

    assert result.records_valid == 1
    _, opportunity = result.opportunities[0]
    assert opportunity.eligibility
    assert "Eligible applicants are public research organisations and SMEs" in opportunity.eligibility
    assert "<p>" not in opportunity.eligibility
    assert opportunity.evidence["eligibility_provenance"].startswith("European Commission")
    assert "topicConditions" in opportunity.evidence["eligibility"]
