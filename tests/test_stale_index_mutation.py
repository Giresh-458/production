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

def test_cache_mutation_is_isolated(tmp_path):
    call_id = "CALL-TEST-MUTATION"
    run_root = tmp_path / "outputs" / "run_mutation"
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
    intermediate.mkdir(exist_ok=True)
    (intermediate / "item.md").write_text("# Old signal", encoding="utf-8")
    old_time = time.time() - 1000
    os.utime(intermediate / "item.md", (old_time, old_time))

    normalized = call_root / "normalized"
    normalized.mkdir(exist_ok=True)

    # Notice we don't have mission_relevance here!
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

    def fake_relevance(rec, ctx):
        gate = {"passed": True, "reason": "mock passed"}
        # Mutating the record passed to filter_func
        rec["mission_relevance"] = gate
        return gate

    with patch("core.mission_gate.relevance", side_effect=fake_relevance):
        ensure_normalized_collection_records(call_root)

        area_data_1 = json.loads(by_area_json.read_text(encoding="utf-8"))
        records_data_1 = json.loads(records_json.read_text(encoding="utf-8"))

        # Verify mutation was isolated
        assert "mission_relevance" not in area_data_1['DePIN'][0], "Mutation leaked into repaired index!"
        assert "mission_relevance" not in records_data_1[0], "Mutation leaked into records.json!"

        # Verify exact match
        assert area_data_1['DePIN'][0] == records_data_1[0], "Repaired index mismatches canonical record!"

        # Repeated hit should remain consistent
        os.utime(records_json, (new_time, new_time))
        os.utime(by_area_json, (new_time, new_time))
        os.utime(by_layer_json, (new_time, new_time))

        ensure_normalized_collection_records(call_root)
        area_data_2 = json.loads(by_area_json.read_text(encoding="utf-8"))
        assert area_data_2['DePIN'][0] == area_data_1['DePIN'][0], "Repeated cache hit introduced differences!"
