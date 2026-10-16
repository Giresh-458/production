import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from core.source_registry import (
    REFRESH_PLAN_PATH,
    SOURCE_REGISTRY_PATH,
    SOURCES_DIR,
    build_source_refresh_plan,
    record_source_check_result,
    sync_source_registry,
)
import run_pipeline


def _sample_registry_payload() -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "generated_at": now,
        "entries": [
            {
                "source_id": "company:test:1",
                "agent_name": "company",
                "name": "Test Company",
                "url": "https://example.com/company",
                "status": "active",
                "active_config": True,
                "fetch_frequency": "weekly",
                "last_checked": now,
                "last_changed": now,
                "config_hash": "cfg1",
                "content_hash": "cnt1",
                "created_at": now,
                "updated_at": now,
            }
        ],
        "stats": {
            "total_entries": 1,
            "active_entries": 1,
            "deprecated_entries": 0,
        },
    }


def test_sync_source_registry_writes_only_to_temporary_path(tmp_path: Path):
    """Ensure sync_source_registry strictly writes to the specified registry_path and preserves default."""
    temp_registry = tmp_path / "source_registry.json"
    assert not temp_registry.exists()

    initial_mtime = SOURCE_REGISTRY_PATH.stat().st_mtime_ns
    initial_bytes = SOURCE_REGISTRY_PATH.read_bytes()

    result = sync_source_registry(
        sources_dir=SOURCES_DIR,
        registry_path=temp_registry,
        agent_filter="funding",
    )

    assert temp_registry.exists()
    assert result.total_entries > 0
    assert Path(result.registry_path) == temp_registry.resolve()

    # The project's global source_registry.json MUST not be touched
    assert SOURCE_REGISTRY_PATH.stat().st_mtime_ns == initial_mtime
    assert SOURCE_REGISTRY_PATH.read_bytes() == initial_bytes


def test_build_source_refresh_plan_writes_only_to_temporary_path(tmp_path: Path):
    """Ensure build_source_refresh_plan uses temporary paths and does not overwrite default files."""
    temp_registry = tmp_path / "source_registry.json"
    temp_plan = tmp_path / "source_refresh_plan.json"
    temp_registry.write_text(json.dumps(_sample_registry_payload(), indent=2), encoding="utf-8")

    initial_registry_mtime = SOURCE_REGISTRY_PATH.stat().st_mtime_ns
    initial_registry_bytes = SOURCE_REGISTRY_PATH.read_bytes()
    initial_plan_exists = REFRESH_PLAN_PATH.exists()
    initial_plan_bytes = REFRESH_PLAN_PATH.read_bytes() if initial_plan_exists else None

    result = build_source_refresh_plan(
        registry_path=temp_registry,
        output_path=temp_plan,
        agent_filter="company",
    )

    assert temp_plan.exists()
    assert result.total_entries == 1
    assert Path(result.refresh_plan_path) == temp_plan.resolve()

    # Ensure default global registry and default refresh plan were not touched
    assert SOURCE_REGISTRY_PATH.stat().st_mtime_ns == initial_registry_mtime
    assert SOURCE_REGISTRY_PATH.read_bytes() == initial_registry_bytes
    if initial_plan_exists:
        assert REFRESH_PLAN_PATH.read_bytes() == initial_plan_bytes


def test_source_status_and_check_result_use_temporary_path(tmp_path: Path):
    """Ensure mutation operations on sources use temporary registry files."""
    temp_registry = tmp_path / "source_registry.json"
    temp_registry.write_text(json.dumps(_sample_registry_payload(), indent=2), encoding="utf-8")

    initial_mtime = SOURCE_REGISTRY_PATH.stat().st_mtime_ns
    initial_bytes = SOURCE_REGISTRY_PATH.read_bytes()

    record_source_check_result(
        source_id="company:test:1",
        checked_at=datetime.now(UTC).isoformat(),
        registry_path=temp_registry,
    )

    record_source_check_result(
        source_id="company:test:1",
        status="paused",
        registry_path=temp_registry,
    )

    # Check mutations stayed in temp_registry
    data = json.loads(temp_registry.read_text(encoding="utf-8"))
    entry = data["entries"][0]
    assert entry["status"] == "paused"

    # Default registry untouched
    assert SOURCE_REGISTRY_PATH.stat().st_mtime_ns == initial_mtime
    assert SOURCE_REGISTRY_PATH.read_bytes() == initial_bytes


