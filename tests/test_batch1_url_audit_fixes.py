"""Regression tests for the Batch-1 per-URL audit (2026-11-20, live examples)."""
from __future__ import annotations

import pytest

from core.problem_extraction import extract_problem_intelligence


def _funding_problem(content: str, title: str = "Topic title"):
    return extract_problem_intelligence(
        content=content, title=title, evidence_bundle={"evidence_snippets": []},
        allow_llm_refinement=False, research_area="ESG", agent_name="funding",
    )["selected_problem"]


# ---------------- problem sentences (official EU topic text, verbatim) ----------------

def test_encouragement_is_not_a_problem():  # HORIZON-CL5-2027-01-D1-07
    text = ("Scope: Cooperation with global, EU, national and regional actors in the field of Disaster Risk "
            "Reduction and climate adaptation, including through integration into or interoperability with "
            "Copernicus, Destination Earth and other relevant initiatives is encouraged.")
    assert _funding_problem(text) is None


def test_real_problem_wins_over_definition_fragment():  # HORIZON-CL5-2027-01-D1-10
    text = ("Scope: In this context, maladaptation refers to adaptation actions unintentionally leading to increased "
            "risk of adverse climate-related outcomes. However, while adaptation is essential, not fully informed "
            "measures can backfire, leading to severe unintended consequences or to short-term gains that are not "
            "sustainable.")
    assert _funding_problem(text).startswith("However, while adaptation is essential")


def test_gaps_and_uncertainties_remain_is_a_problem():  # HORIZON-CL5-2027-01-D1-12 (was rejected)
    text = ("Scope: Land and ocean carbon sinks are central to climate policy. However, significant gaps and "
            "uncertainties remain in quantifying these complex processes and their components.")
    assert _funding_problem(text) == (
        "However, significant gaps and uncertainties remain in quantifying these complex processes and their components."
    )


def test_expected_outcome_section_is_never_the_problem():  # HORIZON-CL5-2027-01-D1-10 / CLIMATE-02
    text = ("Expected Outcome: The risk and adaptation assessment frameworks are improved by better integrating the "
            "maladaptation dimension, reducing risks faced by vulnerable communities. "
            "Scope: The topic covers modelling approaches.")
    assert _funding_problem(text) is None


@pytest.mark.parametrize("sentence", [
    "From risk assessment to climate forecasting, from infrastructure planning to resource management, and more.",
    "Projects should consider how high amounts of data transfers may pose risks in terms of data integrity.",
    "Proposals selected in Area A are also expected to collaborate with the projects selected under another topic.",
    "b) To address cybersecurity risks, external threats and proliferation concerns and safety in ports.",
    "degraded marine and coastal habitats and species",
    "Finally, biotechnology solutions can provide innovative methods to rehabilitate degraded soils and lands.",
])
def test_instructions_fragments_and_risk_compounds_are_not_problems(sentence):
    assert _funding_problem("Scope: " + sentence) is None


def test_call_title_is_not_its_own_problem():  # HORIZON-NATURE-2027-02-02
    title = ("Living labs co-creating solutions for the restoration of ecosystems addressing implementation "
             "bottlenecks and improving resilience to risks")
    assert _funding_problem(f"# {title}\n\nScope: Living labs are a participatory approach.", title=title) is None


# ---------------- analyze_document: themes, title anchor, priorities, titles ----------------

def _analyze(title: str, content: str):
    from agents.funding_agent import FundingDocument, analyze_document

    return analyze_document(FundingDocument(
        organization="EU", title=title, source="https://example.org/call", source_type="web",
        content=content, year=2026,
    ))


def test_carbon_and_mrv_theme_requires_carbon_evidence():
    no_carbon = _analyze("Advancing modelling of extreme events in a changing climate",
                         "Climate adaptation and climate resilience research on extreme events and climate risk.")
    assert no_carbon.focus_area == "ESG"
    assert "Carbon and MRV" not in no_carbon.keywords
    with_carbon = _analyze("Carbon removal monitoring", "Climate adaptation research with MRV of carbon removals and emissions.")
    assert "Carbon and MRV" in with_carbon.keywords


def test_unhyphenated_title_anchor_routes_to_domain():  # HORIZON-CL5-2027-03-D5-04
    analysis = _analyze("Demonstration of zero emission coaches and buses in long distance operations",
                        "Zero-emission coaches and buses for long distance operations. Zero-emission fleets "
                        "and zero-emission charging corridors are demonstrated.")
    assert analysis.focus_area == "ESG"


def test_priority_keywords_are_whole_words_not_modifiers():
    analysis = _analyze("Restoration living labs", "Implementing regulations can reuse effective deployment frameworks.")
    assert "Deployment" not in analysis.research_priorities


