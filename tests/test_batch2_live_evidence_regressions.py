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
        "evidence_snippets": [
            "An AI-driven digital platform coordinates energy system operators and grid flexibility."
        ],
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
    assert any("Paris Agreement" in entity for entity in entities)


def test_recursive_fetch_preserves_heading_structure_over_trafilatura_fallback(monkeypatch):
    import trafilatura
    from agents import hackathon_agent
    from core.recursive_collection import _fetch_recursive_page

    content = "## Events\\n#### Starknet Reignite Hackathon\\n" + ("event detail text " * 40)
    monkeypatch.setattr(
        trafilatura, "bare_extraction",
        lambda *_args, **_kwargs: SimpleNamespace(text="fallback " * 300, title="Fallback title"),
    )
    title, extracted, html = _fetch_recursive_page(
        "https://example.org/events", lambda _url: "<html></html>",
        lambda _html: ("Events", content),
    )
    assert title == "Events"
    assert extracted == content
    assert "#### Starknet Reignite Hackathon" in extracted
    assert html == "<html></html>"

def test_energy_call_rejects_defillama_interview_as_unrelated_data_evidence():
    ctx = _context("AI-driven digital platforms for a more affordable and secure European energy system")
    record = {
        "agent_name": "data_availability",
        "title": "DeFiLlama",
        "problem_statement": (
            "Interview — DeFiLlama Research. The article discusses stablecoin revenue, "
            "digital assets, a financial platform, and AI-agent security."
        ),
        "context_summary": "A DeFi interview and market commentary about stablecoins and tokenized assets.",
        "evidence_snippets": ["The interview discusses digital assets and stablecoin market adoption."],
    }
    result = relevance(record, ctx)
    assert result["passed"] is False
    assert "energy-system/grid evidence" in result["reason"]


def test_energy_call_rejects_world_bank_topic_navigation_as_regulation_evidence():
    ctx = _context("AI-driven digital platforms for a more affordable and secure European energy system")
    record = {
        "agent_name": "regulation",
        "title": "Development Topics",
        "problem_statement": (
            "Browse All Topics: Climate Change, Competitiveness, Digital and AI, "
            "Energy, Environment, and Sustainable Infrastructure."
        ),
        "context_summary": "A general World Bank development topics landing page.",
        "evidence_snippets": ["Browse all topics: Digital and AI; Energy; Environment."],
    }
    result = relevance(record, ctx)
    assert result["passed"] is False
    assert "energy-system/grid evidence" in result["reason"]


def test_energy_system_dataset_evidence_can_pass_without_repeating_ai_platform_title():
    ctx = _context("AI-driven digital platforms for a more affordable and secure European energy system")
    record = {
        "agent_name": "data_availability",
        "title": "Eurostat Electricity Supply, Transformation and Consumption Dataset",
        "problem_statement": (
            "Monthly quantities of electricity in the energy balance cover cross-border trade, "
            "transformation input for generation and pumped storage, distribution losses, and "
            "supply to the internal market. Online data code: nrg_cb_em."
        ),
        "context_summary": "The dataset provides monthly electricity supply and consumption data.",
        "evidence_snippets": [
            "Electricity supply, transformation and consumption - monthly data."
        ],
    }
    result = relevance(record, ctx)
    assert result["passed"] is True


def test_data_availability_keeps_funding_driven_collection_unscoped(monkeypatch, tmp_path):
    source = data_agent.DataSource(
        name="Configured RWA Source", type="dataset",
        url="https://example.org/rwa",
        focus="stablecoin supply, tokenized assets, carbon registry and RWA metrics",
    )
    body = (
        "This page describes stablecoin supply, tokenized real-world assets, carbon "
        "registry records, and digital asset market information in detail."
    )
    page = SimpleNamespace(title="RWA market overview", text=body, url=source.url)
    path = tmp_path / "data_sources.yaml"
    path.write_text("data_sources: {}\n", encoding="utf-8")
    seen = {}
    token = data_agent.current_funding_queries.set(["AI-driven digital platforms energy system"])
    try:
        monkeypatch.setattr(data_agent, "load_sources", lambda _path: [source])
        monkeypatch.setattr("core.source_registry.extract_yaml_limits", lambda _data: {})
        monkeypatch.setattr(data_agent, "collect_structured_source", lambda *_args: None)
        monkeypatch.setattr(data_agent, "collect_source", lambda *_args, **_kwargs: SimpleNamespace(pages=[page]))
        monkeypatch.setattr(
            data_agent, "is_relevant",
            lambda _text, area_hint: seen.setdefault("area_hint", area_hint) is None,
        )
        assert data_agent.build_auto_documents(path, area=None) == []
    finally:
        data_agent.current_funding_queries.reset(token)
    assert seen["area_hint"] == "Unscoped"


