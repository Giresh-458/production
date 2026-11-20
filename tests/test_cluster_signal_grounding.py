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
