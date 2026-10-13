from pathlib import Path


def test_finalizer_is_deterministic_by_default(monkeypatch, tmp_path: Path):
    import core.agent_interface as ai
    observed = []
    original = ai.build_intermediate_artifact_payload

    def wrapped(**kwargs):
        observed.append(kwargs.get("allow_llm_refinement"))
        return original(**kwargs)

    monkeypatch.setattr(ai, "build_intermediate_artifact_payload", wrapped)
    Doc = type("Doc", (), {})
    doc = Doc()
    doc.title = "ESG MRV test"
    doc.url = "https://example.org/mrv"
    doc.content = "Climate MRV systems face interoperability and emissions verification challenges."
    doc.area_hint = "ESG"
    response = ai.finalize_collection_agent_response(
        agent="company", layer="Company", mode="configured_scan", area="ESG",
        documents=[doc], output_dir=tmp_path, started_at=ai.datetime.now(ai.UTC),
    )
    assert response["status"] in {"success", "partial_success"}
    assert observed == [False]


def test_all_collection_agents_default_to_no_llm_refinement():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "agents"
    names = {
        "company_agent.py", "data_availability_agent.py", "expert_agent.py",
        "failure_agent.py", "hackathon_agent.py", "investment_agent.py",
        "lab_agent.py", "opensource_agent.py", "practitioner_agent.py", "regulation_agent.py"
    }
    for name in names:
        text = (root / name).read_text(encoding="utf-8")
        assert 'allow_llm_refinement=bool(payload.get("allow_llm_refinement", False))' in text, name


def test_literature_query_uses_funding_questions_with_blockchain_context_for_esg():
    import agents.literature_agent as lit
    token = lit.current_funding_queries.set(["carbon credit MRV validation", "emissions reporting verification"])
    try:
        q = lit.build_query("ESG")
        assert "carbon credit MRV validation" in q
        assert "emissions reporting verification" in q
        assert 'all:"blockchain"' in q
    finally:
        lit.current_funding_queries.reset(token)


def test_source_router_does_not_accept_contaminated_low_signal_hint(tmp_path: Path):
    import json
    import core.source_registry as sr
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"entries": [{
        "source_id": "regulation:sbti", "agent_name": "regulation",
        "name": "SBTi", "url": "https://sciencebasedtargets.org/",
        "status": "active", "area_hints": ["RWA", "ESG"],
        "source_path": "regulation_sources.esg_reporting.[5]",
    }]}), encoding="utf-8")
    token_force = sr.current_force_source_refresh.set(True)
    token_area = sr.current_source_area.set("RWA")
    try:
        assert sr.apply_refresh_policy("regulation", [{"name":"SBTi","url":"https://sciencebasedtargets.org/"}], registry_path=registry) == []
    finally:
        sr.current_source_area.reset(token_area)
        sr.current_force_source_refresh.reset(token_force)


def test_llm_timeout_is_bounded(monkeypatch):
    import core.llm_provider as lp
    monkeypatch.delenv("RIF_LLM_TIMEOUT", raising=False)
    assert lp._settings()["timeout"] <= 120
