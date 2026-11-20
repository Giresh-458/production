"""Regression tests for the Batch-2/Batch-3 audit (2026-11-20, live examples)."""
from __future__ import annotations

import pytest


# ---------------- Batch-2: relevance gate ----------------

def _ctx():
    from tests.test_mission_gate_tiers import _ctx as base
    return base()


GREENWASHING = {
    "title": "Greenwashing, carbon emission, and ESG",
    "problem_statement": "No explicit problem statement found.",
    "context_summary": "ESG ratings and carbon footprints of firms.",
    "evidence_snippets": ["Our findings indicate that high ESG-rated firms do not have lower carbon emissions."],
}


def test_domain_word_alone_needs_semantic_relation_to_the_call(monkeypatch):
    import core.semantic_retrieval as sr
    from core.mission_gate import relevance

    monkeypatch.setenv("RIF_SEMANTIC_GATE", "true")
    monkeypatch.setattr(sr, "transformer_similarities", lambda q, d: [0.313])  # live cosine
    result = relevance(GREENWASHING, _ctx())
    assert result["passed"] is False and "semantically unrelated" in result["reason"]

    monkeypatch.setattr(sr, "transformer_similarities", lambda q, d: [0.531])
    assert relevance(GREENWASHING, _ctx())["relevance_tier"] == "domain_anchor"

    monkeypatch.setattr(sr, "transformer_similarities", lambda q, d: None)  # model unavailable
    assert relevance(GREENWASHING, _ctx())["passed"] is True


# ---------------- Batch-2: problem statements ----------------

@pytest.mark.parametrize("candidates,title", [
    (["How risk management can prevent future wildfire disasters in the wildland-urban interface."],
     "How risk management can prevent future wildfire disasters in the wildland-urban interface"),
    (["Current regulatory developments, such as the European Corporate Sustainability Reporting Directive (CSRD), "
      "are pushing to make ESG indicators (within this triple perspective: social, environmental and governance "
      "risks) a key set of information to be used for reporters and users of information."],
     "Climate change, ESG criteria and recent regulation"),
])
def test_title_and_context_sentences_are_not_problems(candidates, title):
    from core.normalization import _select_problem_statement

    assert _select_problem_statement(candidates, title=title) == "No explicit problem statement found."


def test_genuine_problem_still_selected():
    from core.normalization import _select_problem_statement

    s = ("Despite these critical benefits, restoration efforts often face significant challenges, including slow "
         "progress, limited scalability, high costs, and uncertain outcomes.")
    assert _select_problem_statement([s], title="Transformative living labs") == s


def test_url_is_never_the_actor():
    from core.normalization import _normalized_actor_and_focus

    actor, _ = _normalized_actor_and_focus({}, {"source": "https://doi.org/10.1002/bse.3929"}, {}, "ESG")
    assert not actor.startswith("http")


def test_openalex_documents_carry_authors_venue_and_type(monkeypatch):
    import core.http_client as http
    from agents.adaptive_research_agent import _openalex

    work = {
        "display_name": "Greenwashing, carbon emission, and ESG", "doi": "https://doi.org/10.1002/bse.3929",
        "abstract_inverted_index": {"Firms": [0], "report": [1]},
        "authorships": [{"author": {"display_name": "Jane Doe"}}, {"author": {"display_name": "John Roe"}}],
        "primary_location": {"source": {"display_name": "Business Strategy and the Environment"}},
    }
    monkeypatch.setattr(http, "fetch_json", lambda *a, **k: ({"results": [work]}, None))
    doc = _openalex("greenwashing")[0]
    assert doc.authors == ["Jane Doe", "John Roe"]
    assert doc.venue == "Business Strategy and the Environment"
    assert doc.evidence_type == "Scholarly publication record" and doc.source_type == "Research publication"


# ---------------- Batch-3: shared topic vocabulary ----------------

@pytest.mark.parametrize("text,expected", [
    ("This study explores the real effects of environmental, social, and governance (ESG) reporting.", "governance"),
    ("The proposal extends the scope to all large companies.", "governance"),
    ("A lack of robust analytics is a bottleneck for developing predictive analytics.", "scalability"),
    ("This is a candidate gap, validated against the evidence already preserved.", "identity"),
    ("This is a candidate gap, validated against the evidence already preserved.", "stablecoin"),
    ("Finally, we incorporated wildfire vulnerability into the model.", "security"),
])
def test_wrong_word_sense_does_not_create_topics(text, expected):
    from core.topic_keywords import match_topics

    assert expected not in match_topics(text)


@pytest.mark.parametrize("text,expected", [
    ("Engaging multiple levels of governance in living labs.", "governance"),
    ("A smart contract audit found a security vulnerability.", "security"),
    ("Verification bottleneck: throughput collapses under load.", "scalability"),
])
def test_genuine_topics_still_match(text, expected):
    from core.topic_keywords import match_topics

    assert expected in match_topics(text)


def test_all_agents_share_one_topic_vocabulary():
    import agents.idea_agent as idea
    import agents.synthesis_agent as synthesis
    import agents.tagging_agent as tagging
    import agents.trend_agent as trend
    from core.topic_keywords import TOPIC_KEYWORDS

    assert tagging.TOPIC_KEYWORDS is TOPIC_KEYWORDS
    assert trend.TOPIC_KEYWORDS is TOPIC_KEYWORDS
    assert synthesis.TOPIC_LABELS is TOPIC_KEYWORDS
    assert idea.TOPIC_HINTS is TOPIC_KEYWORDS


# ---------------- synthesis / ideas ----------------

def test_recurrence_claim_counts_only_members_with_a_problem():
    from agents.synthesis_agent import build_evidence_assessment

    members = [
        {"problem_statement": "No explicit problem statement found.", "source_url": f"https://s{i}.org", "layer": "Literature",
         "agent_name": "literature", "actor": f"A{i}"}
        for i in range(3)
    ]
    assessment = build_evidence_assessment({"members": members}, [])
    claim = assessment["claims"][0]
    assert claim["support"] == 0 and claim["confidence"] == "None"


def test_unsupported_gap_disables_experiment_prototype_and_gap_flag():
    from agents.idea_agent import infer_experiment_direction, infer_prototype_scope, validate_idea_payload

    sections = {"Gap": "No defensible unresolved research gap can be established from the current evidence alone."}
    assert infer_experiment_direction("ESG", sections, "x").startswith("Not designed")
    assert infer_prototype_scope("ESG", sections, "x").startswith("Not scoped")
    v = validate_idea_payload("A real problem statement here.", sections["Gap"], "If x then y", "compare baseline",
                              "Unassessed", {"source_urls": ["u"]})
    assert v["checks"]["gap_present"] is False and v["ready_for_external_review"] is False


def test_proposal_gate_rejects_ideas_without_a_problem():
    from agents.proposal_agent import _proposal_gate

    record = {"problem": "No explicit problem statement found.",
              "validation": {"novelty_status": "external_search_completed", "novelty_search": {}}}
    ok, info = _proposal_gate(record, _ctx())
    assert ok is False and "no source-grounded problem" in info["reason"]
