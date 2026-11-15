from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest


AGENTS = [
    ("lab", "LabAgentError", "name"),
    ("company", "CompanyAgentError", "company_name"),
    ("regulation", "RegulationAgentError", "name"),
    ("opensource", "OpenSourceAgentError", "project_name"),
    ("practitioner", "PractitionerAgentError", "title"),
    ("investment", "InvestmentAgentError", "title"),
    ("failure", "FailureAgentError", "incident_name"),
    ("data_availability", "DataAvailabilityAgentError", "source_name"),
    ("hackathon", "HackathonAgentError", "challenge_name"),
    ("expert", "ExpertAgentError", "title"),
]


@pytest.mark.parametrize(("agent", "error_name", "log_field"), AGENTS)
def test_agent_reports_local_llm_failure_as_typed_agent_error(
    monkeypatch, agent, error_name, log_field
):
    module = importlib.import_module(f"agents.{agent}_agent")
    monkeypatch.setattr(module, "build_analysis_prompt", lambda _document: "test prompt")

    def fail_generation(_prompt):
        raise RuntimeError("Ollama unavailable")

    monkeypatch.setattr(module, "llm_generate", fail_generation)
    document = SimpleNamespace(**{log_field: "Test document"})
    error_type = getattr(module, error_name)

    with pytest.raises(error_type, match="synthetic fallback is disabled"):
        module.analyze_document(document)


@pytest.mark.parametrize(("agent", "error_name", "log_field"), AGENTS)
def test_source_loader_rejects_non_mapping_yaml(tmp_path, agent, error_name, log_field):
    module = importlib.import_module(f"agents.{agent}_agent")
    source_path = tmp_path / f"{agent}.yaml"
    source_path.write_text("- this is a list, not the required mapping\n", encoding="utf-8")

    with pytest.raises(getattr(module, error_name), match="YAML mapping"):
        module.load_sources(source_path)


def test_lab_analysis_fields_are_strings_and_research_area_uses_model_value(monkeypatch):
    from agents import lab_agent

    parsed = {
        "research_area": "RWA",
        "problem": "Scalable verification is unresolved.",
        "active_problems": ["Verification latency"],
        "research_themes": ["Zero-knowledge proofs"],
        "keywords": ["verification"],
        "projects": [],
        "industry_signals": [],
        "problem_signatures": ["verification latency"],
        "why_important": "Latency blocks adoption.",
        "existing_solutions": "Batching",
        "gap": "Limited scale.",
        "idea": "Parallel verification.",
        "feasibility": "Feasible.",
    }
    monkeypatch.setattr(lab_agent, "build_analysis_prompt", lambda _document: "prompt")
    monkeypatch.setattr(lab_agent, "llm_generate", lambda _prompt: json.dumps(parsed))
    document = SimpleNamespace(
        name="Test Lab",
        institution="Test University",
        pages=[SimpleNamespace(content="Research on verification latency.")],
    )

    result = lab_agent.analyze_document(document)

    assert isinstance(result.problem, str)
    assert result.problem == parsed["problem"]
    assert result.research_area == "RWA"


def test_company_product_or_deployment_is_a_string(monkeypatch):
    from agents import company_agent

    parsed = {
        "problem": "Deployment latency",
        "technical_components": ["zero-knowledge proofs"],
        "real_world_constraint": "Latency",
        "research_gap": "Scale",
        "prototype_idea": "Batching",
        "keywords": ["verification"],
        "confidence": "High",
        "funding_alignment": "Strong alignment with RWA settlement research.",
    }
    monkeypatch.setattr(company_agent, "build_analysis_prompt", lambda _document: "prompt")
    monkeypatch.setattr(company_agent, "llm_generate", lambda _prompt: json.dumps(parsed))
    document = SimpleNamespace(
        company_name="Test Company",
        area_hint="RWA",
        source_type="Blockchain-native",
        focus="Tokenized settlement",
        title="Settlement Platform",
        content="Tokenized asset settlement.",
    )

    result = company_agent.analyze_document(document)

    assert isinstance(result.product_or_deployment, str)
    assert result.product_or_deployment == "Tokenized settlement"
    assert result.confidence == "High"
    assert result.funding_alignment == "Strong alignment with RWA settlement research."


