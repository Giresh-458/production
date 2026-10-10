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
@patch('core.intelligence.save_source_discovery_candidates')
def test_source_discovery_duplicate_preservation(
    mock_save_discovery,
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

    call_id = "CALL-TEST-DISC"
    mock_evaluate_health.return_value = (True, 10)

    selected_call = {
        "selected": {
            "call_id": call_id,
            "title": "Blockchain Networking",
            "organization": "TestOrg",
            "research_area": "DePIN", "domain_relevance": [{"domain": "DePIN", "score": 1.0}],
            "research_context": {
                "funding_call_id": call_id, "research_intent": "research",
                "domain": "DePIN",
                "technology_themes": ["blockchain", "ledger"], "domain_relevance": [{"domain": "DePIN", "score": 1.0}],
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

        # 1. Accepted evidence (matches 'blockchain' & 'ledger')
        (intermediate_dir / "acc1.md").write_text("""# Signal
## Shared Metadata
- title: Blockchain basics
- source: https://example.org/accepted
- layer: Literature
- funding_call_id: CALL-TEST-DISC
- research_area: DePIN
- collected_at: 2026-08-24T10:00:00+00:00

## Problem Preview
Blockchain ledger systems.
## Evidence Bundle
### Evidence Snippets
- Blockchain ledger systems.
### Links Found
- https://example.org/link-acc
""", encoding="utf-8")

        # 2. Rejected evidence
        (intermediate_dir / "rej1.md").write_text("""# Signal
## Shared Metadata
- title: Nothing matches here
- source: https://example.org/rejected
- layer: Literature
- funding_call_id: CALL-TEST-DISC
- research_area: DePIN
- collected_at: 2026-08-24T10:00:01+00:00

## Problem Preview
Trees and leaves.
## Evidence Bundle
### Evidence Snippets
- Trees and leaves.
""", encoding="utf-8")

        (intermediate_dir / "confB.md").write_text("""# Signal
## Shared Metadata
- title: Missing title keywords
- source: https://example.org/conflict
- layer: Literature
- funding_call_id: CALL-TEST-DISC
- research_area: Unscoped
- collected_at: 2026-08-24T10:00:02+00:00

## Problem Preview
A completely unrelated problem statement.
## Evidence Bundle
### Evidence Snippets
- Shared conflict ledger snippet.
### Links Found
- https://example.org/link-confB
""", encoding="utf-8")

        (intermediate_dir / "confA.md").write_text("""# Signal
## Shared Metadata
- title: Blockchain conflict resolution
- source: https://example.org/conflict
- layer: Literature
- funding_call_id: CALL-TEST-DISC
- research_area: DePIN
- collected_at: 2026-08-24T10:00:03+00:00

## Problem Preview
Blockchain ledger systems.
## Evidence Bundle
### Evidence Snippets
- Shared conflict ledger snippet.
### Links Found
- https://example.org/link-confA
""", encoding="utf-8")

        return {"tasks": []}

    mock_execute_batch.side_effect = fake_execute_batch

    # Run pipeline
    main()

    call_root = list(tmp_path.glob("202*"))[0] / "calls" / call_id
    gate_report_path = call_root / "workflow" / "mission_relevance_gate.json"
    assert gate_report_path.exists()
    gate_report = json.loads(gate_report_path.read_text(encoding="utf-8"))

    assert gate_report["input_records"] == 3
    assert gate_report["accepted_records"] == 2
    assert gate_report["rejected_records"] == 1

    records = json.loads((call_root / "normalized" / "records.json").read_text(encoding="utf-8"))
    assert len(records) == 2

    by_area = json.loads((call_root / "normalized" / "by_area.json").read_text(encoding="utf-8"))
    assert len(by_area.get("DePIN", [])) == 2

    by_layer = json.loads((call_root / "normalized" / "by_layer.json").read_text(encoding="utf-8"))
    assert len(by_layer.get("Literature", [])) == 2

    from core.intelligence import refresh_intelligence_views
    with patch('core.intelligence.build_source_review_queue'), \
         patch('core.intelligence.ensure_recursive_review_log', return_value="a"), \
         patch('core.intelligence.export_source_health_summary'), \
         patch('core.intelligence.build_structured_output_index', return_value=[]), \
         patch('core.intelligence.load_manifest_entries', return_value=[]), \
         patch('core.intelligence.load_source_registry_entries', return_value=[]), \
         patch('core.intelligence.build_problem_ranking_index', return_value=[]), \
         patch('core.intelligence.build_promotion_index', return_value={}), \
         patch('core.intelligence.build_promotion_summary', return_value={}), \
         patch('core.intelligence.detect_evidence_relationships', return_value={}), \
         patch('core.intelligence.build_problem_clusters', return_value=[]), \
         patch('core.intelligence.build_batch_workflow_status', return_value={}), \
         patch('core.intelligence.build_operational_views', return_value={}), \
         patch('core.intelligence.generate_and_save_quality_metrics', return_value=Path("g")):
         refresh_intelligence_views(call_root)

    assert mock_save_discovery.call_count == 1
    discovery_args = mock_save_discovery.call_args[0][0]

    assert len(discovery_args) == 3

    titles = [r.get("title", "") for r in discovery_args]
    assert "Blockchain basics" in titles
    assert titles.count("Blockchain conflict resolution") == 2
    assert "Missing title keywords" not in titles
    assert "Nothing matches here" not in titles

    problem_statements = [r.get("problem_statement", "") for r in discovery_args]
    assert problem_statements.count("Blockchain ledger systems.") == 3
    assert "A completely unrelated problem statement." not in problem_statements

    research_areas = [r.get("research_area", "") for r in discovery_args]
    assert research_areas.count("DePIN") == 3
    assert "Unscoped" not in research_areas

    # Verify linked_urls were extracted and preserved physically
    linked_urls = [url for r in discovery_args for url in (r.get("linked_urls", []) or [])]
    assert "https://example.org/link-confA" in linked_urls
    assert "https://example.org/link-confB" in linked_urls
