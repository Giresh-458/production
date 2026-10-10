import pytest
import json
import os
import time
from pathlib import Path
import sys
from unittest.mock import patch
import copy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.normalization import ensure_normalized_collection_records

def test_cache_advanced_index_repair(tmp_path):
    call_id = "CALL-ADVANCED"
    run_root = tmp_path / "outputs" / "run_adv"
    call_root = run_root / "calls" / call_id
    call_root.mkdir(parents=True, exist_ok=True)

    workflow_dir = call_root / "workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    selected_call = {
        "selected": {
            "call_id": call_id,
            "title": "Quantum",
            "organization": "QOrg",
            "research_area": "DePIN",
            "research_context": {
                "funding_call_id": call_id,
                "domain": "DePIN",
                "technology_themes": [],
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
    intermediate.mkdir(exist_ok=True)
    (intermediate / "item.md").write_text("# Old signal", encoding="utf-8")
    old_time = time.time() - 1000
    os.utime(intermediate / "item.md", (old_time, old_time))

    normalized = call_root / "normalized"
    normalized.mkdir(exist_ok=True)

    # Missing layer, empty layer, whitespace layer, unrecognized area
    good_records = [
        {
            "evidence_id": "r1",
            "title": "Unrecognized area",
            "research_area": "MysticArts",  # Should map to "Unscoped"
            "layer": "Literature",
            "problem_statement": "Magic.",
            "context_summary": "Magic.",
            "evidence_snippets": ["Magic."],
            "collected_at": "2026-08-24T10:00:00+00:00"
        },
        {
            "evidence_id": "r2",
            "title": "Missing layer",
            "research_area": "DePIN",
            # No layer key
            "problem_statement": "Logic.",
            "context_summary": "Logic.",
            "evidence_snippets": ["Logic."],
            "collected_at": "2026-08-24T10:00:00+00:00"
        },
        {
            "evidence_id": "r3",
            "title": "Empty layer",
            "research_area": "DePIN",
            "layer": "", # Should be omitted
            "problem_statement": "Logic.",
            "context_summary": "Logic.",
            "evidence_snippets": ["Logic."],
            "collected_at": "2026-08-24T10:00:00+00:00"
        },
        {
            "evidence_id": "r4",
            "title": "Whitespace layer",
            "research_area": "DePIN",
            "layer": "   ", # Should be omitted
            "problem_statement": "Logic.",
            "context_summary": "Logic.",
            "evidence_snippets": ["Logic."],
            "collected_at": "2026-08-24T10:00:00+00:00"
        }
    ]

    # Stale indices intentionally corrupted
    stale_area = {
        "DePIN": [good_records[1], good_records[2], good_records[3]],
        "MysticArts": [good_records[0]] # Wrong key
    }
    stale_layer = {
        "Literature": [good_records[0]],
        "": [good_records[2]], # Should not exist
        "   ": [good_records[3]] # Should not exist
    }

    records_json = normalized / "records.json"
    records_json.write_text(json.dumps(good_records), encoding="utf-8")

    by_area_json = normalized / "by_area.json"
    by_area_json.write_text(json.dumps(stale_area), encoding="utf-8")

    by_layer_json = normalized / "by_layer.json"
    by_layer_json.write_text(json.dumps(stale_layer), encoding="utf-8")

    new_time = time.time()
    os.utime(records_json, (new_time, new_time))
    os.utime(by_area_json, (new_time, new_time))
    os.utime(by_layer_json, (new_time, new_time))

    def fake_relevance(rec, ctx):
        gate = {"passed": True}
        rec["mission_relevance"] = gate
        return gate

    with patch("core.mission_gate.relevance", side_effect=fake_relevance):
        ensure_normalized_collection_records(call_root)

        area_data = json.loads(by_area_json.read_text(encoding="utf-8"))
        layer_data = json.loads(by_layer_json.read_text(encoding="utf-8"))

        # Verify Unrecognized area mapping
        assert "MysticArts" not in area_data
        assert "Unscoped" in area_data
        assert area_data["Unscoped"][0]["evidence_id"] == "r1"

        # Verify missing/empty/whitespace layer omitted
        assert "" not in layer_data
        assert "   " not in layer_data

        # Verify mutation isolated (stale index containing conflicting record repaired with full equality)
        assert "mission_relevance" not in area_data["Unscoped"][0]
        assert area_data["Unscoped"][0] == good_records[0]

        # Verify stability
        os.utime(records_json, (new_time, new_time))
        os.utime(by_area_json, (new_time, new_time))
        os.utime(by_layer_json, (new_time, new_time))

        ensure_normalized_collection_records(call_root)
        area_data_2 = json.loads(by_area_json.read_text(encoding="utf-8"))
        assert area_data_2 == area_data

def test_rejected_evidence_triggers_refresh(tmp_path):
    call_id = "CALL-REJECT"
    run_root = tmp_path / "outputs" / "run_rej"
    call_root = run_root / "calls" / call_id
    call_root.mkdir(parents=True, exist_ok=True)

    workflow_dir = call_root / "workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "selected_funding_call.json").write_text(json.dumps({
        "selected": {
            "call_id": call_id,
            "title": "Quantum",
            "organization": "QOrg",
            "research_area": "DePIN",
            "research_context": {
                "funding_call_id": call_id,
                "domain": "DePIN",
                "technology_themes": [],
                "funding_priorities": [],
                "target_outcomes": [],
                "investigation_questions": [],
                "inferred_challenges": [],
                "evidence_refs": [],
                "inference_provenance": "mock",
                "confidence": "high"
            }
        }
    }), encoding="utf-8")

    intermediate = call_root / "intermediate"
    intermediate.mkdir(exist_ok=True)
    (intermediate / "item.md").write_text("# Old signal", encoding="utf-8")
    old_time = time.time() - 1000
    os.utime(intermediate / "item.md", (old_time, old_time))

    normalized = call_root / "normalized"
    normalized.mkdir(exist_ok=True)

    records_json = normalized / "records.json"
    records_json.write_text(json.dumps([{"evidence_id": "r1", "title": "reject"}]), encoding="utf-8")

    new_time = time.time()
    os.utime(records_json, (new_time, new_time))

    def fake_relevance(rec, ctx):
        return {"passed": False}

    with patch("core.mission_gate.relevance", side_effect=fake_relevance):
        with patch("core.normalization.save_normalized_collection_records") as mock_save:
            mock_save.return_value = {"records": records_json}
            ensure_normalized_collection_records(call_root)
            mock_save.assert_called_once()
