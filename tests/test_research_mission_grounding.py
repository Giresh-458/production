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
    funding_priorities=(),
    target_outcomes=(),
    selected_domains=(),
    domain_relevance=(),
    domain="General",
):
    rc = FundingResearchContext(
        funding_call_id="CALL-TCUP",
        domain=domain,
        technology_themes=tuple(themes),
        funding_priorities=tuple(funding_priorities),
        target_outcomes=tuple(target_outcomes),
        investigation_questions=tuple(questions),
        inferred_challenges=tuple(challenges),
        evidence_refs=(),
        inference_provenance=tuple(provenance),
        confidence=confidence,
        research_intent="research",
        research_intent_evidence=("NSF TCUP synopsis",),
        selected_domains=tuple(selected_domains),
        domain_relevance=tuple(domain_relevance),
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
        "context_summary": "This paper examines the geopolitical consequences of military cyber operations and regional conflict escalation.",
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
        "context_summary": "Its evaluation concerns geopolitical strategy, military decision-making, and regional conflict.",
        "keywords": ["stem", "tcup", "nsf", "validation", "deployment", "adoption"],
    }
    result = relevance(record, ctx)
    assert result["passed"] is False


def test_sponsor_identifiers_are_not_substantive_research_anchors():
    ctx = build_context(
        themes=("tcup", "nsf"),
        confidence="High",
        provenance=("OBSERVED",),
        questions=(),
        challenges=(),
    )
    record = {
        "title": "NSF TCUP program administration announcement",
        "problem_statement": "This page describes grant administration and application processing.",
        "context_summary": "The content concerns administrative procedures, not a substantive research topic.",
    }
    result = relevance(record, ctx)
    assert result["passed"] is False


def test_weak_secondary_domain_and_market_adoption_do_not_admit_depin_evidence():
    ctx = build_context(
        themes=("ESG", "Carbon and MRV"),
        confidence="High",
        provenance=("OBSERVED",),
        questions=(),
        challenges=(),
        funding_priorities=("Deployment", "Market adoption"),
        target_outcomes=("Market adoption",),
        selected_domains=("ESG", "DePIN"),
        domain_relevance=(
            {
                "domain": "ESG",
                "score": 0.7143,
                "raw_score": 6.0,
                "matched_terms": ["esg", "sustainability reporting", "greenhouse gas emissions"],
            },
            {
                "domain": "DePIN",
                "score": 0.2857,
                "raw_score": 2.4,
                "matched_terms": ["iot"],
            },
        ),
    )
    record = {
        "title": "Are DePIN Tokens AI-Infrastructure Assets? Market Evidence",
        "problem_statement": (
            "Decentralized physical infrastructure networks (DePINs) are "
            "presented as investable exposure to computing, connectivity, "
            "and energy infrastructure."
        ),
        "context_summary": (
            "The studied DePIN tokens behave predominantly as crypto-risk assets."
        ),
    }

    result = relevance(record, ctx)

    assert result["passed"] is False
    assert "depin" not in result["matched_terms"]
    assert "market" not in result["matched_terms"]
    assert result["matched_phrases"] == []


def test_verified_esg_domain_terms_allow_substantive_sustainability_evidence():
    ctx = build_context(
        themes=("ESG", "Carbon and MRV"),
        confidence="High",
        provenance=("OBSERVED",),
        questions=(),
        challenges=(),
        selected_domains=("ESG", "DePIN"),
        domain_relevance=(
            {
                "domain": "ESG",
                "score": 0.7143,
                "raw_score": 6.0,
                "matched_terms": ["sustainability reporting", "greenhouse gas emissions"],
            },
            {
                "domain": "DePIN",
                "score": 0.2857,
                "raw_score": 2.4,
                "matched_terms": ["iot"],
            },
        ),
    )
    record = {
        "title": "Corporate sustainability reporting and emissions assurance",
        "problem_statement": (
            "The report describes ESG disclosure controls, sustainability reporting, "
            "greenhouse gas emissions data quality, and independent assurance."
        ),
        "context_summary": (
            "It identifies gaps in traceable carbon markets and greenhouse gas emissions reporting."
        ),
    }

    result = relevance(record, ctx)

    assert result["passed"] is True
    assert {"esg", "sustainability", "greenhouse", "gas", "emissions"}.issubset(set(result["matched_terms"]))


def test_strongly_supported_selected_secondary_domain_remains_eligible():
    ctx = build_context(
        themes=("decentralized physical infrastructure", "compute network"),
        confidence="High",
        provenance=("OBSERVED",),
        questions=(),
        challenges=(),
        selected_domains=("DePIN",),
        domain_relevance=(
            {
                "domain": "DePIN",
                "score": 0.8,
                "raw_score": 3.0,
                "matched_terms": ["depin", "compute network"],
            },
        ),
    )
    record = {
        "title": "DePIN research on decentralized physical infrastructure",
        "problem_statement": "The study evaluates decentralized physical infrastructure and compute network design.",
        "context_summary": "The paper measures scaling constraints in DePIN networks.",
    }

    result = relevance(record, ctx)

    assert result["passed"] is True
    assert "depin" in result["matched_terms"]


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
                content="This article discusses military cyber operations, geopolitical conflict, and strategic decision-making.",
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


def test_one_esg_concept_group_is_not_enough_to_support_a_secondary_domain():
    ctx = build_context(
        themes=("ESG",),
        confidence="High",
        provenance=("OBSERVED",),
        questions=(),
        challenges=(),
        selected_domains=("ESG", "DePIN"),
        domain_relevance=(
            {
                "domain": "ESG",
                "score": 0.72,
                "raw_score": 6.0,
                "matched_terms": ["sustainability reporting", "sustainability disclosure"],
            },
            {
                "domain": "DePIN",
                "score": 0.28,
                "raw_score": 2.4,
                "matched_terms": ["iot"],
            },
        ),
    )
    record = {
        "title": "Sustainability reporting controls",
        "problem_statement": "The report describes sustainability reporting controls and disclosure assurance.",
        "context_summary": "The report identifies gaps in sustainability reporting.",
    }

    result = relevance(record, ctx)

    from core.mission_gate import _supported_domain_evidence
    supported_domains = _supported_domain_evidence(ctx.research_context)
    assert result["passed"] is False
    assert supported_domains is not None
    assert "ESG" not in supported_domains


def test_primary_esg_call_can_use_one_focused_esg_anchor_group():
    ctx = build_context(
        themes=("sustainability reporting",),
        confidence="High",
        provenance=("OBSERVED",),
        questions=(),
        challenges=(),
        selected_domains=("ESG",),
        domain_relevance=(
            {
                "domain": "ESG",
                "score": 0.9,
                "raw_score": 6.0,
                "matched_terms": ["sustainability reporting"],
            },
        ),
        domain="ESG",
    )

    from core.mission_gate import _supported_domain_evidence

    supported = _supported_domain_evidence(ctx.research_context)
    assert supported is not None
    assert "ESG" in supported
