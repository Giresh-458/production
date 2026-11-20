"""Regression tests for official EU topicDetails enrichment in the Batch-1 funding collector.

The Funding & Tenders Search API returns only index metadata for Horizon-style
topics (type 1). Eligibility, description, per-action status and the submission
link live in the official ``topicDetails/<id>.json`` document, and topic
conditions delegate country/consortium eligibility to Annex B of the Work
Programme General Annexes. These tests pin that behaviour without network access.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from agents import funding_collector
from agents.funding_agent import FundingSource
from core import structured_sources
from core.funding_selection import has_substantive_eligibility_evidence
from core.schemas import CollectionResult

ANNEX_URL = (
    "https://ec.europa.eu/info/funding-tenders/opportunities/docs/2021-2027/horizon/"
    "wp-call/2026-2027/wp-15-general-annexes_horizon-2026-2027_en.pdf"
)

# Verbatim structure of the 2026-2027 General Annexes text as extracted by pypdf,
# including its split words ("i n") and dash bullets.
ANNEX_TEXT = (
    "B. Eligibility\nEntities eligible for funding\nTo become a beneficiary, legal entities must be "
    "eligible for funding.\nTo be eligible for funding, applicants must be established in one of the "
    "following countries:\n– the Member States of the European Union, including their outermost "
    "regions: Austria, Belgium, Sweden.\n– countries associated to Horizon Europe; Norway, "
    "Switzerland, United Kingdom.\nOther third countries may become associated to Horizon Europe "
    "during the programme.\nConsortium composition\nUnless otherwise provided for in the specific "
    "call/topic conditions, only legal entities forming a consortium are eligible to participate i n "
    "actions provided that the consortium includes, as beneficiaries, three legal entities independent "
    "from each other and each established in a different country as follows:\n– at least one "
    "independent legal entity established in a Member State; and\n– at least two other independent "
    "legal entities, each established in different Member States or Associated Countries.\n"
)

CONDITIONS_HTML = (
    '<p>General conditions</p><p>2. Eligible Countries</p><p>described in '
    f'<a href="{ANNEX_URL}" target="_blank">Annex B</a> of the Work Programme General Annexes.</p>'
    '<p>3. Other Eligible Conditions</p><p>described in '
    f'<a href="{ANNEX_URL}">Annex B</a> of the Work Programme General Annexes.</p>'
)


def _ms(day: date) -> str:
    return str(int(datetime(day.year, day.month, day.day, 17, tzinfo=UTC).timestamp() * 1000))


def _topic_details(identifier: str, *, status: str = "Open", deadline: date | None = None,
                   action_type: str = "HORIZON-RIA HORIZON  Research and Innovation Actions",
                   description: str | None = None, conditions: str = CONDITIONS_HTML) -> dict:
    today = datetime.now(UTC).date()
    deadline = deadline or today + timedelta(days=60)
    return {
        "identifier": identifier,
        "title": "Carbon-credit MRV research",
        "callIdentifier": identifier.rsplit("-", 2)[0],
        "actions": [{
            "status": {"id": "31094502", "abbreviation": status, "description": status},
            "types": [{"typeOfAction": action_type}],
            "plannedOpeningDate": _ms(today - timedelta(days=30)),
            "deadlineDates": [_ms(deadline)],
        }],
        "budgetOverviewJSONItem": {"budgetTopicActionMap": {"1": [{
            "action": f"{identifier} - HORIZON-RIA HORIZON Research and Innovation Actions",
            "budgetYearMap": {"2026": "12000000"},
            "minContribution": "3000000", "maxContribution": "3000000", "expectedGrants": "4",
        }]}},
        "links": [
            {"url": f"https://ec.europa.eu/research/participants/submission/manage/screen/submission/create-draft/1?topic={identifier}"},
            {"url": "https://example.org/not-official"},
        ],
        "description": description or (
            "<p>Expected Outcome:</p><p>Research projects will improve ESG carbon-credit MRV, "
            "greenhouse-gas emissions measurement and sustainability data infrastructure.</p>"
            "<p>Scope:</p><p>Current carbon-credit MRV methods lack verifiable emissions data and "
            "interoperable sustainability reporting, which limits trust in carbon markets.</p>"
        ),
        "conditions": conditions,
        "_document_url": structured_sources.EU_TOPIC_DETAILS_URL_TEMPLATE.format(identifier=identifier.lower()),
    }


def _search_hit(identifier: str, *, status: str = "31094502", deadline: date | None = None) -> dict:
    deadline = deadline or datetime.now(UTC).date() + timedelta(days=60)
    return {
        "reference": f"ref-{identifier}",
        "url": f"https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/{identifier}",
        "metadata": {
            "type": ["1"],
            "identifier": [identifier],
            "callIdentifier": [identifier.rsplit("-", 2)[0]],
            "title": [f"Carbon-credit MRV and emissions research {identifier[-2:]}"],
            "status": [status],
            "deadlineDate": [f"{deadline.isoformat()}T17:00:00.000+0000"],
        },
    }


def _source() -> FundingSource:
    return FundingSource(
        name="EU Open and Forthcoming Funding Calls",
        url="https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/calls-for-proposals",
        tier="tier_3",
        collection={"api": {"types": ["1", "2", "8"], "statuses": ["31094501", "31094502"]}},
        research_domains=["ESG"],
    )


def _run(monkeypatch, hits, details_by_id, *, annex_text=ANNEX_TEXT, annex_error=None):
    monkeypatch.setattr(funding_collector, "eu_funding_tenders_paginated_search", lambda **kwargs: {
        "results": hits, "pages_fetched": 1, "total_expected": len(hits),
        "pagination_complete": True, "errors": [],
    })
    fetched = []

    def fake_details(identifier):
        fetched.append(identifier)
        value = details_by_id.get(identifier)
        if isinstance(value, Exception):
            raise value
        return value

    annex_calls = []

    def fake_annex(url):
        annex_calls.append(url)
        if annex_error:
            raise annex_error
        return annex_text

    def no_search_detail(_identifier):  # the old Search API re-query must not run for topics
        raise AssertionError("Search API detail re-query used for a portal topic")

    monkeypatch.setattr(funding_collector, "eu_funding_tenders_fetch_topic_details", fake_details)
    monkeypatch.setattr(funding_collector, "eu_funding_tenders_fetch_reference_pdf_text", fake_annex)
    monkeypatch.setattr(funding_collector, "eu_funding_tenders_fetch_document", no_search_detail)
    result = funding_collector._collect_eu_funding_tenders(
        _source(), CollectionResult(source_id="EU", method="api"), warnings=[]
    )
    return result, fetched, annex_calls


# --- topicDetails interpretation -------------------------------------------------

def test_topic_facts_use_next_unexpired_deadline_and_topic_specific_budget():
    today = date(2026, 11, 19)
    details = _topic_details("HORIZON-CL5-2026-11-D3-06")
    details["actions"][0]["deadlineDates"] = [_ms(date(2026, 9, 1)), _ms(date(2027, 2, 1))]
    details["budgetOverviewJSONItem"]["budgetTopicActionMap"]["2"] = [{
        "action": "HORIZON-CL5-2026-11-D3-05 - HORIZON-IA", "budgetYearMap": {"2026": "99"},
        "minContribution": "1", "maxContribution": "1",
    }]
    facts = funding_collector._eu_topic_details_facts(details, "HORIZON-CL5-2026-11-D3-06", today=today)
    assert facts["status_label"] == "Open"
    assert facts["deadline"] == "2027-02-01"
    assert facts["budget"] == "topic budget EUR 12,000,000; EU contribution per project EUR 3,000,000; expected grants 4"
    assert facts["submission_links"] == [details["links"][0]["url"]]
    assert "Research and Innovation Actions" in facts["action_types"][0]


@pytest.mark.parametrize("labels,expected", [
    (["Forthcoming"], "Forthcoming"), (["Closed"], "Closed"), (["Closed", "Open"], "Open"), ([], None),
])
def test_topic_facts_status_label(labels, expected):
    details = _topic_details("HORIZON-CL5-2026-11-D3-06")
    details["actions"] = [{"status": {"abbreviation": label}} for label in labels]
    assert funding_collector._eu_topic_details_facts(details, "HORIZON-CL5-2026-11-D3-06")["status_label"] == expected


# --- Annex B eligibility resolution -----------------------------------------------

def test_annex_eligibility_is_resolved_verbatim_from_cited_official_annex(monkeypatch):
    monkeypatch.setattr(funding_collector, "eu_funding_tenders_fetch_reference_pdf_text", lambda url: ANNEX_TEXT)
    text, url, error = funding_collector._eu_resolve_annex_eligibility(CONDITIONS_HTML)
    assert error is None and url == ANNEX_URL
    assert text.startswith("To be eligible for funding, applicants must be established in one of the following countries")
    assert "countries associated to Horizon Europe" in text
    assert "at least one independent legal entity established in a Member State" in text
    assert "Other third countries may become associated" not in text
    assert has_substantive_eligibility_evidence(text)


def test_annex_is_not_consulted_when_conditions_do_not_cite_it_for_eligibility(monkeypatch):
    def fail(_url):
        raise AssertionError("annex fetched without an eligibility citation")
    monkeypatch.setattr(funding_collector, "eu_funding_tenders_fetch_reference_pdf_text", fail)
    conditions = f'<p>Award criteria described in <a href="{ANNEX_URL}">Annex D</a>.</p>'
    assert funding_collector._eu_resolve_annex_eligibility(conditions) == (None, None, None)


def test_annex_without_eligibility_passages_is_not_accepted(monkeypatch):
    monkeypatch.setattr(funding_collector, "eu_funding_tenders_fetch_reference_pdf_text", lambda url: "Annex B\nNothing relevant.")
    text, url, error = funding_collector._eu_resolve_annex_eligibility(CONDITIONS_HTML)
    assert text is None and url == ANNEX_URL and "not found" in error


def test_annex_on_unofficial_host_is_rejected():
    conditions = '<p>Eligible Countries described in <a href="https://evil.example/annex.pdf">Annex B</a></p>'
    text, url, error = funding_collector._eu_resolve_annex_eligibility(conditions)
    assert text is None and url == "https://evil.example/annex.pdf"
    assert "Not an official" in error


# --- collector integration ----------------------------------------------------------

def test_portal_topic_without_search_eligibility_is_enriched_from_official_topic_details(monkeypatch):
    topic_id = "HORIZON-CL5-2026-11-D3-06"
    result, fetched, annex_calls = _run(monkeypatch, [_search_hit(topic_id)], {topic_id: _topic_details(topic_id)})

    assert fetched == [topic_id]
    assert annex_calls == [ANNEX_URL]
    assert result.records_valid == 1, result.records_filtered_by_reason
    assert result.final_status == "VERIFIED"
    doc, opp = result.opportunities[0]
    assert opp.call_id == topic_id
    assert opp.evidence["parent_call_identifier"] == "HORIZON-CL5-2026-11"
    assert opp.status == "OPEN_CALL"
    assert "topicDetails" in opp.evidence["source_status_provenance"]
    assert "official topicDetails action status 'Open'" in opp.evidence["status_cross_check"]
    assert opp.application_url.startswith("https://ec.europa.eu/research/participants/submission/")
    assert "submission-service link" in opp.evidence["application_url"]
    assert has_substantive_eligibility_evidence(opp.eligibility)
    assert "Annex B" in opp.eligibility
    assert ANNEX_URL in opp.evidence["eligibility_provenance"]
    assert "EUR 3,000,000" in opp.funding_amount
    assert "Type of action: HORIZON-RIA HORIZON Research and Innovation Actions" in doc.content
    assert opp.research_context["research_intent"] == "research"


def test_topic_details_closed_status_overrides_stale_search_open_status(monkeypatch):
    topic_id = "HORIZON-CL5-2026-11-D3-07"
    result, _, _ = _run(monkeypatch, [_search_hit(topic_id)], {topic_id: _topic_details(topic_id, status="Closed")})
    assert result.records_valid == 0
    assert result.records_filtered_by_reason == {"topic_details_status_not_current": 1}


def test_topic_details_forthcoming_status_is_emitted_as_upcoming_not_open(monkeypatch):
    topic_id = "HORIZON-CL5-2026-11-D3-08"
    result, _, _ = _run(monkeypatch, [_search_hit(topic_id)], {topic_id: _topic_details(topic_id, status="Forthcoming")})
    assert result.records_valid == 1
    assert result.opportunities[0][1].status == "UPCOMING_CALL"


def test_topic_details_without_any_action_status_is_not_treated_as_verified(monkeypatch):
    topic_id = "HORIZON-CL5-2026-11-D3-09"
    details = _topic_details(topic_id)
    details["actions"][0]["status"] = {}
    result, _, _ = _run(monkeypatch, [_search_hit(topic_id)], {topic_id: details})
    assert result.records_valid == 0
    assert result.records_filtered_by_reason == {"topic_details_status_missing": 1}


def test_topic_details_fetch_failure_is_reported_and_record_is_not_promoted(monkeypatch):
    topic_id = "HORIZON-CL5-2026-11-D3-10"
    result, _, _ = _run(monkeypatch, [_search_hit(topic_id)], {topic_id: RuntimeError("HTTP 503")})
    assert result.records_valid == 0
    assert result.records_filtered_by_reason == {"missing_eligibility_evidence": 1}
    assert result.detail_pages_failed == 1
    assert result.final_status == "FAIL"
    assert any("HTTP 503" in error["error"] for error in result.errors)


def test_unreachable_annex_is_reported_once_and_eligibility_stays_unknown(monkeypatch):
    ids = ["HORIZON-CL5-2026-11-D3-11", "HORIZON-CL5-2026-11-D3-12"]
    result, _, annex_calls = _run(
        monkeypatch, [_search_hit(i) for i in ids], {i: _topic_details(i) for i in ids},
        annex_error=RuntimeError("timeout"),
    )
    assert result.records_valid == 0
    assert result.records_filtered_by_reason == {"missing_eligibility_evidence": 2}
    assert len(annex_calls) == 2
    annex_errors = [e for e in result.errors if e["url"] == ANNEX_URL]
    assert len(annex_errors) == 1 and result.documents_failed == 1


def test_sibling_topics_of_one_call_keep_distinct_identities(monkeypatch):
    ids = ["HORIZON-CL5-2026-11-D3-13", "HORIZON-CL5-2026-11-D3-14"]
    result, _, _ = _run(monkeypatch, [_search_hit(i) for i in ids], {i: _topic_details(i) for i in ids})
    assert sorted(opp.call_id for _, opp in result.opportunities) == ids


def test_expired_topic_deadline_from_topic_details_is_filtered(monkeypatch):
    topic_id = "HORIZON-CL5-2026-11-D3-15"
    past = datetime.now(UTC).date() - timedelta(days=3)
    details = _topic_details(topic_id, deadline=past)
    result, _, _ = _run(monkeypatch, [_search_hit(topic_id)], {topic_id: details})
    assert result.records_valid == 0
    assert result.records_filtered_by_reason == {"expired_deadline": 1}


def test_europeaid_references_do_not_use_topic_details(monkeypatch):
    hit = _search_hit("HORIZON-CL5-2026-11-D3-16")
    hit["metadata"]["identifier"] = ["EuropeAid/167264/DD/ACT/Multi"]
    monkeypatch.setattr(funding_collector, "eu_funding_tenders_paginated_search", lambda **kwargs: {
        "results": [hit], "pages_fetched": 1, "total_expected": 1, "pagination_complete": True, "errors": [],
    })
    monkeypatch.setattr(funding_collector, "eu_funding_tenders_fetch_topic_details",
                        lambda identifier: (_ for _ in ()).throw(AssertionError("topicDetails used")))
    searched = []
    monkeypatch.setattr(funding_collector, "eu_funding_tenders_fetch_document",
                        lambda identifier: searched.append(identifier) or {"metadata": {}})
    result = funding_collector._collect_eu_funding_tenders(
        _source(), CollectionResult(source_id="EU", method="api"), warnings=[]
    )
    assert searched == ["EuropeAid/167264/DD/ACT/Multi"]
    assert not [e for e in result.errors if "topicDetails" in str(e.get("error"))]


# --- structured source helpers ---------------------------------------------------------

def test_topic_details_helper_uses_lowercase_official_document_and_checks_identity(monkeypatch):
    calls = []

    def fake_fetch_json(url, **kwargs):
        calls.append(url)
        return {"TopicDetails": {"identifier": "HORIZON-CL5-2026-11-D3-06", "actions": []}}, url

    monkeypatch.setattr(structured_sources, "fetch_json", fake_fetch_json)
    details = structured_sources.eu_funding_tenders_fetch_topic_details("HORIZON-CL5-2026-11-D3-06")
    assert calls == ["https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/horizon-cl5-2026-11-d3-06.json"]
    assert details["_document_url"] == calls[0]


def test_topic_details_helper_rejects_mismatched_document(monkeypatch):
    monkeypatch.setattr(structured_sources, "fetch_json",
                        lambda url, **kw: ({"TopicDetails": {"identifier": "OTHER-TOPIC"}}, url))
    with pytest.raises(ValueError, match="mismatch"):
        structured_sources.eu_funding_tenders_fetch_topic_details("HORIZON-CL5-2026-11-D3-06")


@pytest.mark.parametrize("identifier", ["", "EuropeAid/167264/DD/ACT/Multi", "../../etc", "a b"])
def test_topic_details_helper_rejects_non_topic_identifiers(identifier):
    with pytest.raises(ValueError):
        structured_sources.eu_funding_tenders_fetch_topic_details(identifier)


@pytest.mark.parametrize("url", [
    "http://ec.europa.eu/info/funding-tenders/opportunities/docs/x.pdf",
    "https://ec.europa.eu.evil.example/info/funding-tenders/opportunities/docs/x.pdf",
    "https://ec.europa.eu/info/other/x.pdf",
    "https://ec.europa.eu/info/funding-tenders/opportunities/docs/x.html",
])
def test_reference_pdf_helper_only_accepts_official_portal_pdfs(url):
    with pytest.raises(ValueError, match="Not an official"):
        structured_sources.eu_funding_tenders_fetch_reference_pdf_text(url)


# --- Batch-1 -> Batch-2 handoff (network mocked, everything else real) -------------

def _scan_and_select(monkeypatch, tmp_path, details_by_id, hits):
    from agents import funding_agent
    from core.batch2 import validate_batch1_context
    from run_pipeline import build__selected_call_context, get_all_open_funding_calls

    source = _source()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(funding_agent, "load_sources", lambda _path: [source])

    def fake_collect_auto_sources(sources, area=None, warnings=None, is_test_mode=False):
        result, _, _ = _run(monkeypatch, hits, details_by_id)
        result.source_id = source.name
        yield result

    monkeypatch.setattr(funding_collector, "collect_auto_sources", fake_collect_auto_sources)
    response = funding_agent.run_agent("configured_scan", input_data={
        "sources_path": str(tmp_path / "sources.yaml"),
        "output_dir": str(tmp_path / "markdown"),
        "db_path": str(tmp_path / "funding.db"),
        "analysis_mode": "collect_only",
        "force_refresh": True,
        "run_id": "handoff-test",
    })
    selected, rejected = get_all_open_funding_calls(
        {"outputs": response.get("outputs", [])},
        require_domain_match=True,
        require_research_context=True,
    )
    contexts = [build__selected_call_context(record) for record in selected]
    return response, selected, rejected, contexts, validate_batch1_context


def test_enriched_open_research_topic_reaches_batch2_with_valid_context(monkeypatch, tmp_path):
    topic_id = "HORIZON-CL5-2026-11-D3-21"
    response, selected, rejected, contexts, validate = _scan_and_select(
        monkeypatch, tmp_path, {topic_id: _topic_details(topic_id)}, [_search_hit(topic_id)]
    )
    assert response["status"] == "success"
    assert [r["call_id"] for r in selected] == [topic_id], rejected
    context = contexts[0]
    assert validate(context) == []
    assert context.status == "OPEN_CALL"
    assert context.application_url.startswith("https://ec.europa.eu/research/participants/submission/")
    rc = context.research_context
    assert rc.research_intent == "research"
    assert any(str(ref).startswith("https://ec.europa.eu/") for ref in rc.evidence_refs)


@pytest.mark.parametrize("variant,expected_reason", [
    ("forthcoming", "status=UPCOMING_CALL"),
    ("innovation_action", "research intent is not verified as research (research_intent=non_research)"),
])
def test_non_open_or_non_research_topics_never_reach_batch2(monkeypatch, tmp_path, variant, expected_reason):
    topic_id = "HORIZON-CL5-2026-11-D3-22"
    if variant == "forthcoming":
        details = _topic_details(topic_id, status="Forthcoming")
    else:
        details = _topic_details(
            topic_id,
            action_type="HORIZON-IA HORIZON Innovation Actions",
            description=(
                "<p>Expected Outcome:</p><p>Deployment of ESG carbon-credit MRV platforms and "
                "greenhouse-gas emissions measurement services for sustainability reporting.</p>"
            ),
        )
    _, selected, rejected, contexts, _ = _scan_and_select(
        monkeypatch, tmp_path, {topic_id: details}, [_search_hit(topic_id)]
    )
    assert selected == [] and contexts == []
    assert len(rejected) == 1
    assert expected_reason in rejected[0]["reasons"]


# --- research intent from the official type of action ----------------------------

@pytest.mark.parametrize("action_type,expected", [
    ("HORIZON-RIA HORIZON  Research and Innovation Actions", "research"),
    ("HORIZON-ERC HORIZON ERC Grants", "research"),
    ("HORIZON-TMA-MSCA-DN HORIZON TMA MSCA Doctoral Networks", "research"),
    ("HORIZON-CSA HORIZON Coordination and Support Actions", "non_research"),
    ("HORIZON-IA HORIZON Innovation Actions", "non_research"),
    ("HORIZON-PCP HORIZON Pre-commercial Procurement", "non_research"),
    ("HORIZON-PPI HORIZON Public Procurement of Innovative solutions", "non_research"),
    ("HORIZON-COFUND HORIZON Programme Cofund Actions", "non_research"),
    ("HORIZON-ERC-POC HORIZON ERC Proof of Concept Grants", "non_research"),
])
def test_official_action_type_determines_research_intent(action_type, expected):
    intent, evidence = funding_collector._eu_action_type_research_intent([action_type])
    assert intent == expected
    assert evidence.startswith("Official EU type of action:")


def test_missing_action_type_leaves_intent_to_source_text():
    assert funding_collector._eu_action_type_research_intent([]) == (None, None)


def test_pcp_topic_with_research_keywords_is_not_research(monkeypatch):
    """Live regression: HORIZON-HLTH-2027-01-ENVHLTH-MISSCLIMA-03 (PCP) was labelled research."""
    topic_id = "HORIZON-HLTH-2027-01-ENVHLTH-MISSCLIMA-03"
    details = _topic_details(
        topic_id,
        action_type="HORIZON-PCP HORIZON Pre-commercial Procurement",
        description=(
            "<p>Expected Outcome:</p><p>Research on ESG carbon-credit MRV and greenhouse-gas emissions "
            "measurement for sustainability.</p><p>This topic does not provide direct funding to "
            "developers, industry or research organisations to perform Research and Development (R&amp;D).</p>"
        ),
    )
    result, _, _ = _run(monkeypatch, [_search_hit(topic_id)], {topic_id: details})
    rc = result.opportunities[0][1].research_context
    assert rc["research_intent"] == "non_research"
    assert rc["research_intent_evidence"] == [
        "Official EU type of action: HORIZON-PCP HORIZON Pre-commercial Procurement (not a research action type)"
    ]


def test_csa_networking_topic_is_not_research(monkeypatch):
    """Live regression: HORIZON-NATURE-2027-02-03 (CSA) was labelled research."""
    topic_id = "HORIZON-NATURE-2027-02-03"
    details = _topic_details(topic_id, action_type="HORIZON-CSA HORIZON Coordination and Support Actions")
    result, _, _ = _run(monkeypatch, [_search_hit(topic_id)], {topic_id: details})
    assert result.opportunities[0][1].research_context["research_intent"] == "non_research"


def test_ria_topic_records_official_type_first_in_intent_evidence(monkeypatch):
    topic_id = "HORIZON-NATURE-2027-02-02"
    result, _, _ = _run(monkeypatch, [_search_hit(topic_id)], {topic_id: _topic_details(topic_id)})
    _, opp = result.opportunities[0]
    rc = opp.research_context
    assert rc["research_intent"] == "research"
    assert rc["research_intent_evidence"][0] == (
        "Official EU type of action: HORIZON-RIA HORIZON Research and Innovation Actions"
    )
    assert "type of action" in opp.evidence["research_intent_provenance"]


# --- topic-specific eligibility conditions (double-check finding) --------------------

NATURE_CONDITIONS = (
    "<p>General conditions</p><p>2. Eligible countries</p><p>described in "
    f'<a href="{ANNEX_URL}">Annex B</a> of the Work Programme General Annexes.</p>'
    "<p>A number of non-EU/non-Associated Countries that are not automatically eligible for funding "
    "have made specific provisions for making funding available for their participants in Horizon "
    "Europe projects. See the information in the Horizon Europe Programme Guide .</p>"
    "<p>3. Other Eligibility Conditions</p><p>The following additional eligibility criteria apply: "
    "The proposals must apply the multi-actor approach. See definition of the multi-actor approach in "
    "this work programme part.</p><p>described in "
    f'<a href="{ANNEX_URL}">Annex B</a> of the Work Programme General Annexes.</p>'
    "<p>4. Financial and operational capacity and exclusion</p><p>described in Annex C.</p>"
)


def test_topic_specific_eligibility_is_extracted_verbatim_without_boilerplate():
    text = funding_collector._eu_topic_specific_eligibility(NATURE_CONDITIONS)
    assert text == (
        "Topic-specific eligibility conditions: The following additional eligibility criteria apply: "
        "The proposals must apply the multi-actor approach. See definition of the multi-actor approach "
        "in this work programme part."
    )


def test_conditions_that_only_point_to_annex_b_add_nothing():
    assert funding_collector._eu_topic_specific_eligibility(CONDITIONS_HTML) is None


def test_handoff_eligibility_carries_topic_specific_criteria(monkeypatch):
    """Live regression: HORIZON-NATURE-2027-02-02 lost its multi-actor requirement."""
    topic_id = "HORIZON-NATURE-2027-02-02"
    details = _topic_details(topic_id, conditions=NATURE_CONDITIONS)
    result, _, _ = _run(monkeypatch, [_search_hit(topic_id)], {topic_id: details})
    _, opp = result.opportunities[0]
    assert "The proposals must apply the multi-actor approach." in opp.eligibility
    assert "To be eligible for funding, applicants must be established" in opp.eligibility
    assert "A number of non-EU/non-Associated Countries" not in opp.eligibility


def test_two_stage_topic_uses_first_stage_deadline_for_new_applicants():
    today = date(2027, 3, 1)
    topic_id = "HORIZON-MISS-2027-05-SOIL-06-two-stage"
    details = _topic_details(topic_id)
    details["actions"][0]["deadlineDates"] = [_ms(date(2027, 4, 8)), _ms(date(2027, 9, 15))]
    details["budgetOverviewJSONItem"]["budgetTopicActionMap"]["1"][0]["deadlineModel"] = "two-stage"
    facts = funding_collector._eu_topic_details_facts(details, topic_id, today=today)
    assert facts["deadline"] == "2027-04-08"
    # After stage 1 closes, the stage-2 date must not make the topic look open.
    later = funding_collector._eu_topic_details_facts(details, topic_id, today=date(2027, 5, 1))
    assert later["deadline"] == "2027-04-08"


def test_two_stage_topic_past_first_stage_is_filtered_as_expired(monkeypatch):
    topic_id = "HORIZON-MISS-2027-05-SOIL-07-two-stage"
    today = datetime.now(UTC).date()
    details = _topic_details(topic_id)
    details["actions"][0]["deadlineDates"] = [_ms(today - timedelta(days=5)), _ms(today + timedelta(days=120))]
    details["budgetOverviewJSONItem"]["budgetTopicActionMap"]["1"][0]["deadlineModel"] = "two-stage"
    hit = _search_hit(topic_id, deadline=today + timedelta(days=120))  # stale index metadata
    result, _, _ = _run(monkeypatch, [hit], {topic_id: details})
    assert result.records_valid == 0
    assert result.records_filtered_by_reason == {"expired_deadline": 1}
