from __future__ import annotations
import json
from pathlib import Path
import pytest
from unittest.mock import patch
from core.agent_registry import run_registered_agent
from tests.test_multi_call_evidence_attribution import make_context

@patch("core.agent_registry.sync_source_registry")
@patch("core.agent_registry.build_source_refresh_plan")
@patch("core.agent_registry.mark_agent_sources_checked")
def test_provenance_trace(
    mock_mark_agent_sources_checked,
    mock_build_source_refresh_plan,
    mock_sync_source_registry,
    tmp_path: Path,
):
    root = tmp_path / "rif"
    root.mkdir()
    
    # Funding A
    ctx = make_context("FUNDING-A", "Trace Program", ["traceable provenance evidence", "document demonstrating provenance"])
    
    # Collector
    col_out = run_registered_agent(
        "lab",
        "manual_text",
        area="RWA",
        input_data={"text": "This is a traceable evidence document demonstrating provenance for FUNDING-A. " * 50, "source_url": "https://example.com/test-source", "output_dir": str(root / "intermediate" / "lab")},
        funding_contexts=[ctx]
    )
    assert col_out["status"] in ("success", "partial_success")
    assert col_out["items_saved"] > 0
    print("COL_OUT:", col_out)
    
    # Normalize
    from core.normalization import save_normalized_collection_records
    norm_res = save_normalized_collection_records(root, root)
    records = json.loads(norm_res["records"].read_text(encoding="utf-8"))
    assert len(records) == 1
    assert "FUNDING-A" in records[0]["funding_call_ids"]
    
    # Tagging & Clustering & Synthesis
    run_registered_agent("tagging", "configured_scan", area="RWA", input_data={"outputs_root": str(root), "output_dir": str(root / "processed" / "tags")})
    run_registered_agent("clustering", "configured_scan", area="RWA", input_data={"outputs_root": str(root), "output_dir": str(root / "processed" / "clusters"), "threshold": 0.20})
    run_registered_agent("trend", "configured_scan", area="RWA", input_data={"outputs_root": str(root), "output_dir": str(root / "processed" / "trends")})
    run_registered_agent("synthesis", "configured_scan", area="RWA", input_data={"outputs_root": str(root), "output_dir": str(root / "synthesis" / "generated"), "manifest_dir": str(root / "synthesis" / "manifest")})
    
    # Idea
    from unittest.mock import patch
    with patch("agents.idea_agent.perform_novelty_search") as mock_search, patch("agents.proposal_agent.mission_relevance") as mock_mission:
        mock_search.return_value = {"novelty_status": "no_overlap_in_search", "novelty_confidence": "High", "search_coverage": 1.0, "search_mode": "external", "query_executed": True, "similar_work": []}
        idea = run_registered_agent("idea", "configured_scan", area="RWA", input_data={"output_dir": str(root / "ideas"), "manifest_dir": str(root / "synthesis" / "manifest"), "external_novelty_validation": True})
        
        mock_mission.return_value = {"passed": True, "score": 0.8, "reason": "Mocked"}
        proposal = run_registered_agent("proposal", "configured_scan", area="RWA", input_data={"output_dir": str(root / "proposals"), "idea_dir": str(root / "ideas"), "__selected_call_context": ctx})
    
    # Verify provenance
    print("IDEA:", idea)
    print("PROPOSAL:", proposal)
    proposals = json.loads((root / "proposals" / "rwa" / "proposals.json").read_text(encoding="utf-8"))
    assert len(proposals) > 0
    p = proposals[0]
    assert "FUNDING-A" in p["funding_call_ids"]
    


def test_artifact_collection_timestamp_is_not_replaced_with_run_start():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from core.agent_interface import _document_collected_at

    source_time = datetime(2026, 11, 12, 10, 30, tzinfo=timezone.utc)
    assert _document_collected_at(
        SimpleNamespace(collected_at=source_time)
    ) == source_time
    assert _document_collected_at(
        SimpleNamespace(collected_at="2026-11-12T10:30:00Z")
    ) == source_time


def test_artifact_collection_timestamp_falls_back_for_naive_source_time():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from core.agent_interface import _document_collected_at

    before = datetime.now(timezone.utc)
    collected_at = _document_collected_at(
        SimpleNamespace(collected_at="2026-11-12T10:30:00")
    )
    after = datetime.now(timezone.utc)

    assert collected_at.tzinfo is not None
    assert before <= collected_at <= after


def test_intermediate_artifact_rejects_tampered_provenance_and_unsafe_run_identity():
    from datetime import datetime, timezone

    from core.agent_interface import (
        build_intermediate_artifact_payload,
        validate_intermediate_artifact,
    )

    payload, _ = build_intermediate_artifact_payload(
        agent="data_availability",
        layer="Data Availability",
        mode="manual_text",
        area="RWA",
        title="Dataset catalog evidence",
        source="https://example.org/catalog",
        content=(
            "This source describes a reproducible dataset catalog for research. "
            "It documents data access, schema coverage, update frequency, and "
            "benchmark limitations for research teams."
        ),
        collected_at=datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc),
    )
    payload["agent_specific_body"] = {
        "source_type": "dataset_catalog",
        "evidence_type": "dataset_listing",
        "focus": "RWA",
    }
    payload["shared_header"]["run_id"] = "run-2026-11-14"
    payload["shared_header"]["funding_call_ids"] = ["CALL-001"]
    payload["shared_header"]["funding_call_id"] = "CALL-001"

    assert validate_intermediate_artifact(payload) == []

    tampered_hash = dict(payload)
    tampered_hash["provenance"] = dict(payload["provenance"])
    tampered_hash["provenance"]["content_hash"] = "0" * 64
    assert any(
        "content_hash does not match raw_content" in error
        for error in validate_intermediate_artifact(tampered_hash)
    )

    mismatched_time = dict(payload)
    mismatched_time["provenance"] = dict(payload["provenance"])
    mismatched_time["provenance"]["collected_at"] = "2026-11-13T10:00:00+00:00"
    assert any(
        "provenance 'collected_at' does not match shared metadata" in error
        for error in validate_intermediate_artifact(mismatched_time)
    )

    unsafe_run = dict(payload)
    unsafe_run["shared_header"] = dict(payload["shared_header"])
    unsafe_run["shared_header"]["run_id"] = "../escape"
    assert any(
        "run_id contains unsafe path characters" in error
        for error in validate_intermediate_artifact(unsafe_run)
    )
