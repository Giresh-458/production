import pytest
import json
import os
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.normalization import ensure_normalized_collection_records

def test_legacy_ungated_records_bypass(tmp_path):
    call_id = "CALL-TEST-CACHE"
    run_root = tmp_path / "outputs" / "run_cache"
    call_root = run_root / "calls" / call_id
    call_root.mkdir(parents=True)

    # Setup context
    workflow_dir = call_root / "workflow"
    workflow_dir.mkdir(parents=True)
    selected_call = {
        "selected": {
            "call_id": call_id,
            "title": "Quantum",
            "organization": "QOrg",
            "research_area": "DePIN",
            "research_context": {
                "funding_call_id": call_id,
                "domain": "DePIN",
                "technology_themes": ["quantum"],
                "funding_priorities": [], "target_outcomes": [], "investigation_questions": [], "inferred_challenges": [], "evidence_refs": [], "inference_provenance": [], "confidence": "high"
            }
        }
    }
    (workflow_dir / "selected_funding_call.json").write_text(json.dumps(selected_call), encoding="utf-8")

    # Create intermediate
    intermediate = call_root / "intermediate"
    intermediate.mkdir()
    (intermediate / "item.md").write_text("# Old signal", encoding="utf-8")

    # Old timestamp for intermediate
    old_time = time.time() - 1000
    os.utime(intermediate / "item.md", (old_time, old_time))

    # Create stale normalized records containing an ungated legacy item
    normalized = call_root / "normalized"
    normalized.mkdir()
    records_json = normalized / "records.json"

    # Item that should fail the gate (doesn't match 'quantum')
    legacy_records = [{
        "evidence_id": "legacy1",
        "title": "Unrelated",
        "research_area": "DePIN",
        "problem_statement": "Trees.",
        "context_summary": "Leaves.",
        "evidence_snippets": ["Trees."],
        "collected_at": "2026-08-24T10:00:00+00:00"
    }]
    records_json.write_text(json.dumps(legacy_records), encoding="utf-8")

    # New timestamp for records to force cache hit
    new_time = time.time()
    os.utime(records_json, (new_time, new_time))

    # Invoke
    ensure_normalized_collection_records(call_root)

    # Verify
    records = json.loads(records_json.read_text(encoding="utf-8"))
    assert len(records) == 0, "Legacy ungated record bypassed the mission gate!"

def test_cache_hit_global_refresh_is_preserved(tmp_path):
    run_root = tmp_path / "outputs" / "run_cache_global"
    run_root.mkdir(parents=True)

    intermediate = run_root / "intermediate"
    intermediate.mkdir()
    (intermediate / "item.md").write_text("# Old signal", encoding="utf-8")

    old_time = time.time() - 1000
    os.utime(intermediate / "item.md", (old_time, old_time))

    normalized = run_root / "normalized"
    normalized.mkdir()
    records_json = normalized / "records.json"

    legacy_records = [{
        "evidence_id": "global1",
        "title": "Global Item",
        "research_area": "DePIN",
        "problem_statement": "Test.",
        "context_summary": "Test.",
        "evidence_snippets": ["Test."],
        "collected_at": "2026-08-24T10:00:00+00:00"
    }]
    records_json.write_text(json.dumps(legacy_records), encoding="utf-8")

    new_time = time.time()
    os.utime(records_json, (new_time, new_time))

    from unittest.mock import patch
    with patch("core.normalization.save_normalized_collection_records") as mock_save:
        ensure_normalized_collection_records(run_root)
        mock_save.assert_not_called()

    records = json.loads(records_json.read_text(encoding="utf-8"))
    assert len(records) == 1
    assert records[0]["evidence_id"] == "global1"
