from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from core.funding_selection import FundingCallContext
from core.research_schemas import FundingResearchContext
from scripts.pipeline_tools import run_batch2


def make_context() -> FundingCallContext:
    research = FundingResearchContext(
        funding_call_id="CALL-BATCH2-001",
        domain="DePIN",
        technology_themes=("decentralized infrastructure",),
        funding_priorities=("resilient infrastructure",),
        target_outcomes=("validated prototype",),
        investigation_questions=("How can infrastructure reliability be improved?",),
        inferred_challenges=("Reliability under intermittent connectivity",),
        evidence_refs=("https://example.org/call",),
        inference_provenance=("Official call text",),
        confidence="high",
        research_intent="research",
        research_intent_evidence=("The call explicitly funds applied research.",),
    )
    return FundingCallContext(
        funding_call_id="CALL-BATCH2-001",
        funding_body="Example Funder",
        program_name="Infrastructure Research",
        call_id="CALL-BATCH2-001",
        call_title="Research into resilient decentralized infrastructure",
        status="OPEN_CALL",
        opening_date="2026-01-01",
        deadline="2027-01-01",
        funding_amount="100000",
        currency="EUR",
        duration="12 months",
        eligible_costs="Research costs",
        eligibility="Universities and research organisations may apply.",
        geography="EU",
        trl="TRL 2-5",
        research_priorities=["resilience"],
        required_partners="Academic partner",
        deliverables="Prototype",
        evaluation_criteria="Excellence and impact",
        application_url="https://example.org/apply",
        source_url="https://example.org/call",
        source_name="Example Funder",
        publication_date="2026-01-01",
        last_updated="2026-01-02",
        research_area="DePIN",
        selection_mode="autonomous",
        selection_score=0.9,
        selection_reasons=["Verified open research call"],
        research_context=research,
    )


def make_args(tmp_path: Path, context_path: Path, **overrides):
    values = {
        "funding_context": str(context_path),
        "output_root": str(tmp_path / "outputs" / "pipeline_run"),
        "run_id": "batch2-test-run",
        "execution_mode": "parallel",
        "max_workers": 6,
        "timeout_budget": 120,
        "min_healthy_agents": 6,
        "force_refresh": False,
        "dry_run": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def write_context(tmp_path: Path) -> Path:
    path = tmp_path / "funding_context.json"
    path.write_text(json.dumps(make_context().to_dict()), encoding="utf-8")
    return path


def test_load_funding_context_round_trips_validated_context(tmp_path):
    path = write_context(tmp_path)
    loaded = run_batch2.load_funding_context(path)
    assert loaded.funding_call_id == "CALL-BATCH2-001"
    assert loaded.research_context.research_intent == "research"


def test_load_funding_context_rejects_missing_or_invalid_context(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        run_batch2.load_funding_context(tmp_path / "missing.json")

    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"funding_call_id": "CALL-X"}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        run_batch2.load_funding_context(path)


def test_dry_run_plans_exactly_the_11_downstream_agents(monkeypatch, tmp_path):
    context_path = write_context(tmp_path)
    captured = {}

    def fake_execute_batch_run(**kwargs):
        captured.update(kwargs)
        return {"status": "dry_run", "tasks": [], "report_path": "dry-run-report.json"}

    monkeypatch.setattr(run_batch2, "execute_batch_run", fake_execute_batch_run)
    code, report = run_batch2.run_batch2(make_args(tmp_path, context_path, dry_run=True))

    assert code == 0
    assert report["status"] == "dry_run"
    assert set(captured["agent_names"]) == run_batch2.DOWNSTREAM_FUNDING_CONTEXT_AGENTS
    assert len(captured["agent_names"]) == 11
    assert captured["area_names"] == [None]
    assert captured["base_input_data"]["analysis_mode"] == "collect_only"
    assert captured["base_input_data"]["allow_llm_refinement"] is False
    assert captured["base_input_data"]["allow_llm_analysis"] is False
    assert captured["funding_context"].funding_call_id == "CALL-BATCH2-001"


def test_collection_health_failure_prevents_normalization(monkeypatch, tmp_path):
    context_path = write_context(tmp_path)
    agents = sorted(run_batch2.DOWNSTREAM_FUNDING_CONTEXT_AGENTS)
    tasks = [
        {
            "agent_name": name,
            "status": "success" if index < 5 else "error",
            "outputs_count": 1 if index < 5 else 0,
        }
        for index, name in enumerate(agents)
    ]
    monkeypatch.setattr(
        run_batch2,
        "execute_batch_run",
        lambda **kwargs: {"tasks": tasks, "report_path": "collection.json"},
    )
    monkeypatch.setattr(
        run_batch2,
        "save_normalized_collection_records",
        lambda *args, **kwargs: pytest.fail("Normalization must not run after a failed collection gate"),
    )

    code, report = run_batch2.run_batch2(make_args(tmp_path, context_path))
    assert code == 3
    assert report["status"] == "collection_failed"
    assert report["collection"]["healthy_agents"] == 5


def test_successful_run_checks_normalized_records_and_ledger(monkeypatch, tmp_path):
    context_path = write_context(tmp_path)
    agents = sorted(run_batch2.DOWNSTREAM_FUNDING_CONTEXT_AGENTS)
    tasks = [
        {
            "agent_name": name,
            "status": "success" if index < 6 else "error",
            "outputs_count": 1 if index < 6 else 0,
        }
        for index, name in enumerate(agents)
    ]
    monkeypatch.setattr(
        run_batch2,
        "execute_batch_run",
        lambda **kwargs: {"tasks": tasks, "report_path": "collection.json"},
    )

    def fake_normalize(roots, call_root, call_id, filter_func, *, target_run_id):
        normalized = call_root / "normalized"
        normalized.mkdir(parents=True, exist_ok=True)
        record = {"evidence_id": "evidence-1", "record_id": "record-1", "title": "Grounded signal"}
        assert filter_func(record) is True
        records_path = normalized / "records.json"
        errors_path = normalized / "errors.json"
        records_path.write_text(json.dumps([record]), encoding="utf-8")
        errors_path.write_text("[]", encoding="utf-8")
        db_path = normalized / "evidence_ledger.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE canonical_evidence (evidence_id TEXT)")
            conn.execute("INSERT INTO canonical_evidence VALUES ('evidence-1')")
        return {"records": records_path, "errors": errors_path}

    monkeypatch.setattr(run_batch2, "save_normalized_collection_records", fake_normalize)
    monkeypatch.setattr(
        run_batch2,
        "mission_relevance",
        lambda record, context: {"passed": True, "score": 1.0, "reason": "fixture"},
    )

    code, report = run_batch2.run_batch2(make_args(tmp_path, context_path))
    assert code == 0
    assert report["status"] == "success"
    assert report["normalization"]["records_count"] == 1
    assert report["normalization"]["ledger_records"] == 1
    assert report["batch3_handoff"] == "validated; execution stops before Batch-3"
