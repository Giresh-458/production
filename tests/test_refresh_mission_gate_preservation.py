import pytest
import json
from pathlib import Path
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.intelligence import refresh_intelligence_views
from core.funding_selection import FundingCallContext

def test_refresh_preserves_mission_gate(tmp_path):
    call_id = "CALL-TEST-99"
    run_dir = tmp_path / "outputs" / "run_abc"
    call_root = run_dir / "calls" / call_id
    call_root.mkdir(parents=True)

    # Mock a funding call context
    workflow_dir = call_root / "workflow"
    workflow_dir.mkdir(parents=True)

    # We create a fake selected call json that requires the keyword "blockchain" to pass the mission gate.
    # The mission gate matches substantive words from research_area, tech_themes, etc.
    selected_call = {
        "selected": {
            "call_id": call_id,
            "title": "Blockchain for Healthcare",
            "organization": "Test Org",
            "research_area": "DigitalHealthCPS",
            "research_context": {
                "funding_call_id": call_id,
                "domain": "Healthcare",
                "technology_themes": ["blockchain", "distributed ledger"],
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
    (workflow_dir / "selected_funding_call.json").write_text(json.dumps(selected_call), encoding="utf-8")

    # Now we create some intermediate md files.
    # 1. Accepted record (has the substantive keyword)
    accepted_md = call_root / "intermediate" / "accepted.md"
    accepted_md.parent.mkdir(parents=True)
    accepted_md.write_text("""# Signal

## Shared Metadata
- title: Accepted Blockchain Record
- source: https://example.org/1
- layer: Literature
- funding_call_id: CALL-TEST-99
- research_area: DigitalHealthCPS
- collected_at: 2026-08-24T10:00:00+00:00

## Problem Preview
This discusses distributed blockchain in healthcare, which matches the mission gate exactly.

## Evidence Bundle
### Evidence Snippets
- This discusses distributed blockchain in healthcare, which matches the mission gate exactly.
""", encoding="utf-8")

    # 2. Rejected record (missing all substantive keywords, won't pass mission gate)
    rejected_md = call_root / "intermediate" / "rejected.md"
    rejected_md.write_text("""# Signal

## Shared Metadata
- title: Rejected Record about Trees
- source: https://example.org/2
- layer: Literature
- funding_call_id: CALL-TEST-99
- research_area: Unscoped
- collected_at: 2026-08-24T10:00:00+00:00

## Problem Preview
This is just an empty document about random trees and leaves and sunshine.

## Evidence Bundle
### Evidence Snippets
- Nothing here will match the anchor text.
""", encoding="utf-8")

    with patch('core.intelligence.save_source_discovery_candidates') as mock_save_discovery, \
         patch('core.intelligence.build_source_review_queue'), \
         patch('core.intelligence.ensure_recursive_review_log'), \
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

    # Now verify normalized/records.json
    records_json = call_root / "normalized" / "records.json"
    assert records_json.exists(), "records.json was not created"

    records = json.loads(records_json.read_text(encoding="utf-8"))

    # We should ONLY see the accepted record
    assert len(records) == 1
    assert "blockchain" in records[0].get("title", "").lower()

    # The rejected record must not be in records.json!
    titles = [r.get("title", "").lower() for r in records]
    assert not any("trees" in t for t in titles), "Rejected record was reintroduced!"

    # Also verify that by_area and by_layer agree
    by_area = json.loads((call_root / "normalized" / "by_area.json").read_text(encoding="utf-8"))
    assert len(by_area.get("DigitalHealthCPS", [])) == 1
    assert "Unscoped" not in by_area or len(by_area["Unscoped"]) == 0

    by_layer = json.loads((call_root / "normalized" / "by_layer.json").read_text(encoding="utf-8"))
    assert len(by_layer.get('Literature', [])) == 1

    # Assert source discovery receives exactly the accepted record
    assert mock_save_discovery.call_count == 1
    discovery_arg = mock_save_discovery.call_args[0][0]
    assert len(discovery_arg) == 1
    assert 'blockchain' in discovery_arg[0].get('title', '').lower()

    # Verify that missing context fails safely
    (workflow_dir / "selected_funding_call.json").unlink()
    with pytest.raises(FileNotFoundError, match="is missing, so mission gate cannot be applied"):
        refresh_intelligence_views(call_root)

    # Verify that malformed context fails safely
    (workflow_dir / "selected_funding_call.json").write_text(json.dumps({"wrong": "format"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="failed to reconstruct mission context"):
        refresh_intelligence_views(call_root)


def test_mission_context_parity(tmp_path):
    from run_pipeline import build__selected_call_context
    from core.normalization import resolve_refresh_context
    from core.mission_gate import relevance

    call_id = "CALL-TEST-PARITY"
    run_root = tmp_path / "outputs" / "run_parity"
    call_root = run_root / "calls" / call_id
    workflow_dir = call_root / "workflow"
    workflow_dir.mkdir(parents=True)

    selected_call = {
        "selected": {
            "call_id": call_id,
            "title": "Quantum Networking",
            "organization": "QOrg",
            "research_area": "DePIN",
            "research_context": {
                "funding_call_id": call_id,
                "domain": "DePIN",
                "technology_themes": ["quantum", "entanglement"],
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
    (workflow_dir / "selected_funding_call.json").write_text(json.dumps(selected_call), encoding="utf-8")

    # 1. run_pipeline.py context
    run_pipeline_context = build__selected_call_context(selected_call["selected"], call_id)

    # 2. refresh context
    _, _, filter_func = resolve_refresh_context(call_root)

    record_pass = {
        "title": "Quantum entanglement sharing",
        "research_area": "DePIN",
        "funding_call_id": call_id,
        "content": "Quantum entanglement",
        "problem_statement": "Quantum entanglement networks.",
        "tokens": ["quantum", "entanglement", "networks"]
    }

    record_fail = {
        "title": "Local Unrelated Stuff",
        "research_area": "DePIN",
        "funding_call_id": call_id,
        "content": "Classic networking",
        "problem_statement": "Classic networking.",
        "tokens": ["classic", "networking"]
    }

    # test parity
    pass_gate1 = relevance(record_pass, run_pipeline_context)
    pass_gate2 = filter_func(record_pass)
    assert pass_gate1["passed"] == pass_gate2
    assert pass_gate2 is True

    fail_gate1 = relevance(record_fail, run_pipeline_context)
    fail_gate2 = filter_func(record_fail)
    assert fail_gate1["passed"] == fail_gate2
    assert fail_gate2 is False
