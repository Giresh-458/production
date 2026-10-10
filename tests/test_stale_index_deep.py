import pytest
import json
import os
import time
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.normalization import ensure_normalized_collection_records

def test_deep_index_repair(tmp_path):
    call_id = "CALL-TEST-DEEP-REPAIR"
    run_root = tmp_path / "outputs" / "run_repair"
    call_root = run_root / "calls" / call_id
    call_root.mkdir(parents=True)

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
                "funding_priorities": [],
                "target_outcomes": [],
                "investigation_questions": [],
                "inferred_challenges": [],
                "evidence_refs": [],
                "inference_provenance": "mock",
                "confidence": "high"
            }
        }
    }
    (workflow_dir / "selected_funding_call.json").write_text(json.dumps(selected_call), encoding="utf-8")

    intermediate = call_root / "intermediate"
    intermediate.mkdir()
    (intermediate / "item.md").write_text("# Old signal", encoding="utf-8")
    old_time = time.time() - 1000
    os.utime(intermediate / "item.md", (old_time, old_time))

    normalized = call_root / "normalized"
    normalized.mkdir()

    good_record = {
        "evidence_id": "good1",
        "title": "Quantum Setup",
        "research_area": "DePIN",
        "layer": "Literature",
        "problem_statement": "Quantum logic.",
        "context_summary": "Quantum.",
        "evidence_snippets": ["Quantum."],
        "collected_at": "2026-08-24T10:00:00+00:00"
    }

    # Conflicting record: SAME evidence_id, DIFFERENT title / problem statement
    conflicting_record = {
        "evidence_id": "good1",
        "title": "Stale Title",
        "research_area": "DePIN",
        "layer": "Literature",
        "problem_statement": "Stale Problem.",
        "context_summary": "Quantum.",
        "evidence_snippets": ["Quantum."],
        "collected_at": "2026-08-24T10:00:00+00:00"
    }

    records_json = normalized / "records.json"
    records_json.write_text(json.dumps([good_record]), encoding="utf-8")

    by_area_json = normalized / "by_area.json"
    by_area_json.write_text(json.dumps({"DePIN": [conflicting_record]}), encoding="utf-8")

    by_layer_json = normalized / "by_layer.json"
    by_layer_json.write_text(json.dumps({"Literature": [conflicting_record]}), encoding="utf-8")

    new_time = time.time()
    os.utime(records_json, (new_time, new_time))
    os.utime(by_area_json, (new_time, new_time))
    os.utime(by_layer_json, (new_time, new_time))

    # Mock so that we just check IF it was called. Also mock the mission gate so we guarantee good_record passes!
    with patch("core.mission_gate.relevance") as mock_gate, patch("core.normalization.save_normalized_collection_records") as mock_save:
        mock_gate.return_value = {"passed": True}

        ensure_normalized_collection_records(call_root)

        # It should NOT have called save_normalized_collection_records (full refresh).
        # It should repair the indexes locally!
        mock_save.assert_not_called()

    area_data = json.loads(by_area_json.read_text(encoding="utf-8"))
    assert len(area_data.get("DePIN", [])) == 1
    assert area_data["DePIN"][0]["title"] == "Quantum Setup"
    assert area_data["DePIN"][0]["problem_statement"] == "Quantum logic."

    layer_data = json.loads(by_layer_json.read_text(encoding="utf-8"))
    assert len(layer_data.get("Literature", [])) == 1
    assert layer_data["Literature"][0]["title"] == "Quantum Setup"