def test_data_availability_quality_gate_rejects_research_interview_as_dataset():
    from core.quality import evaluate_collection_quality

    evidence = {
        "key_paragraphs": [
            "Interview — DeFiLlama Research. A conversation about stablecoin revenue and DeFi founders."
        ],
        "evidence_snippets": [
            "The interview discusses tokenized assets, financial platforms and market adoption."
        ],
        "excerpt_windows": [],
        "section_headings": [],
        "bullet_lists": [],
    }
    result = evaluate_collection_quality(
        agent_name="data_availability",
        title="DeFiLlama",
        source="https://defillama.com/",
        research_area="RWA",
        raw_content=("Interview about stablecoin revenue and digital assets. " * 20),
        evidence_bundle=evidence,
        area_scores={"RWA": 8},
    )
    assert result["passed"] is False
    assert "data-availability evidence" in result["rejection_reason"]


def test_regulation_quality_gate_rejects_generic_world_bank_topics_page():
    from core.quality import evaluate_collection_quality

    evidence = {
        "key_paragraphs": [
            "Browse All Topics: Climate Change, Digital and AI, Energy, Environment, and Development Topics."
        ],
        "evidence_snippets": [
            "The World Bank addresses complex development challenges through knowledge and innovation."
        ],
        "excerpt_windows": [],
        "section_headings": [],
        "bullet_lists": [],
    }
    result = evaluate_collection_quality(
        agent_name="regulation",
        title="Development Topics",
        source="https://www.worldbank.org/en/topic/climatechange/brief/carbon-markets",
        research_area="ESG",
        raw_content=("World Bank development topics and carbon markets. " * 30),
        evidence_bundle=evidence,
        area_scores={"ESG": 8},
    )
    assert result["passed"] is False
    assert "legal/standards instrument" in result["rejection_reason"]


def test_batch1_energy_ai_call_is_not_primary_esg_and_keeps_energy_themes(monkeypatch):
    from agents import funding_agent

    title = (
        "Large-Scale piloting of next-generation AI-driven digital platforms "
        "for a more affordable, secure and sustainable European energy system (IA) - Apply AI"
    )
    content = (
        "This topic supports a large-scale cross-border pilot demonstrating AI-driven "
        "digital platforms for the European energy system, optimizing electricity-system "
        "operation and flexibility management. Expected outcomes include renewable energy "
        "integration, energy storage, grid flexibility, and quantifiable reductions in "
        "greenhouse gas emissions and energy costs. Participation is limited to legal "
        "entities established in EU Member States and associated countries."
    )
    document = funding_agent.FundingDocument(
        organization="European Commission",
        title=title,
        source="https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/HORIZON-BRIDGING-2027-01-12",
        source_type="Official Funding Portal",
        content=content,
        year=2026,
    )
    monkeypatch.setattr(funding_agent, "extract_funding_problem_intelligence", lambda *_args, **_kwargs: {})
    result = funding_agent.analyze_document(document)
    assert result.focus_area == "General"
    assert "AI-Driven Digital Platforms" in result.research_context["technology_themes"]
    assert "Energy Systems" in result.research_context["technology_themes"]
    assert not result.research_context["inferred_challenges"]


def test_batch1_strategic_autonomy_eligibility_rationale_is_not_a_technical_problem():
    from core.problem_extraction import _is_administrative_or_financial_rule

    passage = (
        "In order to safeguard the Union's strategic assets and avoid technological "
        "dependency on a non-EU source, the EU must carefully assess strategic weaknesses, "
        "vulnerabilities and high-risk dependencies which put at risk the attainment of its ambitions."
    )
    assert _is_administrative_or_financial_rule(passage)

def test_funding_problem_extractor_does_not_select_eligibility_rationale():
    from core.problem_extraction import extract_problem_intelligence

    passage = (
        "In order to safeguard the Union's strategic assets and avoid technological "
        "dependency on a non-EU source, the EU must carefully assess strategic weaknesses, "
        "vulnerabilities and high-risk dependencies which put at risk the attainment of its ambitions."
    )
    result = extract_problem_intelligence(
        passage,
        "AI-driven digital platforms for the European energy system",
        {},
        allow_llm_refinement=False,
        research_area="General",
        agent_name="funding",
    )
    assert result["selected_problem"] is None

def test_eurostat_electricity_dataset_passes_data_availability_quality_gate():
    from core.quality import evaluate_collection_quality

    paragraph = (
        "Electricity supply, transformation and consumption - monthly data. "
        "Monthly quantities of electricity cover cross-border trade, transformation "
        "input for generation and pumped storage, distribution losses, and supply to "
        "the internal market. Online data code nrg_cb_em."
    )
    evidence = {
        "key_paragraphs": [paragraph, "Dataset information: monthly electricity data."],
        "evidence_snippets": [
            paragraph,
            "Data coverage: 2008-01 to 2026-08.",
            "Time frequency: monthly; unit of measure: gigawatt-hour.",
        ],
        "excerpt_windows": [paragraph, "Monthly quantities of electricity in the energy balance."],
        "section_headings": ["Dataset information", "Electricity supply"],
        "bullet_lists": [],
    }
    result = evaluate_collection_quality(
        agent_name="data_availability",
        title="Eurostat Electricity Supply, Transformation and Consumption Dataset",
        source="https://ec.europa.eu/eurostat/databrowser/view/nrg_cb_em/default/table?lang=en",
        research_area="Unscoped",
        raw_content=(paragraph + " ") * 12,
        evidence_bundle=evidence,
        area_scores={},
    )
    assert result["passed"] is True

