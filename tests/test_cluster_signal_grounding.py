"""Cluster signal types must not assert a problem that was never extracted."""
import pytest

from core.intelligence import _cluster_signal_type


@pytest.mark.parametrize("layer,expected", [
    ("Literature", "research_problem"),
    ("Practitioner", "practitioner_pain_point"),
    ("Failure", "failure_mode"),
    ("OpenSource", "engineering_gap"),
    ("Company", "market_need"),
    ("Hackathon", "prototype_challenge"),
])
def test_problem_asserting_labels_require_an_extracted_problem(layer, expected):
    with_problem = {"layer": layer, "problem_statement": "Current MRV methods lack verifiable emissions data."}
    assert _cluster_signal_type(with_problem) == expected
    # Live regression: literature record with the collectors' placeholder was
    # labelled research_problem in Batch-3 clustering.
    for placeholder in ("No explicit problem statement found.", "", None):
        assert _cluster_signal_type({"layer": layer, "problem_statement": placeholder}) == "general_signal"


@pytest.mark.parametrize("layer,expected", [
    ("Lab", "research_activity"),
    ("Regulation", "compliance_requirement"),
    ("Funding", "funding_signal"),
    ("Investment", "capital_signal"),
    ("DataAvailability", "evaluation_support"),
])
def test_source_type_labels_are_unchanged_without_a_problem(layer, expected):
    assert _cluster_signal_type({"layer": layer, "problem_statement": "No explicit problem statement found."}) == expected


def test_problem_field_alias_is_honoured():
    assert _cluster_signal_type({"layer": "Literature", "problem": "A real extracted research gap statement."}) == "research_problem"


def test_page_dump_text_does_not_feed_tags_or_clustering():
    """Live regression: a lab's serialized blog listing produced 7 unrelated topic tags."""
    from agents.tagging_agent import build_tags_for_record
    from core.intelligence_quality import evidence_text

    dump = ("Page title: Our Vision: PL Update 2026 Source URL: https://protocol.ai/blog Network About "
            "Join Blog privacy stablecoins settlement identity DePIN prototype scalability governance")
    record = {"title": "Protocol Labs", "problem_statement": "No explicit problem statement found.",
              "context_summary": dump, "evidence_snippets": [dump], "layer": "Lab", "research_area": "DePIN"}
    assert "privacy" not in evidence_text(record)
    assert build_tags_for_record(record)["topic_tags"] == []

    genuine = dict(record, context_summary="Carbon credit registries lack interoperable data, causing double counting.",
                   evidence_snippets=[])
    assert "interoperable" in evidence_text(genuine)
