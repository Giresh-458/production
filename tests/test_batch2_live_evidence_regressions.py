from types import SimpleNamespace

import pytest

from agents import data_availability_agent as data_agent
from core.funding_selection import FundingCallContext
from core.mission_gate import relevance
from core.normalization import _normalized_actor_and_focus, _normalized_record_dedupe_key
from core.research_schemas import FundingResearchContext


def _context(call_title="Tribal Colleges and Universities Program (TCUP)"):
    research = FundingResearchContext(
        funding_call_id="CALL-TEST", domain="ESG",
        technology_themes=("ESG", "Carbon and MRV"),
        funding_priorities=("Deployment", "Market adoption"),
        target_outcomes=("Deployment",), investigation_questions=(), inferred_challenges=(),
        evidence_refs=("https://example.org/call",),
        inference_provenance=("source_grounded_problem_extraction",),
        confidence="Medium", research_intent="research",
        research_intent_evidence=("The call describes applied research pilots.",),
        selected_domains=("ESG",),
        domain_relevance=({"domain": "ESG", "score": 1.0, "raw_score": 6.0,
                           "matched_terms": ["sustainability", "emissions"]},),
    )
    return FundingCallContext(
        funding_call_id="CALL-TEST", funding_body="Test Funding Body",
        program_name=call_title, call_id="CALL-TEST", call_title=call_title,
        status="OPEN_CALL", opening_date=None, deadline=None, funding_amount=None,
        currency=None, duration=None, eligible_costs=None,
        eligibility="Eligible research organizations.", geography=None, trl=None,
        research_priorities=[], required_partners=None, deliverables=None,
        evaluation_criteria=None, application_url="https://example.org/call",
        source_url="https://example.org/call", source_name="Test Funding Body",
        publication_date=None, last_updated=None, research_area="ESG",
        selection_mode="test", selection_score=1.0, selection_reasons=[],
        research_context=research,
    )


def test_data_availability_empty_crawl_does_not_turn_yaml_focus_into_evidence(monkeypatch, tmp_path):
    source = data_agent.DataSource(
        name="Configured Test Dataset", type="dataset",
        url="https://example.org/dataset",
        focus="a deliberately long configured focus phrase about stablecoin benchmark coverage and reproducibility",
    )
    path = tmp_path / "data_sources.yaml"
    path.write_text("data_sources: {}\n", encoding="utf-8")
    monkeypatch.setattr(data_agent, "load_sources", lambda _path: [source])
    monkeypatch.setattr("core.source_registry.extract_yaml_limits", lambda _data: {})
    monkeypatch.setattr(data_agent, "collect_structured_source", lambda *_args: None)
    monkeypatch.setattr(data_agent, "collect_source", lambda *_args, **_kwargs: SimpleNamespace(pages=[]))
    monkeypatch.setattr(data_agent, "is_relevant", lambda *_args: True)
    assert data_agent.build_auto_documents(path, area="DePIN") == []


def test_data_availability_uses_extracted_page_text_not_configured_focus(monkeypatch, tmp_path):
    source = data_agent.DataSource(
        name="Configured Test Dataset", type="dataset",
        url="https://example.org/dataset",
        focus="CONFIGURATION-ONLY-FOCUS-MUST-NOT-ENTER-EVIDENCE",
    )
    body = (
        "This dataset provides decentralized physical infrastructure telemetry, "
        "including network availability, device activity, and reproducible evaluation "
        "measurements for independent benchmark studies."
    )
    page = SimpleNamespace(title="Dataset documentation", text=body, url=source.url)
    path = tmp_path / "data_sources.yaml"
    path.write_text("data_sources: {}\n", encoding="utf-8")
    monkeypatch.setattr(data_agent, "load_sources", lambda _path: [source])
    monkeypatch.setattr("core.source_registry.extract_yaml_limits", lambda _data: {})
    monkeypatch.setattr(data_agent, "collect_structured_source", lambda *_args: None)
    monkeypatch.setattr(data_agent, "collect_source", lambda *_args, **_kwargs: SimpleNamespace(pages=[page]))
    monkeypatch.setattr(data_agent, "is_relevant", lambda *_args: True)
    documents = data_agent.build_auto_documents(path, area="DePIN")
    assert len(documents) == 1
    assert body in documents[0].content
    assert "CONFIGURATION-ONLY-FOCUS-MUST-NOT-ENTER-EVIDENCE" not in documents[0].content


