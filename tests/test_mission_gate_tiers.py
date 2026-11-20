"""Mission gate: generic vocabulary alone is not relevance (live regression)."""
from dataclasses import replace

from core.mission_gate import relevance
from tests.test_research_mission_grounding import build_context

TITLE = "Living labs co-creating solutions for the restoration of ecosystems addressing implementation bottlenecks"


def _ctx():
    base = build_context(
        themes=("ESG", "Carbon and MRV"), confidence="Medium", provenance=("source_grounded_problem_extraction",),
        questions=(), challenges=("Preventive restoration integrating wildfire prevention and post-fire recovery remains limited.",),
        funding_priorities=("sustainability", "climate adaptation"), selected_domains=("ESG",), domain="ESG",
    )
    rc = replace(base.research_context, evidence_refs=("https://ec.europa.eu/topic",))
    return replace(base, call_title=TITLE, program_name=TITLE, research_area="ESG", research_context=rc)


def test_generic_words_only_are_rejected():
    # Verbatim shape of the live Protocol Labs blog-listing record.
    record = {"title": "Protocol Labs", "problem_statement": "No explicit problem statement found.",
              "context_summary": "The latest from Protocol Labs: climate tooling, sustainability and neurotech updates."}
    result = relevance(record, _ctx())
    assert result["passed"] is False
    assert result["reason"].startswith("only generic vocabulary overlap")


def test_call_specific_phrase_passes_as_call_specific():
    record = {"title": "Co-Creating Sustainability Interventions in Practice",
              "context_summary": "Living labs face collaboration challenges in climate and sustainability interventions."}
    result = relevance(record, _ctx())
    assert result["passed"] is True and result["relevance_tier"] == "call_specific"
    assert "living labs" in result["call_specific_phrases"]


def test_domain_anchor_passes_as_domain_anchor():
    record = {"title": "Corporate sustainability reporting",
              "context_summary": "Climate disclosures under corporate sustainability reporting rules and carbon accounting."}
    result = relevance(record, _ctx())
    assert result["passed"] is True and result["relevance_tier"] == "domain_anchor"
    assert "sustainability reporting" in result["domain_anchor_terms"]


def test_brand_term_alone_is_not_a_domain_anchor():
    record = {"title": "New European Bauhaus neighbourhoods",
              "context_summary": "New European Bauhaus climate and sustainability showcase for neighbourhoods."}
    result = relevance(record, _ctx())
    assert result["passed"] is False
