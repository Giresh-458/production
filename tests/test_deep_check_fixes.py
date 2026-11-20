"""Regression tests for defects found by the 2026-11-20 deep check (live observations)."""
from __future__ import annotations

import pytest

from tests.test_eu_topic_details_enrichment import _run, _search_hit, _topic_details


# ---------------- Batch-1 ----------------

def test_cascade_call_keeps_its_own_identity_not_the_parent_topic(monkeypatch):
    """Live: competitive-calls-cs/45656490 was labelled HORIZON-CL6-2024-CLIMATE-01-5 (closed 2024)."""
    hit = _search_hit("HORIZON-CL6-2024-CLIMATE-01-5")
    hit["url"] = "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/competitive-calls-cs/45656490"
    hit["metadata"]["type"] = ["8"]
    hit["metadata"]["eligibility"] = ["Eligible applicants are SMEs established in an EU Member State."]
    hit["metadata"]["description"] = ["ESG carbon-credit MRV and emissions measurement research for sustainability data."]
    result, fetched, _ = _run(monkeypatch, [hit], {})
    assert fetched == []  # a cascade call has no topicDetails document
    _, opp = result.opportunities[0]
    assert opp.call_id == "EU-CS-45656490"
    assert opp.evidence["parent_topic_identifier"] == "HORIZON-CL6-2024-CLIMATE-01-5"
    assert opp.research_context["funding_call_id"] == "EU-CS-45656490"


def test_portal_topic_status_is_verified_even_when_search_metadata_is_complete(monkeypatch):
    """Live: 2 of 6 topics skipped the official status check because metadata looked complete."""
    topic_id = "HORIZON-CL5-2027-01-D1-13"
    hit = _search_hit(topic_id)
    hit["metadata"]["eligibility"] = ["Eligible applicants are research organisations established in an EU Member State."]
    hit["metadata"]["description"] = ["ESG carbon-credit MRV and emissions measurement research for sustainability data."]
    result, fetched, _ = _run(monkeypatch, [hit], {topic_id: _topic_details(topic_id, status="Closed")})
    assert fetched == [topic_id]
    assert result.records_valid == 0
    assert result.records_filtered_by_reason == {"topic_details_status_not_current": 1}


@pytest.mark.parametrize("text", [
    "Demonstration of zero-emission coaches and buses for long distance operations.",
    "Low-emission transport corridors for freight logistics.",
])
def test_transport_decarbonisation_terms_qualify_as_esg(text):
    from core.schemas import rank_research_areas

    esg = next(item for item in rank_research_areas(text) if item["domain"] == "ESG")
    assert esg["raw_score"] > 0


def test_companion_eligibility_keeps_only_applicant_criteria():
    """Live: Women TechEU FAQ merge included appeal steps and 'perks via a Slack community'."""
    from agents.funding_agent import _eligibility_sentences_only

    text = (
        "Women TechEU targets early-stage deep tech startups founded, or co-founded, by women holding a top "
        "management position (CEO, CTO or equivalent). Applicants should plan their submission accordingly, "
        "allowing enough time before the deadline. Applicants should appeal if they believe the results of the "
        "eligibility checks are incorrect. The management of these perks is done via a dedicated Slack Community "
        "with +50 startups already registered. Applicants must meet all eligibility criteria, including the "
        "registration date of the company."
    )
    kept = _eligibility_sentences_only(text)
    assert "women holding a top management position" in kept
    assert "registration date of the company" in kept
    for noise in ("Slack", "appeal", "plan their submission"):
        assert noise not in kept


# ---------------- Batch-3 ----------------

@pytest.mark.parametrize("text,expect_security", [
    ("Finally, we incorporated wildfire vulnerability, as informed by retrospective estimates of burnt area.", False),
    ("Corporate sustainability reporting improves disclosure and auditability of ESG data.", False),
    ("A smart contract audit found a security vulnerability that enabled the exploit.", True),
])
def test_security_tag_requires_security_context(text, expect_security):
    from agents.tagging_agent import build_tags_for_record

    record = {"title": "Evidence", "problem_statement": text, "context_summary": text, "layer": "Literature",
              "research_area": "ESG"}
    assert ("#topic-security" in build_tags_for_record(record)["topic_tags"]) is expect_security