def test_practitioner_role_is_a_string(monkeypatch):
    from agents import practitioner_agent

    parsed = {
        "pain_point": "Verification timeouts",
        "deployment_constraint": "Latency",
        "technical_problem": "Slow proofs",
        "research_opportunity": "Parallel proving",
        "prototype_idea": "Parallel verifier",
        "keywords": ["latency"],
        "confidence": "High",
    }
    monkeypatch.setattr(practitioner_agent, "build_analysis_prompt", lambda _document: "prompt")
    monkeypatch.setattr(practitioner_agent, "llm_generate", lambda _prompt: json.dumps(parsed))
    document = SimpleNamespace(
        title="Test Practitioner",
        source_type="Article",
        evidence_type="Practitioner Experience",
        practitioner_role_hint="Engineer",
        area_hint="RWA",
    )

    result = practitioner_agent.analyze_document(document)

    assert isinstance(result.practitioner_role, str)
    assert result.practitioner_role == "Engineer"
    assert result.confidence == "High"


def test_expert_identity_fields_are_strings(monkeypatch):
    from agents import expert_agent

    parsed = {
        "expertise_themes": "Cryptography",
        "recent_work": "2026 proof-systems research",
        "key_projects_or_papers": "Proof benchmark",
        "collaboration_relevance": "Relevant",
        "proposal_relevance": "Strong fit",
        "keywords": ["cryptography"],
        "confidence": "High",
    }
    monkeypatch.setattr(expert_agent, "build_analysis_prompt", lambda _document: "prompt")
    monkeypatch.setattr(expert_agent, "llm_generate", lambda _prompt: json.dumps(parsed))
    document = SimpleNamespace(
        title="Professor Profile",
        source_type="Expert Profile",
        evidence_type="Profile",
        expert_name_hint="Dr Example",
        affiliation_hint="Example University",
        role_hint="Professor",
        area_hint="RWA",
        content="Cryptography researcher.",
        url="https://example.org/profile",
    )

    result = expert_agent.analyze_document(document)

    assert isinstance(result.expert_name, str)
    assert isinstance(result.affiliation, str)
    assert isinstance(result.role, str)
    assert result.expert_name == "Dr Example"
    assert result.affiliation == "Example University"
    assert result.role == "Professor"
    assert result.confidence == "High"

def test_configured_live_source_cannot_bypass_quality_gate(tmp_path):
    from datetime import UTC, datetime
    from core.agent_interface import finalize_collection_agent_response

    weak_document = SimpleNamespace(
        title="Thin configured source",
        url="https://example.org/thin",
        content="lorem ipsum " * 40,
    )
    result = finalize_collection_agent_response(
        agent="company",
        layer="Company",
        mode="configured_scan",
        area="RWA",
        documents=[weak_document],
        output_dir=tmp_path,
        started_at=datetime.now(UTC),
    )

    assert result["status"] == "partial_success"
    assert result["items_saved"] == 0
    assert not list(tmp_path.rglob("*.md"))
    assert any("quality gate" in warning.lower() for warning in result["warnings"])

SOURCE_CONFIGS = [
    ("lab", "labs_sources.yaml"),
    ("company", "company_sources.yaml"),
    ("regulation", "regulation_sources.yaml"),
    ("opensource", "opensource_sources.yaml"),
    ("practitioner", "practitioner_sources.yaml"),
    ("investment", "investment_sources.yaml"),
    ("failure", "failure_sources.yaml"),
    ("data_availability", "data_sources.yaml"),
    ("hackathon", "hackathon_sources.yaml"),
    ("expert", "expert_sources.yaml"),
]


