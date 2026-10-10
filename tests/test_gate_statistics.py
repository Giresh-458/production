import pytest
import json
from unittest.mock import patch
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from run_pipeline import main

@patch('run_pipeline.run_stage')
@patch('run_pipeline.get_all_open_funding_calls')

@patch('run_pipeline.execute_batch_run')
@patch('run_pipeline.evaluate_collection_health')
@patch('core.source_registry.sync_source_registry')
@patch('core.source_registry.build_source_refresh_plan')
def test_duplicate_mission_gate_statistics(
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

    call_id = "CALL-TEST-DUPES"
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

    # Mock execute_batch_run to create intermediate files
    def fake_execute_batch(**kwargs):
        root = Path(kwargs["base_input_data"]["pipeline_run_dir"])
        intermediate_dir = root / "calls" / call_id / "intermediate"
        intermediate_dir.mkdir(parents=True, exist_ok=True)

        # 1. Accepted evidence (matches 'quantum') - copy 1
        (intermediate_dir / "acc1.md").write_text("""# Signal
## Shared Metadata
- title: Quantum entanglement
- source: https://example.org/quant
- layer: Literature
- funding_call_id: CALL-TEST-DUPES
- research_area: DePIN
- collected_at: 2026-08-24T10:00:00+00:00

## Problem Preview
Quantum entanglement networks.
## Evidence Bundle
### Evidence Snippets
- Quantum entanglement networks.
""", encoding="utf-8")

        # 2. Accepted evidence (matches 'quantum') - copy 2 (DUPLICATE source and title -> same evidence_id)
        (intermediate_dir / "acc2.md").write_text("""# Signal
## Shared Metadata
- title: Quantum entanglement
- source: https://example.org/quant
- layer: Literature
- funding_call_id: CALL-TEST-DUPES
- research_area: DePIN
- collected_at: 2026-08-24T10:00:01+00:00

## Problem Preview
Quantum entanglement networks part 2.
## Evidence Bundle
### Evidence Snippets
- Quantum entanglement networks.
""", encoding="utf-8")

        # 3. Rejected evidence (no matching substantive keywords)
        (intermediate_dir / "rej1.md").write_text("""# Signal
## Shared Metadata
- title: Classic networking
- source: https://example.org/class
- layer: Literature
- funding_call_id: CALL-TEST-DUPES
- research_area: DePIN
- collected_at: 2026-08-24T10:00:02+00:00

## Problem Preview
Classic networking networks.
## Evidence Bundle
### Evidence Snippets
- Classic networking networks.
""", encoding="utf-8")

        return {"tasks": []}

    mock_execute_batch.side_effect = fake_execute_batch

    # Run the pipeline
    main()

    # Assertions
    run_dirs = list(tmp_path.glob("202*"))
    assert len(run_dirs) == 1
    call_root = run_dirs[0] / "calls" / call_id

    gate_report_path = call_root / "workflow" / "mission_relevance_gate.json"
    assert gate_report_path.exists()
    gate_report = json.loads(gate_report_path.read_text(encoding="utf-8"))

    assert gate_report["accepted_records"] == 1
    assert gate_report["rejected_records"] == 1
    assert gate_report["input_records"] == 2

    records = json.loads((call_root / "normalized" / "records.json").read_text(encoding="utf-8"))
    assert len(records) == 1

    by_area = json.loads((call_root / "normalized" / "by_area.json").read_text(encoding="utf-8"))
    assert len(by_area.get("DePIN", [])) == 1

    by_layer = json.loads((call_root / "normalized" / "by_layer.json").read_text(encoding="utf-8"))
    assert len(by_layer.get("Literature", [])) == 1