def test_manual_data_availability_focus_cannot_make_unrelated_text_relevant(monkeypatch):
    monkeypatch.setattr(data_agent, "funding_context_relevance", lambda _text: False)
    with pytest.raises(data_agent.DataAvailabilityAgentError):
        data_agent.build_manual_text_document(
            text="This page describes a university admissions office and campus parking.",
            source_name="Unrelated page", source_type="Manual",
            evidence_type="Description", area=None, focus="stablecoin benchmark dataset",
        )


def test_selected_call_title_anchors_reject_broad_carbon_false_positive():
    ctx = _context("AI-driven digital platforms for an affordable and secure European energy system")
    record = {
        "agent_name": "literature",
        "title": "Governance of marine carbon dioxide removal",
        "problem_statement": "Marine carbon dioxide removal may counterbalance residual emissions.",
        "context_summary": "The study discusses sustainable marine carbon removal and ocean governance.",
    }
    result = relevance(record, ctx)
    assert result["passed"] is False
    assert result["matched_title_anchors"] == []


def test_selected_call_title_anchors_accept_specific_energy_platform_evidence():
    ctx = _context("AI-driven digital platforms for an affordable and secure European energy system")
    record = {
        "agent_name": "company",
        "title": "AI platform for energy-system flexibility",
        "problem_statement": "An AI-driven digital platform coordinates energy system operators and grid flexibility.",
        "context_summary": "The platform evaluates digital control of energy assets and measurable grid response.",
    }
    result = relevance(record, ctx)
    assert result["passed"] is True
    assert len(result["matched_title_anchors"]) >= 2


def test_failed_soft_quality_gate_is_review_only_not_batch3_evidence():
    result = relevance(
        {
            "title": "A highly relevant ESG emissions benchmark",
            "problem_statement": "The dataset measures sustainability emissions with reproducible evaluation.",
            "context_summary": "The benchmark supports sustainability and emissions analysis.",
            "machine_metadata": {"quality_gate": {"passed": False, "accepted_with_warnings": True}},
        },
        _context(),
    )
    assert result["passed"] is False
    assert "review only" in result["reason"]


def test_data_availability_metadata_echo_is_not_research_evidence():
    name = "NIST CPS / IoT Testbed"
    focus = "smart grid, SCADA, robotics, autonomous vehicles, IoT, CPS experiments"
    echo = f"{name} {focus}"
    record = {
        "agent_name": "data_availability", "title": name,
        "problem_statement": echo, "context_summary": echo,
        "evidence_snippets": [echo],
        "machine_metadata": {
            "source_hints": {"source_name": name, "focus": focus},
            "quality_gate": {"passed": True},
        },
    }
    result = relevance(record, _context())
    assert result["passed"] is False
    assert "configured source metadata" in result["reason"]


def test_literature_metadata_never_uses_doi_as_actor_or_dict_repr_as_focus():
    actor, focus = _normalized_actor_and_focus(
        {},
        {"doi": "10.1234/example", "authors": ["Ada Lovelace", "Grace Hopper"],
         "venue": "Journal of Reproducible Systems"},
        {"source_hints": {"focus": {"nested": "must not stringify"}}},
        "RWA",
    )
    assert actor == "Ada Lovelace, Grace Hopper"
    assert focus == "RWA"
    assert "10.1234" not in actor
    assert "nested" not in focus


