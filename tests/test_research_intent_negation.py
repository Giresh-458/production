"""Research-intent detection must not count negated funding/R&D sentences as evidence."""
import pytest

from agents.funding_agent import detect_research_intent
from core.problem_extraction import _is_objective_or_task_statement


@pytest.mark.parametrize("sentence", [
    # Verbatim from live topic HORIZON-HLTH-2027-01-ENVHLTH-MISSCLIMA-03.
    "This topic does not provide direct funding to developers, industry or research organisations "
    "to perform Research and Development (R&D).",
    "The programme will not fund new research grants for academic research projects this year.",
    "This call doesn't offer any funding for research proposals.",
])
def test_negated_research_funding_sentences_are_not_evidence(sentence):
    intent, evidence = detect_research_intent(sentence)
    assert intent == "non_research"
    assert evidence == []


def test_negation_in_one_sentence_does_not_hide_positive_evidence_elsewhere():
    text = (
        "This topic does not provide direct funding to developers to perform R&D.\n"
        "Research grants will support studies of verifiable emissions data for carbon markets."
    )
    intent, evidence = detect_research_intent(text)
    assert intent == "research"
    assert evidence == ["Research grants will support studies of verifiable emissions data for carbon markets."]


def test_positive_research_grant_sentence_is_still_detected():
    intent, evidence = detect_research_intent(
        "The grant supports research projects on zero-knowledge proofs for connected vehicles."
    )
    assert intent == "research" and evidence


@pytest.mark.parametrize("sentence", [
    # Verbatim live regression from HORIZON-NATURE-2027-02-03.
    "The network should integrate coordinators of living labs projects funded under topic HORIZON-NATURE-2027-02-02.",
    "The consortium must include at least one public authority responsible for the pilot sites.",
    "Selected projects are expected to share data with the European Open Science Cloud.",
])
def test_requirements_on_funded_networks_are_tasks_not_problems(sentence):
    assert _is_objective_or_task_statement(sentence)


@pytest.mark.parametrize("sentence", [
    "Current carbon-credit MRV methods lack verifiable emissions data, which limits trust in carbon markets.",
    "Networks of sensors in rural areas suffer from unreliable connectivity and data loss.",
])
def test_genuine_problem_statements_are_not_flagged_as_tasks(sentence):
    assert not _is_objective_or_task_statement(sentence)


def test_conjunction_led_list_fragment_is_not_a_funding_problem_candidate():
    from core.problem_extraction import _funding_candidate_segments

    # Verbatim live text from HORIZON-NEB-2026-01-BUSINESS-03.
    paragraph = (
        "Factors can include, for example, building or infrastructure condition, location, and "
        "spatial or structural limitations; zoning regulations; legal barriers; lack of data; or lack "
        "of resources, funds, or incentives for property owners and public authorities to renovate spaces."
    )
    segments = _funding_candidate_segments(paragraph)
    assert not any(segment.lower().startswith("or ") for segment in segments)
    assert segments  # the self-contained leading clause is still evaluated
