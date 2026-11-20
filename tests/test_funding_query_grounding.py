"""Batch-2 retrieval queries must identify the specific call, not only its domain."""
from __future__ import annotations

from dataclasses import replace

from core.funding_context import generate_funding_queries
from tests.test_research_mission_grounding import build_context

TITLE = "Living labs co-creating solutions for the restoration of ecosystems addressing implementation bottlenecks"
PROBLEM = (
    "The communication on integrated wildfire risk management highlights the need for more research "
    "on preventive restoration integrating wildfire prevention and post-fire recovery objectives."
)


def _context(*, confidence="Medium", evidence_refs=("https://ec.europa.eu/topic",), challenges=(PROBLEM,), title=TITLE):
    base = build_context(
        themes=("ESG", "Carbon and MRV"),
        confidence=confidence,
        provenance=("source_grounded_problem_extraction",),
        questions=("What challenges or constraints does the call identify for ESG, Carbon and MRV?",),
        challenges=challenges,
        funding_priorities=("Deployment",),
        selected_domains=("ESG",),
        domain="ESG",
    )
    rc = replace(base.research_context, evidence_refs=tuple(evidence_refs))
    return replace(base, call_title=title, program_name=title, research_area="ESG", research_context=rc)


def test_medium_confidence_queries_lead_with_call_title_and_grounded_problem():
    queries = generate_funding_queries(_context(), max_queries=6)
    assert queries[0] == TITLE
    assert queries[1] == PROBLEM
    # Domain-template questions and bare themes are identical for every call in
    # a domain; with call-specific queries available they are not used (observed
    # live: they retrieved ESG-rating/greenwashing papers for a restoration call).
    assert all("What challenges" not in query for query in queries)
    assert queries == [TITLE, PROBLEM]


def test_domain_questions_remain_a_fallback_without_call_identity():
    queries = generate_funding_queries(_context(title="", evidence_refs=()), max_queries=6)
    assert queries and any("ESG" in query for query in queries)


def test_ungrounded_challenge_is_not_used_as_a_query():
    queries = generate_funding_queries(_context(evidence_refs=()), max_queries=6)
    assert queries[0] == TITLE
    assert PROBLEM not in queries


def test_long_problem_is_bounded_at_a_word_boundary():
    long_problem = "Source-grounded restoration problem " + " ".join(["wildfire"] * 80)
    queries = generate_funding_queries(_context(challenges=(long_problem,)), max_queries=6)
    assert len(queries[1]) <= 240 and long_problem.startswith(queries[1])