@pytest.mark.parametrize(("agent", "filename"), SOURCE_CONFIGS)
def test_checked_in_source_configuration_loads_and_has_http_urls(monkeypatch, agent, filename):
    from pathlib import Path
    from agents.utils import is_valid_http_url

    module = importlib.import_module(f"agents.{agent}_agent")
    monkeypatch.setattr(module, "apply_refresh_policy", lambda _agent, sources: sources)
    sources = module.load_sources(Path("sources") / filename)

    assert sources, f"{filename} loaded no source entries"
    assert all(is_valid_http_url(source.url) for source in sources)


def test_http_source_url_validator_rejects_unsafe_or_malformed_urls():
    from agents.utils import is_valid_http_url

    assert is_valid_http_url("https://example.org/research")
    assert is_valid_http_url("http://localhost:8080/api")
    assert not is_valid_http_url("ftp://example.org/file")
    assert not is_valid_http_url("javascript:alert(1)")
    assert not is_valid_http_url("https://user:password@example.org/")
    assert not is_valid_http_url("https://example.org/has whitespace")
    assert not is_valid_http_url("https://[::1")


def test_collection_diagnostics_surface_failed_urls_and_skipped_links():
    from agents.utils import append_collection_diagnostics

    warnings = []
    result = SimpleNamespace(
        pages=[],
        failed_urls=[{"url": "https://example.org/private", "reason": "robots_disallow"}],
    )
    review = {
        "skipped_links": [
            {"url": "https://partner.example/apply", "reason": "relevant_link_outside_allowed_domains"}
        ]
    }

    append_collection_diagnostics(
        warnings,
        "Example source",
        result=result,
        review=review,
    )

    assert any("robots_disallow" in warning for warning in warnings)
    assert any("relevant_link_outside_allowed_domains" in warning for warning in warnings)

def test_hackathon_partial_llm_json_uses_safe_document_defaults(monkeypatch):
    from agents import hackathon_agent

    monkeypatch.setattr(hackathon_agent, "build_analysis_prompt", lambda _document: "prompt")
    parsed = {
        "challenge_name": "RWA Builder Challenge",
        "source_type": "Hackathon Platform",
        "area": "RWA",
        "problem_statement": "",
        "sponsor_or_organizer": "Test Organizer",
        "required_technology": "zero-knowledge proofs",
        "expected_output": "prototype demo",
        "industry_research_relevance": "Prototype-oriented ecosystem need",
        "prototype_idea": "Build a verification prototype",
        "funding_collaboration_signal": "Sponsor signal",
        "keywords": [],
        "confidence": "Medium",
    }
    monkeypatch.setattr(hackathon_agent, "llm_generate", lambda _prompt: json.dumps(parsed))
    document = SimpleNamespace(
        challenge_name="RWA Builder Challenge",
        source_type="Hackathon Platform",
        evidence_type="Challenge",
        sponsor_or_organizer="Test Organizer",
        area_hint="RWA",
        content="Build a prototype for tokenized settlement and verification.",
    )

    result = hackathon_agent.analyze_document(document)

    assert result.challenge_name == document.challenge_name
    assert result.area == "RWA"
    assert result.problem_statement == ""
    assert result.confidence == "Medium"
    assert result.corroboration_required is True


def test_regulation_invalid_evidence_weight_is_bounded_and_confidence_is_parsed(monkeypatch):
    from agents import regulation_agent

    parsed = {
        "confidence": "High",
        "evidence_weight": "not-a-number",
        "effective_date": "2026-01-01",
        "supersedes": "Prior regulation",
        "requirement": "Mandatory identity verification",
        "technical_problem": "Verification overhead",
        "research_opportunity": "Privacy-preserving verification",
        "prototype_idea": "A compliant verification prototype",
    }
    monkeypatch.setattr(regulation_agent, "build_analysis_prompt", lambda _document: "prompt")
    monkeypatch.setattr(regulation_agent, "llm_generate", lambda _prompt: json.dumps(parsed))
    document = SimpleNamespace(
        name="Test Regulation",
        issuing_body="Test Authority",
        area_hint="RWA",
        content="Regulation requires compliance and identity verification.",
        source_type="Regulation",
        evidence_type="Binding Rule",
    )

    result = regulation_agent.analyze_document(document)

    assert result.confidence == "High"
    assert result.effective_date == "2026-01-01"
    assert result.supersedes == "Prior regulation"
    assert 0.0 <= result.evidence_weight <= 1.0

