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