# ---------------- Batch-2: semantic call match ----------------

def _gate_ctx():
    from tests.test_mission_gate_tiers import _ctx
    return _ctx()


RECORD = {
    "title": "Citarum Living Lab: Co-creating visions for sustainable river revitalisation",
    "problem_statement": "No explicit problem statement found.",
    "context_summary": "A participatory river revitalisation programme in Indonesia.",
    "evidence_snippets": ["Co-creation workshops with communities shaped the river revitalisation vision."],
}


def test_semantic_match_rescues_on_topic_evidence_when_enabled(monkeypatch):
    import core.semantic_retrieval as sr
    from core.mission_gate import relevance

    monkeypatch.setenv("RIF_SEMANTIC_GATE", "true")
    monkeypatch.setattr(sr, "transformer_similarities", lambda q, docs: [0.467])
    result = relevance(RECORD, _gate_ctx())
    assert result["passed"] is True and result["relevance_tier"] == "semantic_call_match"
    assert result["semantic_similarity"] == 0.467


@pytest.mark.parametrize("env,similarities", [("false", [0.9]), ("true", None), ("true", [0.436])])
def test_semantic_match_is_off_unavailable_or_below_threshold(monkeypatch, env, similarities):
    import core.semantic_retrieval as sr
    from core.mission_gate import relevance

    monkeypatch.setenv("RIF_SEMANTIC_GATE", env)
    monkeypatch.setattr(sr, "transformer_similarities", lambda q, docs: similarities)
    assert relevance(RECORD, _gate_ctx())["passed"] is False


# ---------------- Ideas ----------------

def test_idea_templates_require_supporting_evidence():
    from agents.idea_agent import evidence_gated_dataset_need, infer_hypothesis

    nature = "Restoration of ecosystems faces implementation bottlenecks and wildfire risk."
    hypothesis = infer_hypothesis(nature, nature, "ESG", nature)
    assert "carbon-tokenization" not in hypothesis and "MRV" not in hypothesis
    assert nature in hypothesis
    assert evidence_gated_dataset_need("ESG", nature).startswith("Not yet determined")

    carbon = "Carbon credit registries lack interoperable MRV records."
    assert "carbon-tokenization" in infer_hypothesis(carbon, carbon, "ESG", carbon)
    assert "MRV datasets" in evidence_gated_dataset_need("ESG", carbon)


@pytest.mark.parametrize("problem,expected", [
    ("No explicit problem statement found.", "ESG research idea (problem unresolved)"),
    ("Insufficient problem signal", "ESG research idea (problem unresolved)"),
    ("Unresolved interoperability gaps block registry reconciliation.",
     "ESG research idea: Unresolved interoperability gaps block registry reconciliation"),
])
def test_idea_title_comes_from_grounded_problem(problem, expected):
    """Live: keyword-soup titles such as 'ESG Unscoped Found Https Research Idea'."""
    from agents.idea_agent import _is_unresolved_problem, grounded_idea_title

    resolved = "" if _is_unresolved_problem(problem) else problem
    assert grounded_idea_title(resolved, "ESG", "ignored") == expected


def test_semantic_gate_is_on_by_default_for_every_entry_point(monkeypatch):
    """Live: Batch-3 re-applied the gate without the flag and dropped 3 of 8 records."""
    from core.mission_gate import _semantic_gate_enabled

    monkeypatch.delenv("RIF_SEMANTIC_GATE", raising=False)
    assert _semantic_gate_enabled() is True
    monkeypatch.setenv("RIF_SEMANTIC_GATE", "false")
    assert _semantic_gate_enabled() is False