def test_failure_analysis_uses_supported_incident_type_and_model_fields(monkeypatch):
    from agents import failure_agent

    parsed = {
        "incident_type": "Bridge Hack",
        "what_failed": "A bridge validator was compromised.",
        "root_cause": "Insufficient validator controls.",
        "impact": "Unauthorized asset movement.",
        "technical_problem": "Validator compromise",
        "research_opportunity": "Threshold verification",
        "prototype_idea": "A monitoring prototype",
        "prevention_mechanism": "Independent validator checks",
        "keywords": ["bridge", "validator"],
        "confidence": "High",
        "incident_date": "2026-02-10",
        "estimated_loss": "$1 million",
        "attack_vector": "Compromised validator keys",
        "affected_system": "Example Bridge",
        "recurrence_signal": "Repeated validator incidents",
        "evidence_strength": "Very High",
    }
    monkeypatch.setattr(failure_agent, "build_analysis_prompt", lambda _document: "prompt")
    monkeypatch.setattr(failure_agent, "llm_generate", lambda _prompt: json.dumps(parsed))
    document = SimpleNamespace(
        incident_name="Example Bridge Incident",
        area_hint="RWA",
        content="A bridge exploit compromised validator keys.",
        source_type="Incident Report",
        evidence_type="Postmortem",
    )

    result = failure_agent.analyze_document(document)

    assert result.incident_type == "Bridge Hack"
    assert result.confidence == "High"
    assert result.incident_date == "2026-02-10"
    assert result.estimated_loss == "$1 million"
    assert result.attack_vector == "Compromised validator keys"
    assert result.affected_system == "Example Bridge"
    assert result.evidence_strength == "Very High"


def test_data_availability_analysis_uses_model_access_and_dataset_metadata(monkeypatch):
    from agents import data_availability_agent

    parsed = {
        "what_it_provides": "Historical settlement events",
        "usable_for": "Protocol evaluation",
        "access_type": "Restricted",
        "evaluation_possibility": "Reproducible benchmark",
        "limitations": "Partial geographic coverage",
        "prototype_use": "Settlement risk analysis",
        "keywords": ["settlement", "benchmark"],
        "confidence": "High",
        "dataset_scope": "Ethereum settlement events",
        "coverage_period": "2023-2026",
        "license_or_access_terms": "Research access",
        "benchmark_metrics": "Latency and failure rate",
        "benchmark_gap": "No labeled recovery events",
        "reproducibility": "High",
    }
    monkeypatch.setattr(data_availability_agent, "build_analysis_prompt", lambda _document: "prompt")
    monkeypatch.setattr(data_availability_agent, "llm_generate", lambda _prompt: json.dumps(parsed))
    document = SimpleNamespace(
        source_name="Settlement Dataset",
        area_hint="RWA",
        source_type="Dataset",
        evidence_type="Dataset Description",
        content="A dataset of historical settlement events.",
    )

    result = data_availability_agent.analyze_document(document)

    assert result.access_type == "Restricted"
    assert result.confidence == "High"
    assert result.dataset_scope == "Ethereum settlement events"
    assert result.coverage_period == "2023-2026"
    assert result.license_or_access_terms == "Research access"
    assert result.benchmark_metrics == "Latency and failure rate"
    assert result.benchmark_gap == "No labeled recovery events"
    assert result.reproducibility == "High"