def test_exact_duplicate_literature_content_collapses_across_doi_aliases():
    first = {"agent_name": "literature", "source_id": "doi:10.32920/26052442.v1",
             "content_hash": "same-hash", "evidence_id": "a"}
    second = {"agent_name": "literature", "source_id": "doi:10.32920/23546229",
              "content_hash": "same-hash", "evidence_id": "b"}
    assert _normalized_record_dedupe_key(first) == _normalized_record_dedupe_key(second)


def test_generic_sentence_starters_are_not_named_entities():
    from core.evidence import _named_entities_or_programs
    entities = _named_entities_or_programs(
        "Such methods are evaluated. Meanwhile, the study reports results. Energy Web publishes a technical benchmark.",
        {},
    )
    assert "Such" not in entities
    assert "Meanwhile" not in entities
    assert "Energy Web" in entities


def test_hackathon_agent_rejects_generic_platform_or_directory_pages():
    from agents.hackathon_agent import has_specific_hackathon_details
    assert not has_specific_hackathon_details(
        "Building our decentralized future",
        "ETHGlobal supports developers and hosts hackathons around the world.",
    )
    assert not has_specific_hackathon_details(
        "Contests", "Browse a directory of contests and opportunities for developers."
    )


def test_hackathon_agent_accepts_dated_challenge_with_operational_details():
    from agents.hackathon_agent import has_specific_hackathon_details
    assert has_specific_hackathon_details(
        "Starknet Reignite Hackathon",
        "Registration opens March 2, 2027. Teams submit projects by March 28, 2027. "
        "Tracks, judging criteria, and prizes are published in the challenge brief.",
    )


def test_zero_knowledge_is_a_controlled_privacy_anchor_for_settlement_calls():
    ctx = _context("Privacy-preserving settlement research")
    record = {
        "agent_name": "literature",
        "title": "Zero-knowledge verification throughput benchmark",
        "problem_statement": "Zero knowledge verification latency limits secure settlement throughput.",
        "context_summary": "The benchmark measures zero knowledge proof verification and settlement performance.",
    }
    result = relevance(record, ctx)
    assert result["passed"] is True
    assert {"privacy", "settlement"}.issubset(set(result["matched_title_anchors"]))


def test_energy_only_or_energy_plus_digital_does_not_pass_a_specific_ai_platform_call():
    ctx = _context("AI-driven digital platforms for an affordable and secure European energy system")
    record = {
        "agent_name": "company",
        "title": "Energy Web",
        "problem_statement": "Energy Web builds digital infrastructure for energy markets.",
        "context_summary": "The organization works on energy-sector digital infrastructure and sustainability.",
    }
    result = relevance(record, ctx)
    assert result["passed"] is False
    assert len(result["matched_title_anchors"]) < 3


def test_generic_hackathon_listing_titles_are_not_promoted_as_event_artifacts():
    from agents.hackathon_agent import is_generic_hackathon_listing_title
    assert is_generic_hackathon_listing_title("Starknet Events")
    assert is_generic_hackathon_listing_title("Contests")
    assert not is_generic_hackathon_listing_title("Starknet Reignite Hackathon")


def test_hackathon_manual_text_focus_cannot_make_unrelated_text_relevant(monkeypatch):
    from agents import hackathon_agent
    monkeypatch.setattr(hackathon_agent, "funding_context_relevance", lambda _text: False)
    with pytest.raises(hackathon_agent.HackathonAgentError):
        hackathon_agent.build_manual_text_document(
            text="This page describes a university admissions office and campus parking.",
            challenge_name="Stablecoin Hackathon", source_type="Manual",
            evidence_type="Problem Statement", sponsor_or_organizer="Example",
            area=None, focus="zero-knowledge settlement",
        )


def test_hackathon_page_extraction_keeps_dates_and_registration_details(monkeypatch):
    from agents import hackathon_agent
    body = "Registration closes March 28, 2027. The hackathon includes prizes and tracks."
    from bs4 import BeautifulSoup
    monkeypatch.setattr(hackathon_agent, "load_requests_and_bs4", lambda: (None, BeautifulSoup))
    html = f"<html><head><title>Specific Hackathon</title></head><body><h1>Specific Hackathon</h1><h4>Starknet Reignite Hackathon</h4><p>{body}</p></body></html>"
    title, content = hackathon_agent.extract_page_text(html)
    assert title == "Specific Hackathon"
    assert body in content
    assert "#### Starknet Reignite Hackathon" in content


