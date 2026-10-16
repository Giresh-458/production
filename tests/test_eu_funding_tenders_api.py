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
    assert all(call["params"]["sortBy"] == "deadline" for call in calls)
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
            "eligibility": ["Eligible applicants are research organisations and SMEs established in an EU Member State."],
            "description": ["ESG carbon-credit MRV, greenhouse-gas emissions measurement, and sustainability research using interoperable data infrastructure."],
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
        "title": ["Carbon-credit MRV research call"],
        "status": ["31094502"],
        "deadlineDate": ["2026-12-01"],
        "callIdentifier": ["EXAMPLE-2026"],
        "eligibility": ["Eligible applicants are SMEs and public research organisations established in an EU Member State."],
        "description": ["ESG carbon credits, MRV, emissions measurement, and sustainability research."],
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
        research_domains=["ESG"],
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
            "descriptionByte": ["<p>Research on ESG carbon-credit MRV, emissions measurement, and sustainability data.</p>"],
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

    assert result.records_valid == 1
    _, opportunity = result.opportunities[0]
    assert opportunity.eligibility
    assert "Eligible applicants are public research organisations and SMEs" in opportunity.eligibility
    assert "<p>" not in opportunity.eligibility
    assert opportunity.evidence["eligibility_provenance"].startswith("European Commission")
    assert "topicConditions" in opportunity.evidence["eligibility"]



def test_eu_eligibility_parser_extracts_specific_criteria_from_beneficiary_html():
    from agents.funding_collector import _extract_eu_eligibility_from_html

    html = (
        "<h3>How to submit</h3><p>Submit through the online application portal.</p>"
        "<h3>Eligibility conditions</h3>"
        "<p>Eligible applicants are SMEs and public research organisations "
        "established in an EU Member State.</p>"
    )
    extracted = _extract_eu_eligibility_from_html(html)
    assert extracted is not None
    assert "Eligible applicants are SMEs and public research organisations" in extracted
    assert "<p>" not in extracted


def test_eu_eligibility_parser_does_not_promote_generic_process_language():
    from agents.funding_collector import _extract_eu_eligibility_from_html

    generic = (
        "<p>Administrators continuously check the eligibility of applications "
        "according to the eligibility criteria specified in the call.</p>"
    )
    assert _extract_eu_eligibility_from_html(generic) is None


def test_eu_collector_filters_expired_records_instead_of_emitting_closed_opportunities(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from agents import funding_collector
    from agents.funding_agent import FundingSource
    from core.schemas import CollectionResult

    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    record = {
        "reference": "expired-call",
        "url": "https://ec.europa.eu/funding-tenders/opportunities/portal/screen/opportunities/competitive-calls-cs/expired",
        "metadata": {
            "title": ["Expired carbon-credit MRV call"],
            "status": ["31094502"],
            "deadlineDate": [yesterday],
            "eligibility": ["Eligible applicants are SMEs established in an EU Member State."],
            "description": ["ESG carbon-credit MRV, emissions measurement, and sustainability research."],
        },
    }
    monkeypatch.setattr(
        funding_collector, "eu_funding_tenders_paginated_search",
        lambda **kwargs: {
            "results": [record], "pages_fetched": 1, "total_expected": 1,
            "pagination_complete": True, "errors": [],
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
    assert result.records_valid == 0
    assert result.records_filtered_by_reason["expired_deadline"] == 1
    assert result.final_status == "NO_QUALIFIED_OPPORTUNITIES"


def test_eu_collector_filters_records_without_eligibility_or_strong_domain_evidence(monkeypatch):
    from agents import funding_collector
    from agents.funding_agent import FundingSource
    from core.schemas import CollectionResult

    records = [
        {
            "reference": "missing-eligibility",
            "url": "https://ec.europa.eu/funding-tenders/opportunities/portal/screen/opportunities/competitive-calls-cs/no-elig",
            "metadata": {
                "title": ["ESG Carbon MRV Research Call"],
                "status": ["31094502"],
                "deadlineDate": ["2026-12-01"],
                "description": ["ESG carbon-credit MRV, emissions tracking, and sustainability data."],
                "beneficiaryAdministration": [
                    "<p>Administrators check eligibility according to criteria specified in the call.</p>"
                ],
            },
        },
        {
            "reference": "off-domain",
            "url": "https://ec.europa.eu/funding-tenders/opportunities/portal/screen/opportunities/competitive-calls-cs/off-domain",
            "metadata": {
                "title": ["Nuclear fission reactor facility programme"],
                "status": ["31094502"],
                "deadlineDate": ["2026-12-01"],
                "eligibility": ["Eligible applicants are nuclear research facilities established in the EU."],
                "description": ["Nuclear fission reactor research facility infrastructure."],
            },
        },
    ]
    monkeypatch.setattr(
        funding_collector, "eu_funding_tenders_paginated_search",
        lambda **kwargs: {
            "results": records, "pages_fetched": 1, "total_expected": 2,
            "pagination_complete": True, "errors": [],
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
    assert result.records_valid == 0
    assert result.records_filtered_by_reason["missing_eligibility_evidence"] == 1
    assert result.records_filtered_by_reason["off_domain_or_weak_domain_evidence"] == 1


def test_eu_document_detail_helper_uses_official_reference_endpoint(monkeypatch):
    calls = []

    def fake_fetch_json(url, **kwargs):
        calls.append((url, kwargs))
        return {"reference": "a/b", "metadata": {"beneficiaryAdministration": ["<p>content</p>"]}}, url

    monkeypatch.setattr(structured_sources, "fetch_json", fake_fetch_json)
    result = structured_sources.eu_funding_tenders_fetch_document("a/b")

    assert result["reference"] == "a/b"
    assert calls[0][0].endswith("/document/a%2Fb")
    assert calls[0][1]["params"] == {"apiKey": "SEDIA"}
