from pathlib import Path

from agents import adaptive_research_agent
from agents.adaptive_research_agent import AdaptiveDocument
from core.funding_context import generate_funding_queries
from core.funding_selection import FundingCallContext
from core.mission_gate import relevance
from core.research_schemas import FundingResearchContext


def build_context(
    *,
    themes=("stem", "tcup", "nsf"),
    confidence="Low",
    provenance=("INFERENCE",),
    questions=(
        "What technical barriers limit the deployment of stem, tcup, nsf?",
        "What validation and evaluation gaps limit adoption of stem, tcup, nsf?",
    ),
    challenges=(
        "Investigate barriers affecting the transition of stem, tcup, nsf toward validation, deployment, adoption and commercialization.",
    ),
):
    rc = FundingResearchContext(
        funding_call_id="CALL-TCUP",
        domain="General",
        technology_themes=tuple(themes),
        funding_priorities=(),
        target_outcomes=(),
        investigation_questions=tuple(questions),
        inferred_challenges=tuple(challenges),
        evidence_refs=(),
        inference_provenance=tuple(provenance),
        confidence=confidence,
        research_intent="research",
        research_intent_evidence=("NSF TCUP synopsis",),
        selected_domains=(),
        domain_relevance=(),
    )
    return FundingCallContext(
        funding_call_id="CALL-TCUP",
        funding_body="National Science Foundation",
        program_name="Tribal Colleges and Universities Program (TCUP)",
        call_id="21-595",
        call_title="Tribal Colleges and Universities Program (TCUP)",
        status="OPEN_CALL",
        opening_date="2021-06-24",
        deadline="2026-10-14",
        funding_amount=None,
        currency=None,
        duration=None,
        eligible_costs=None,
        eligibility="Eligible Tribal Colleges and Universities and specified Native-serving institutions",
        geography=None,
        trl=None,
        research_priorities=[],
        required_partners=None,
        deliverables=None,
        evaluation_criteria=None,
        application_url="https://example.org/tcup",
        source_url="https://example.org/tcup",
        source_name="NSF",
        publication_date=None,
        last_updated=None,
        research_area="General",
        selection_mode="test",
        selection_score=0.7,
        selection_reasons=[],
        research_context=rc,
    )


def test_query_metadata_cannot_make_unrelated_source_relevant():
    ctx = build_context(
        themes=("decentralized physical infrastructure", "compute network"),
        confidence="High",
        provenance=("OBSERVED",),
        questions=(),
        challenges=(),
    )
    record = {
        "title": "Russia's cyber campaigns and the Ukraine war",
        "problem_statement": "This source discusses military cyber operations and geopolitical conflict.",
        "context_summary": "It does not discuss decentralized physical infrastructure or compute networks.",
        "source_type": "adaptive_web_research",
        "evidence_type": "adaptive",
        "actor": "https://doi.org/unrelated",
        "keywords": ["decentralized", "physical", "infrastructure", "compute", "network"],
    }
    result = relevance(record, ctx)
    assert result["passed"] is False
    assert result["matched_terms"] == []


def test_generic_inferred_challenges_do_not_admit_unrelated_source():
    ctx = build_context()
    record = {
        "title": "Russia's cyber campaigns and the Ukraine war",
        "problem_statement": "The paper discusses deployment barriers and adoption challenges in cyber warfare.",
        "context_summary": "Its evaluation concerns geopolitical strategy; it has no STEM or TCUP topic coverage.",
        "keywords": ["stem", "tcup", "nsf", "validation", "deployment", "adoption"],
    }
    result = relevance(record, ctx)
    assert result["passed"] is False


def test_low_confidence_queries_do_not_use_generic_inferred_questions():
    ctx = build_context()
    queries = generate_funding_queries(ctx, max_queries=6)
    assert queries
    assert any("Tribal Colleges and Universities Program (TCUP)" in query for query in queries)
    assert not any("What technical barriers limit" in query for query in queries)
    assert not any("What validation and evaluation gaps" in query for query in queries)


def test_adaptive_research_does_not_emit_synthetic_evidence_when_all_sources_fail_gate(monkeypatch, tmp_path: Path):
    ctx = build_context()

    def fake_openalex(query, limit=6):
        return [
            AdaptiveDocument(
                title="Russia's cyber campaigns and the Ukraine war",
                source="https://example.org/cyber-campaigns",
                content="This article discusses military cyber operations and geopolitical conflict; it does not cover STEM education or TCUP institutions.",
                query=query,
                retrieved_at="2026-10-11T00:00:00+00:00",
            )
        ]

    monkeypatch.setattr(adaptive_research_agent, "_openalex", fake_openalex)
    monkeypatch.setattr(adaptive_research_agent, "_search_web", lambda query, limit=4: [])
    output_dir = tmp_path / "adaptive"

    result = adaptive_research_agent.run_agent(
        mode="configured_scan",
        area="Unscoped",
        input_data={"output_dir": str(output_dir), "run_id": "test-run"},
        funding_context=ctx,
    )

    gate = result.get("metadata", {}).get("mission_relevance_gate", {})
    assert gate.get("candidates") > 0
    assert gate.get("accepted") == 0
    assert gate.get("rejected") == gate.get("candidates")
    assert result.get("outputs") == []
    assert not list(output_dir.rglob("*.md"))
    assert any("no synthetic mission artifact" in warning.lower() for warning in result.get("warnings", []))