def test_pipeline_execution_does_not_mutate_default_registry(tmp_path: Path, monkeypatch):
    """Regression check: running the pipeline with mocked stages does not mutate default registry."""
    initial_mtime = SOURCE_REGISTRY_PATH.stat().st_mtime_ns
    initial_bytes = SOURCE_REGISTRY_PATH.read_bytes()

    mock_funding = {
        "status": "success",
        "outputs": [{
            "call_id": "test-call-1",
            "title": "Test Call",
            "status": "OPEN_CALL",
            "source_url": "https://example.com/call",
            "application_url": "https://example.com/call/apply",
            "eligibility": "Eligible applicants must be legal entities established in an EU Member State.",
            "deadline": "2030-01-01",
            "domain_relevance": [{"domain": "DePIN", "score": 0.8, "raw_score": 3.0, "matched_terms": ["depin"]}],
            "research_context": {
                "funding_call_id": "test-call-1",
                "domain": "DePIN",
                "technology_themes": [],
                "funding_priorities": [],
                "target_outcomes": [],
                "investigation_questions": [],
                "inferred_challenges": [],
                "evidence_refs": [],
                "inference_provenance": "mock",
                "confidence": 1.0,
                "research_intent": "research",
                "research_intent_evidence": [],
            },
        }],
    }

    def fake_run_stage(agent, mode, area, payload, context=None, **kwargs):
        if agent == "funding":
            return mock_funding
        if agent == "proposal":
            return {"status": "success", "outputs": ["proposal.md"]}
        return {"status": "success", "outputs": [{"prop": 1}], "outputs_count": 1}

    def fake_save(roots, call_root, call_id, **kwargs):
        if "filter_func" in kwargs and kwargs["filter_func"]:
            kwargs["filter_func"]({"evidence_id": "e1", "title": "t", "research_area": "DePIN"})
        norm = call_root / "normalized"
        norm.mkdir(parents=True, exist_ok=True)
        (norm / "records.json").write_text('[{"evidence_id": "e1", "record_id": "r1"}]', encoding="utf-8")
        prop_dir = call_root / "proposals"
        prop_dir.mkdir(parents=True, exist_ok=True)
        (prop_dir / "proposal_1.md").write_text("# Proposal", encoding="utf-8")
        return [{"record_id": "r1"}]

    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--output-root", str(tmp_path)])

    with patch("run_pipeline.run_stage", side_effect=fake_run_stage), \
         patch("run_pipeline.execute_batch_run", return_value={"tasks": [{"agent_name": f"a_{i}", "status": "success", "outputs_count": 1} for i in range(11)]}), \
         patch("run_pipeline.evaluate_collection_health", return_value=(True, 11)), \
         patch("run_pipeline.save_normalized_collection_records", side_effect=fake_save), \
         patch("run_pipeline.mission_relevance", return_value={"passed": True}), \
         patch("core.source_registry.sync_source_registry") as mock_sync, \
         patch("core.source_registry.build_source_refresh_plan") as mock_plan:

        exit_code = run_pipeline.main()

    assert exit_code == 0
    assert mock_sync.call_count == 1
    assert mock_plan.call_count == 1

    # Verify default registry was not mutated
    assert SOURCE_REGISTRY_PATH.stat().st_mtime_ns == initial_mtime
    assert SOURCE_REGISTRY_PATH.read_bytes() == initial_bytes
