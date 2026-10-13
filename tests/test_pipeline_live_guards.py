from __future__ import annotations

from pathlib import Path


def _sample_doc():
    from dataclasses import make_dataclass
    Doc = make_dataclass("Doc", [
        ("title", str), ("url", str), ("content", str), ("area_hint", str)
    ])
    return Doc(
        title="ESG MRV test",
        url="https://example.org/carbon-mrv",
        content=(
            "Carbon credit MRV systems face interoperability and "
            "validation challenges. Emissions monitoring datasets require reliable "
            "integration and verification before deployment."
        ),
        area_hint="ESG",
    )


def test_collection_artifact_builder_does_not_call_llm_when_disabled(monkeypatch):
    import core.agent_interface as ai

    calls = []
    monkeypatch.setattr(
        ai,
        "extract_problem_intelligence",
        lambda *args, **kwargs: calls.append(kwargs.get("allow_llm_refinement")) or {
            "problem_candidates": [],
            "selected_problem": None,
            "original_candidate": None,
            "problem_type": "unresolved",
            "problem_evidence": [],
            "problem_confidence": "None",
            "intelligence_mode": "deterministic",
        },
    )
    payload, _ = ai.build_intermediate_artifact_payload(
        agent="company",
        layer="Company",
        mode="configured_scan",
        area="ESG",
        title="ESG MRV test",
        source="https://example.org/carbon-mrv",
        content="Carbon credit MRV systems face interoperability and validation challenges.",
        allow_llm_refinement=False,
    )
    assert calls == [False]
    assert payload["shared_header"]["research_area"] == "ESG"


def test_finalize_collection_defaults_to_deterministic(monkeypatch, tmp_path: Path):
    import core.agent_interface as ai

    observed = []
    original = ai.build_intermediate_artifact_payload

    def wrapped(**kwargs):
        observed.append(kwargs.get("allow_llm_refinement"))
        return original(**kwargs)

    monkeypatch.setattr(ai, "build_intermediate_artifact_payload", wrapped)
    response = ai.finalize_collection_agent_response(
        agent="company",
        layer="Company",
        mode="configured_scan",
        area="ESG",
        documents=[_sample_doc()],
        output_dir=tmp_path,
        started_at=ai.datetime.now(ai.UTC),
    )
    assert response["status"] in {"success", "partial_success"}
    assert observed == [False]


def test_normal_clustering_does_not_use_hidden_provenance_llm(monkeypatch):
    from core.intelligence import build_problem_clusters

    calls = []
    import core.intelligence as intelligence
    monkeypatch.setattr(
        intelligence,
        "detect_derivative_relationships",
        lambda entries, **kwargs: calls.append(kwargs.get("llm_resolver")) or {"group_by_record_id": {}}
    )
    records = [
        {
            "record_id": "a",
            "research_area": "ESG",
            "title": "Carbon credit MRV",
            "problem": "Clinical devices need reliable validation.",
            "problem_statement": "Clinical devices need reliable validation.",
            "source_url": "https://example.org/a",
            "file_path": "/a",
            "layer": "Literature",
        },
        {
            "record_id": "b",
            "research_area": "ESG",
            "title": "Emissions reporting reliability",
            "problem": "Clinical monitoring systems need reliable validation.",
            "problem_statement": "Clinical monitoring systems need reliable validation.",
            "source_url": "https://example.org/b",
            "file_path": "/b",
            "layer": "Company",
        },
    ]
    build_problem_clusters(records, threshold=0.35, llm_resolver=None)
    assert calls == [None]
