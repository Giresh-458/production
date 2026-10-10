import pytest
import json
import os
import time
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.normalization import ensure_normalized_collection_records

def test_stale_index_survives_cache_hit(tmp_path):
    call_id = "CALL-TEST-STALE-IDX"
    run_root = tmp_path / "outputs" / "run_stale"
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
        "problem_statement": "Quantum.",
        "context_summary": "Quantum.",
        "evidence_snippets": ["Quantum."],
        "collected_at": "2026-08-24T10:00:00+00:00"
    }

    bad_record = {
        "evidence_id": "bad1",
        "title": "Unrelated",
        "research_area": "DePIN",
        "layer": "Literature",
        "problem_statement": "Trees.",
        "context_summary": "Leaves.",
        "evidence_snippets": ["Trees."],
        "collected_at": "2026-08-24T10:00:00+00:00"
    }

    records_json = normalized / "records.json"
    records_json.write_text(json.dumps([good_record]), encoding="utf-8")

    by_area_json = normalized / "by_area.json"
    by_area_json.write_text(json.dumps({"DePIN": [good_record, bad_record]}), encoding="utf-8")

    by_layer_json = normalized / "by_layer.json"
    by_layer_json.write_text(json.dumps({"Literature": [good_record, bad_record]}), encoding="utf-8")

    new_time = time.time()
    os.utime(records_json, (new_time, new_time))
    os.utime(by_area_json, (new_time, new_time))
    os.utime(by_layer_json, (new_time, new_time))

    with patch("core.mission_gate.relevance") as mock_gate, patch("core.normalization.save_normalized_collection_records") as mock_save:
        mock_gate.return_value = {"passed": True}

        ensure_normalized_collection_records(call_root)

        # We upgraded to repair, so it should NOT call full refresh
        mock_save.assert_not_called()

    area_data = json.loads(by_area_json.read_text(encoding="utf-8"))
    assert len(area_data.get("DePIN", [])) == 1, "Stale record survived in by_area.json!"

    layer_data = json.loads(by_layer_json.read_text(encoding="utf-8"))
    assert len(layer_data.get("Literature", [])) == 1, "Stale record survived in by_layer.json!"