def test_data_availability_preserves_dataset_metadata_from_extraction(monkeypatch):
    import json

    payload = {
        "area": "Unscoped",
        "what_it_provides": "Monthly electricity supply and consumption data.",
        "usable_for": "Electricity demand analysis and forecasting.",
        "access_type": "API",
        "evaluation_possibility": "Evaluate forecasting and system-operation models.",
        "limitations": "Monthly rather than real-time observations.",
        "prototype_use": "Use as historical training and validation data.",
        "problem_signature": "Unscoped | eurostat | electricity forecasting",
        "keywords": ["electricity", "generation", "consumption"],
        "confidence": "High",
        "dataset_scope": "EU electricity supply and consumption",
        "coverage_period": "2008-01 to 2026-08",
        "license_or_access_terms": "Free public Eurostat API",
        "benchmark_metrics": "Monthly electricity quantities in GWh",
        "benchmark_gap": "No high-frequency real-time grid telemetry",
        "reproducibility": "High",
        "evidence_type": "API Documentation",
    }
    monkeypatch.setattr(data_agent, "llm_generate", lambda _prompt: json.dumps(payload))
    document = data_agent.DataDocument(
        source_name="Eurostat Electricity Supply Dataset",
        source_type="Dataset",
        evidence_type="API Documentation",
        area_hint="Unscoped",
        focus="Monthly electricity supply and consumption",
        url="https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/nrg_cb_em?lang=EN",
        content="Monthly electricity supply and consumption dataset.",
        source_mode="Automatic",
    )
    result = data_agent.analyze_document(document)
    assert result.area == "Unscoped"
    assert result.access_type == "API"
    assert result.confidence == "High"
    assert result.dataset_scope == "EU electricity supply and consumption"
    assert result.coverage_period == "2008-01 to 2026-08"
    assert result.license_or_access_terms == "Free public Eurostat API"
    assert result.benchmark_metrics == "Monthly electricity quantities in GWh"
    assert result.benchmark_gap == "No high-frequency real-time grid telemetry"
    assert result.reproducibility == "High"

def test_eu_electricity_regulation_passes_regulation_quality_gate():
    from core.quality import evaluate_collection_quality

    passage = (
        "Regulation (EU) 2019/943 on the internal market for electricity establishes "
        "rules for cross-border exchanges, system operation, balancing and transparency. "
        "Article 3 sets principles for the electricity market."
    )
    evidence = {
        "key_paragraphs": [passage],
        "evidence_snippets": [
            passage,
            "Member States shall ensure the application of the electricity market rules."
        ],
        "excerpt_windows": [passage],
        "section_headings": ["Article 3 - Principles"],
        "bullet_lists": [],
    }
    result = evaluate_collection_quality(
        agent_name="regulation",
        title="Regulation (EU) 2019/943 on the internal market for electricity",
        source="https://eur-lex.europa.eu/eli/reg/2019/943/oj",
        research_area="Unscoped",
        raw_content=(passage + " ") * 10,
        evidence_bundle=evidence,
        area_scores={},
    )
    assert result["passed"] is True


def test_eu_electricity_regulation_passes_energy_mission_gate():
    ctx = _context("AI-driven digital platforms for a more affordable and secure European energy system")
    passage = (
        "Regulation (EU) 2019/943 sets rules for the internal electricity market, "
        "cross-border exchanges, electricity system operation and balancing."
    )
    record = {
        "agent_name": "regulation",
        "title": "Regulation (EU) 2019/943",
        "problem_statement": "Rules for the internal electricity market and system operation.",
        "context_summary": "The instrument covers cross-border electricity exchanges.",
        "evidence_snippets": [passage],
    }
    result = relevance(record, ctx)
    assert result["passed"] is True

def test_unscoped_evidence_bundle_selects_energy_dataset_passages():
    from core.evidence import build_evidence_bundle

    raw = (
        '{\n  "class": "dataset",\n'
        '  "label": "Electricity supply, transformation and consumption - monthly data",\n'
        '  "source": "Eurostat",\n'
        '  "dimension": {"time": {"label": "Time"}, "geo": {"label": "Geopolitical entity"}},\n'
        '  "value": {"0": 12.5, "1": 13.1}\n}'
    )
    bundle = build_evidence_bundle(
        raw_content=raw,
        title="Eurostat nrg_cb_em",
        research_area="Unscoped",
        metadata={"source_url": "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/nrg_cb_em"},
    )
    assert bundle["evidence_snippets"]
    assert "electricity supply" in bundle["evidence_snippets"][0].casefold()

