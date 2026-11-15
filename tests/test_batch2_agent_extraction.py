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


def test_practitioner_role_is_a_string(monkeypatch):
    from agents import practitioner_agent

    parsed = {
        "pain_point": "Verification timeouts",
        "deployment_constraint": "Latency",
        "technical_problem": "Slow proofs",
        "research_opportunity": "Parallel proving",
        "prototype_idea": "Parallel verifier",
        "keywords": ["latency"],
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


def test_expert_identity_fields_are_strings(monkeypatch):
    from agents import expert_agent

    parsed = {
        "expertise_themes": "Cryptography",
        "recent_work": "2026 proof-systems research",
        "key_projects_or_papers": "Proof benchmark",
        "collaboration_relevance": "Relevant",
        "proposal_relevance": "Strong fit",
        "keywords": ["cryptography"],
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

