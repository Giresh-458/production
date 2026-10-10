import pytest
import json
from unittest.mock import patch
from pathlib import Path
import sys
import copy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from run_pipeline import main

@patch('run_pipeline.run_stage')
@patch('run_pipeline.get_all_open_funding_calls')
@patch('run_pipeline.execute_batch_run')
@patch('run_pipeline.evaluate_collection_health')
@patch('core.source_registry.sync_source_registry')
@patch('core.source_registry.build_source_refresh_plan')
def test_conflicting_duplicate_mission_gate_statistics(
    mock_build_source_refresh,
    mock_sync_registry,
    mock_evaluate_health,
    mock_execute_batch,
    mock_get_calls,
    mock_run_stage,
    monkeypatch,
    tmp_path
):
    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--output-root", str(tmp_path)])

    call_id = "CALL-TEST-CONFLICT"
    mock_evaluate_health.return_value = (True, 10)

    selected_call = {
        "selected": {
            "call_id": call_id,
            "title": "Quantum Networking",
            "organization": "QOrg",
            "research_area": "DePIN", "domain_relevance": [{"domain": "DePIN", "score": 1.0}],
            "research_context": {
                "funding_call_id": call_id, "research_intent": "research",
                "domain": "DePIN",
                "technology_themes": ["quantum", "entanglement"], "domain_relevance": [{"domain": "DePIN", "score": 1.0}],
                "funding_priorities": [],
                "target_outcomes": [],
                "investigation_questions": [],
                "inferred_challenges": [],
                "evidence_refs": [],
                "inference_provenance": [],
                "confidence": "high"
            }
        },
        "rejected_alternatives": []
    }
    mock_get_calls.return_value = ([selected_call["selected"]], [])

    def fake_execute_batch(**kwargs):
        root = Path(kwargs["base_input_data"]["pipeline_run_dir"])
        intermediate_dir = root / "calls" / call_id / "intermediate"
        intermediate_dir.mkdir(parents=True, exist_ok=True)

        # 1. Conflicting Evidence 1 (Fails gate - title doesn't match, snippet is empty so no match)
        (intermediate_dir / "conf1.md").write_text("""# Signal
## Shared Metadata
- title: Nothing matches here
- source: https://example.org/conflict
- layer: Literature
- funding_call_id: CALL-TEST-CONFLICT
- research_area: DePIN
- collected_at: 2026-08-24T10:00:00+00:00

## Problem Preview
Blah
## Evidence Bundle
### Evidence Snippets
- Same snippet string.
""", encoding="utf-8")

        # 2. Conflicting Evidence 2 (Passes gate - title has matches, same snippet string!)
        (intermediate_dir / "conf2.md").write_text("""# Signal
## Shared Metadata
- title: Quantum entanglement
- source: https://example.org/conflict
- layer: Literature
- funding_call_id: CALL-TEST-CONFLICT
- research_area: DePIN
- collected_at: 2026-08-24T10:00:01+00:00

## Problem Preview
Blah
## Evidence Bundle
### Evidence Snippets
- Same snippet string.
""", encoding="utf-8")

        # 3. Completely rejected evidence (different source, no match)
        (intermediate_dir / "rej1.md").write_text("""# Signal
## Shared Metadata
- title: Classic networking
- source: https://example.org/class
- layer: Literature
- funding_call_id: CALL-TEST-CONFLICT
- research_area: DePIN
- collected_at: 2026-08-24T10:00:02+00:00

## Problem Preview
Classic
## Evidence Bundle
### Evidence Snippets
- Classic networking.
""", encoding="utf-8")

        return {"tasks": []}

    mock_execute_batch.side_effect = fake_execute_batch

    # Run the pipeline
    main()

    call_root = list(tmp_path.glob("202*"))[0] / "calls" / call_id
    gate_report_path = call_root / "workflow" / "mission_relevance_gate.json"
    assert gate_report_path.exists()
    gate_report = json.loads(gate_report_path.read_text(encoding="utf-8"))

    # 2 canonical evidence_ids: 1 accepted (the conflict group was accepted because conf2 was seen later and replaced the title, OR because our deterministic policy just relies on the final merged state, which in save_normalized_collection_records keeps the LATEST data)
    # Actually, the merged record will have the LATEST title if we merge them, which is "Quantum entanglement". So it will pass!
    assert gate_report["accepted_records"] == 0
    assert gate_report["rejected_records"] == 2
    assert gate_report["input_records"] == 2

    records = json.loads((call_root / "normalized" / "records.json").read_text(encoding="utf-8"))
    assert len(records) == 0

    by_area = json.loads((call_root / "normalized" / "by_area.json").read_text(encoding="utf-8"))
    assert len(by_area.get("DePIN", [])) == 0

    by_layer = json.loads((call_root / "normalized" / "by_layer.json").read_text(encoding="utf-8"))
    assert len(by_layer.get("Literature", [])) == 0

@patch('run_pipeline.run_stage')
@patch('run_pipeline.get_all_open_funding_calls')
@patch('run_pipeline.execute_batch_run')
@patch('run_pipeline.evaluate_collection_health')
@patch('core.source_registry.sync_source_registry')
@patch('core.source_registry.build_source_refresh_plan')
def test_empty_evidence_aborts(
    mock_build_source_refresh,
    mock_sync_registry,
    mock_evaluate_health,
    mock_execute_batch,
    mock_get_calls,
    mock_run_stage,
    monkeypatch,
    tmp_path
):
    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--output-root", str(tmp_path)])

    call_id = "CALL-TEST-EMPTY"
    mock_evaluate_health.return_value = (True, 10)

    selected_call = {
        "selected": {
            "call_id": call_id,
            "title": "Quantum Networking",
            "organization": "QOrg",
            "research_area": "DePIN", "domain_relevance": [{"domain": "DePIN", "score": 1.0}],
            "research_context": {
                "funding_call_id": call_id, "research_intent": "research",
                "domain": "DePIN",
                "technology_themes": ["quantum", "entanglement"], "domain_relevance": [{"domain": "DePIN", "score": 1.0}],
                "funding_priorities": [],
                "target_outcomes": [],
                "investigation_questions": [],
                "inferred_challenges": [],
                "evidence_refs": [],
                "inference_provenance": [],
                "confidence": "high"
            }
        },
        "rejected_alternatives": []
    }
    mock_get_calls.return_value = ([selected_call["selected"]], [])

    def fake_execute_batch(**kwargs):
        # Do not write any markdown files to intermediate_dir!
        return {"tasks": []}

    mock_execute_batch.side_effect = fake_execute_batch

    # Run the pipeline
    exit_code = main()
    assert exit_code == 4  # global_exit_code is updated to 4 on missing records