def test_announcement_headline_is_not_the_programme_name():  # filpgf.io
    from agents.funding_agent import FundingDocument, analyze_document

    analysis = analyze_document(FundingDocument(
        organization="Filecoin", title="Announcing the General Track", source="https://example.org/propgf",
        source_type="web", content="# Announcing the General Track\n\nGrants for public goods.", year=2026,
    ))
    assert analysis.program_name == "General Track"


# ---------------- dates ----------------

def test_deadline_prefers_last_submission_day_over_review_cutoff():  # filpgf.io
    from agents.funding_agent import extract_call_dates

    text = ("Applications Open 2026 Deadline 9 Jun 2026 Cut-off for the June review window "
            "Deadline 16 Jun 2026 Last day to submit Batch 3 applications")
    assert extract_call_dates(text) == (None, "2026-06-16")


def test_opening_date_does_not_cross_a_deadline_label():
    from agents.funding_agent import extract_call_dates

    assert extract_call_dates("Applications open 1 May 2026. Deadline: 9 Jun 2026") == ("2026-05-01", "2026-06-09")


# ---------------- status evidence, CTAs, reasons ----------------

def test_status_evidence_cites_the_sentence_that_closed_the_programme():  # Flashbots README
    from agents.funding_agent import _status_evidence_for_classification

    text = ("No, grant fellows are accepted on a rolling basis. "
            "Please note that grant giving through FRP is currently paused, but we remain open to collaboration.")
    evidence = _status_evidence_for_classification(text, "CLOSED_CALL")
    assert "currently paused" in evidence


def test_on_page_cta_application_url_is_host_independent():
    from agents.funding_agent import _resolve_application_url

    page = "https://programme.example.net/"
    assert _resolve_application_url("Build with us. Apply for the Program", page, "OPEN_CALL") == page
    assert _resolve_application_url("Apply for the Program", page, "CLOSED_CALL") is None


def test_no_site_specific_cta_literals_remain():
    from pathlib import Path

    for path in ("agents/funding_agent.py", "agents/funding_collector.py"):
        source = Path(path).read_text(encoding="utf-8").casefold()
        for literal in ("nearprotocolrewards", "hzn.xyz", "apply to horizon", "apply for protocol rewards",
                        "is_superteam_detail"):
            assert literal not in source, (path, literal)


def test_missing_anchor_reason_names_the_actual_domain():
    from core.schemas import rank_research_areas, required_anchor_message

    esg = next(i for i in rank_research_areas("New European Bauhaus circularity for vertical spaces")
               if i["domain"] == "ESG")
    assert "ZK-IoV" not in esg["evidence"]
    assert required_anchor_message("ESG").startswith("ESG requires")
    assert required_anchor_message("ZK-IoV").startswith("ZK-IoV requires direct evidence for both")


@pytest.mark.parametrize("title", [
    "Concentrated solar thermal systems for decarbonising industrial processes",
    "Sustainable aircraft design towards a climate-neutral aviation",
])
def test_decarbonisation_word_forms_are_esg(title):
    from core.schemas import rank_research_areas

    assert next(i for i in rank_research_areas(title) if i["domain"] == "ESG")["raw_score"] > 0


# ---------------- EU collector: cancellation + itemised filters ----------------

def test_cancelled_topic_is_detected_from_official_identifier_and_notice():
    from agents.funding_collector import _eu_cancellation_facts

    cancelled = _eu_cancellation_facts({"identifier": "HORIZON-NEB-2027-01-REGEN-03-CANCELLED"}, "x")
    assert cancelled["cancelled"] is True
    proposed = _eu_cancellation_facts({
        "identifier": "HORIZON-X-01",
        "latestInfos": [{"content": "<p>As part of this amendment, it is proposed to cancel topic "
                                    "<strong>HORIZON-X-01</strong> and replace it.</p>"}],
    }, "HORIZON-X-01")
    assert proposed["cancelled"] is False and proposed["cancellation_proposed_notice"]
    assert _eu_cancellation_facts({"identifier": "HORIZON-Y-02"}, "HORIZON-Y-02")["cancelled"] is False


def test_filtered_records_are_itemised():
    from agents.funding_collector import _record_eu_filter
    from core.schemas import CollectionResult

    result = CollectionResult(source_id="EU", method="api")
    _record_eu_filter(result, "off_domain_or_weak_domain_evidence", "Some topic", "https://ec.europa.eu/x")
    assert result.records_filtered == 1
    assert result.records_filtered_details == [
        {"title": "Some topic", "url": "https://ec.europa.eu/x", "reason": "off_domain_or_weak_domain_evidence"}
    ]