def test_hackathon_auto_collection_splits_individual_event_pages_and_excludes_listing_metadata(monkeypatch, tmp_path):
    from agents import hackathon_agent

    source = SimpleNamespace(
        name="Configured Organizer", type="Hackathon", url="https://example.org/events",
        focus="CONFIGURED FOCUS MUST NOT APPEAR IN SOURCE EVIDENCE",
    )
    root = SimpleNamespace(
        title="Starknet Events", content="General events calendar.",
        url="https://example.org/events", depth=0,
    )
    event = SimpleNamespace(
        title="Starknet Reignite Hackathon",
        content=(
            "12 - 26 May, 2025. This two-week online hackathon competition invites developers "
            "to build projects. Registration and submission details are provided."
        ),
        url="https://example.org/events/reignite", depth=1,
    )
    path = tmp_path / "hackathon_sources.yaml"
    path.write_text("sources: []\n", encoding="utf-8")
    review = {"review_id": "review-1"}
    monkeypatch.setattr(hackathon_agent, "load_sources", lambda _path: [source])
    monkeypatch.setattr(hackathon_agent, "fetch_html", lambda _url: "<html></html>")
    monkeypatch.setattr(
        hackathon_agent, "collect_recursive_pages_with_review",
        lambda **_kwargs: ([root, event], review),
    )
    monkeypatch.setattr(hackathon_agent, "save_recursive_review", lambda _review: None)
    monkeypatch.setattr(hackathon_agent, "update_recursive_review", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("core.source_registry.extract_yaml_limits", lambda _raw: {})
    monkeypatch.setattr(hackathon_agent, "is_relevant", lambda *_args: True)

    documents = hackathon_agent.build_auto_documents(path, area="DePIN")

    assert len(documents) == 1
    assert documents[0].challenge_name == "Starknet Reignite Hackathon"
    assert documents[0].url == event.url
    assert documents[0].recursive_page_count == 1
    assert "CONFIGURED FOCUS MUST NOT APPEAR" not in documents[0].content
    assert "Configured Organizer" not in documents[0].content


def test_entity_noise_filter_does_not_drop_legitimate_name_starting_with_one():
    from core.evidence import _named_entities_or_programs
    entities = _named_entities_or_programs("One Medical provides clinical services.", {})
    assert "One Medical" in entities


def test_event_directory_split_keeps_only_specific_dated_hackathons():
    from agents.hackathon_agent import extract_hackathon_event_sections
    content = (
        "12 - 26 May, 2025 · online\n"
        "#### Starknet Reignite Hackathon\n"
        "A two-week online hackathon competition for developers.\n"
        "20 February, 2026 · in person\n"
        "#### Liquidity & Liquors: Starknet’s Private Trader Meetup\n"
        "Registration is open. The meetup discusses leaderboards and competitions."
    )
    sections = extract_hackathon_event_sections(content)
    assert len(sections) == 1
    assert sections[0][0] == "Starknet Reignite Hackathon"


def test_named_entity_extraction_drops_source_title_and_sentence_openers():
    from core.evidence import _named_entities_or_programs
    title = "Governance of marine carbon dioxide removal - gaps, challenges and pathways"
    entities = _named_entities_or_programs(
        "Deliberate ocean-based interventions are studied. Deep uncertainty remains. "
        "Regulating and employing a framework can help. The Paris Agreement sets climate goals.",
        {"source_hints": {"title": title}},
    )
    assert title not in entities
    assert "Deliberate" not in entities
    assert "Deep" not in entities
    assert "Regulating" not in entities
    assert "Employing" not in entities
    assert "Paris Agreement" in entities
