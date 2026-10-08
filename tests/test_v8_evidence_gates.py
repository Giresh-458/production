from core.mission_gate import relevance
from core.funding_selection import FundingCallContext
from core.research_schemas import FundingResearchContext
from core.novelty_search import LocalKnowledgeBaseSearcher
from agents.funding_agent import detect_research_intent
from agents.idea_agent import _json_safe
from core.research_schemas import ExistingWork


def ctx():
    return FundingCallContext(
        funding_call_id="x", funding_body="Sponsor", program_name="Research Grant", call_id="x",
        call_title="Secure medical device interoperability", status="OPEN_CALL", opening_date=None, deadline=None,
        funding_amount=None, currency=None, duration=None, eligible_costs=None, eligibility="academic researchers",
        geography="India", trl=None, research_priorities=[], required_partners=None, deliverables=None,
        evaluation_criteria=None, application_url="https://example.org/apply", source_url="https://example.org",
        source_name="Sponsor", publication_date=None, last_updated=None, research_area=None, selection_mode="test",
        selection_score=1.0, selection_reasons=[], research_context=FundingResearchContext(
            funding_call_id="x", domain="General", technology_themes=("medical device interoperability",),
            funding_priorities=("clinical validation",), target_outcomes=(), investigation_questions=("How can interoperability be validated?",),
            inferred_challenges=(), evidence_refs=(), inference_provenance=("OBSERVED",), confidence="High",
            research_intent="research", research_intent_evidence=("research funding",), selected_domains=(), domain_relevance=()
        )
    )


def test_generic_overlap_is_rejected():
    gate = relevance({"title":"Research program", "problem_statement":"deployment security project", "context_summary":"funding application", "source_type":"blog"}, ctx())
    assert gate["passed"] is False


def test_substantive_overlap_passes():
    gate = relevance({"title":"Clinical validation of medical device interoperability", "problem_statement":"interoperability validation remains difficult", "context_summary":"clinical validation", "source_type":"paper"}, ctx())
    assert gate["passed"] is True


def test_research_intent_is_conservative():
    assert detect_research_intent("University program for student groups and public events") [0] == "non_research"
    assert detect_research_intent("Research funding supports a research project investigating interoperability") [0] == "research"


def test_existing_work_is_json_safe():
    payload = {"work": ExistingWork("A", "https://a", ["B"], "RELATED", [])}
    safe = _json_safe(payload)
    assert safe["work"]["title"] == "A"


def test_local_novelty_does_not_claim_novelty():
    result = LocalKnowledgeBaseSearcher().search("example problem")
    assert result["similar_work"] == []
    assert result["search_coverage"] == 0.0