def test_failure_agent_uses_model_incident_type_and_evidence_metadata(monkeypatch):
    from agents import failure_agent
    from core.schemas import AREA_KEYWORDS

    area = next(iter(AREA_KEYWORDS))
    parsed = {
        "incident_type": "Bridge Hack",
        "what_failed": "Bridge validator compromise",
        "root_cause": "Compromised signing keys",
        "impact": "Unauthorized asset transfer",
        "technical_problem": "Insufficient key isolation",
        "research_opportunity": "Threshold signing with monitoring",
        "prototype_idea": "A threshold-signature bridge monitor",
        "prevention_mechanism": "Independent signer quorum",
        "confidence": "High",
        "incident_date": "2026-01-15",
        "estimated_loss": "Unknown",
        "attack_vector": "Validator key compromise",
        "affected_system": "Example Bridge",
        "recurrence_signal": "Repeated validator compromise",
        "evidence_strength": "High",
        "keywords": ["bridge", "validator"],
    }
    monkeypatch.setattr(failure_agent, "build_analysis_prompt", lambda _document: "prompt")
    monkeypatch.setattr(failure_agent, "llm_generate", lambda _prompt: json.dumps(parsed))
    document = SimpleNamespace(
        incident_name="Example Bridge incident",
        source_type="Security Report",
        evidence_type="Incident Report",
        area_hint=area,
        content="Bridge validator exploit caused unauthorized asset transfer.",
    )

    result = failure_agent.analyze_document(document)

    assert result.incident_type == "Bridge Hack"
    assert result.confidence == "High"
    assert result.incident_date == "2026-01-15"
    assert result.attack_vector == "Validator key compromise"
    assert result.evidence_strength == "High"

def test_saved_artifacts_with_source_failure_warning_return_partial_success(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from core import agent_interface

    document = SimpleNamespace(
        title="Substantive source evidence",
        url="https://example.org/research",
        content="A source-grounded technical research finding with clear provenance.",
    )

    def fake_payload(**kwargs):
        area = kwargs["area"] or "RWA"
        return {
            "shared_header": {
                "title": kwargs["title"],
                "source": kwargs["source"],
                "research_area": area,
            },
            "metadata": {
                "quality_gate": {"passed": True},
                "area_normalization": {"keyword_scores": {area: 1.0}},
            },
            "problem_intelligence": {"selected_problem": "A documented technical limitation."},
        }

    output_path = tmp_path / "saved.md"

    def fake_save(*, payload, output_dir):
        output_path.write_text("# Saved evidence\n", encoding="utf-8")
        return output_path

    monkeypatch.setattr(agent_interface, "build_intermediate_artifact_payload", fake_payload)
    monkeypatch.setattr(agent_interface, "save_intermediate_markdown", fake_save)

    result = agent_interface.finalize_collection_agent_response(
        agent="company",
        layer="Company",
        mode="configured_scan",
        area="RWA",
        documents=[document],
        output_dir=tmp_path,
        started_at=datetime.now(UTC),
        warnings=["Source 'Example' failed one relevant page: HTTP 503."],
    )

    assert result["items_saved"] == 1
    assert result["status"] == "partial_success"
    assert any("HTTP 503" in warning for warning in result["warnings"])


def test_literature_arxiv_provider_failure_is_returned_as_collection_warning(monkeypatch):
    from agents import literature_agent
    import core.http_client as http_client

    warnings = []
    token = literature_agent._literature_collection_warnings.set(warnings)

    def fail_request(*_args, **_kwargs):
        raise RuntimeError("arXiv service unavailable")

    monkeypatch.setattr(http_client, "get_response", fail_request)
    try:
        papers = literature_agent.fetch_arxiv_entries("RWA")
    finally:
        literature_agent._literature_collection_warnings.reset(token)

    assert papers == []
    assert any("arXiv" in warning and "service unavailable" in warning for warning in warnings)


def test_literature_json_provider_failure_is_returned_as_collection_warning(monkeypatch):
    from agents import literature_agent

    import core.http_client as http_client

    warnings = []
    token = literature_agent._literature_collection_warnings.set(warnings)
    monkeypatch.setattr(
        http_client,
        "fetch_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )
    try:
        payload = literature_agent._fetch_provider_json(
            "https://api.example.org/papers",
            "Test Provider",
        )
    finally:
        literature_agent._literature_collection_warnings.reset(token)

    assert payload == {}
    assert any("Test Provider" in warning for warning in warnings)

