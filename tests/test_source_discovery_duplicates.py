import pytest
import json
from unittest.mock import patch
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from run_pipeline import main
from core.agent_registry import DOWNSTREAM_FUNDING_CONTEXT_AGENTS

@patch('run_pipeline.run_stage')
@patch('run_pipeline.get_all_open_funding_calls')
@patch('run_pipeline.execute_batch_run')
@patch('run_pipeline.evaluate_collection_health')
@patch('run_pipeline.prepare_batch2_source_registry')
@patch('core.intelligence.save_source_discovery_candidates')
def test_source_discovery_duplicate_preservation(
    mock_save_discovery,
    mock_prepare_registry,
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
    mock_prepare_registry.return_value = {"agents_synced": sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS), "refresh_due_entries": 0}

    selected_call = {
        "selected": {
            "call_id": call_id,
            "status": "OPEN_CALL",
            "title": "Blockchain Networking",
            "organization": "TestOrg",
            "source_url": f"https://funding.example.org/calls/{call_id}",
            "application_url": f"https://funding.example.org/calls/{call_id}",
            "eligibility": "Eligible applicants: research universities and technology organizations may apply.",
            "research_area": "DePIN", "domain_relevance": [{"domain": "DePIN", "score": 1.0}],
            "research_context": {
                "funding_call_id": call_id, "research_intent": "research",
                "domain": "DePIN",
                "technology_themes": ["blockchain", "ledger"], "domain_relevance": [{"domain": "DePIN", "score": 1.0}],
                "funding_priorities": [],
                "target_outcomes": [],
                "investigation_questions": [],
                "inferred_challenges": [f"Source-supported research challenge for {call_id}."],
                "evidence_refs": [f"https://funding.example.org/calls/{call_id}"],
                "inference_provenance": ["source_grounded_problem_extraction"],
                "research_intent_evidence": ["The official call explicitly describes a research challenge."],
                "confidence": "high"
            }
        },
        "rejected_alternatives": []
    }
    mock_get_calls.return_value = ([selected_call["selected"]], [])

    def fake_run_stage(agent, *args, **kwargs):
        if agent == "funding":
            return {"status": "success", "outputs": [selected_call["selected"]], "errors": []}
        return {"status": "success", "outputs_count": 1, "outputs": []}

    mock_run_stage.side_effect = fake_run_stage

    def fake_execute_batch(**kwargs):
        root = Path(kwargs["base_input_data"]["pipeline_run_dir"])
        run_id = kwargs["base_input_data"]["run_id"]
        intermediate_dir = root / "intermediate" / "literature"
        intermediate_dir.mkdir(parents=True, exist_ok=True)

        # 1. Accepted evidence (matches 'blockchain' & 'ledger')
        write_test_intermediate_artifact(
            root,
            run_id=run_id,
            funding_call_id=call_id,
            title="Blockchain basics",
            source_url="https://example.org/accepted",
            content="Blockchain ledger systems.\n\nBlockchain ledger systems.\n\nhttps://example.org/link-acc",
            research_area="DePIN",
            evidence_snippets=["Blockchain ledger systems."],
            collected_at="2026-08-24T10:00:00+00:00",
        )

        # 2. Rejected evidence
        write_test_intermediate_artifact(
            root,
            run_id=run_id,
            funding_call_id=call_id,
            title="Nothing matches here",
            source_url="https://example.org/rejected",
            content="Trees and leaves.\n\nTrees and leaves.",
            research_area="DePIN",
            evidence_snippets=["Trees and leaves."],
            collected_at="2026-08-24T10:00:01+00:00",
        )

        write_test_intermediate_artifact(
            root,
            run_id=run_id,
            funding_call_id=call_id,
            title="Missing title keywords",
            source_url="https://example.org/conflict",
            content="A completely unrelated problem statement.\n\nShared conflict ledger snippet.\n\nhttps://example.org/link-confB",
            research_area="Unscoped",
            evidence_snippets=["Shared conflict ledger snippet."],
            collected_at="2026-08-24T10:00:02+00:00",
        )

        write_test_intermediate_artifact(
            root,
            run_id=run_id,
            funding_call_id=call_id,
            title="Blockchain conflict resolution",
            source_url="https://example.org/conflict",
            content="Blockchain ledger systems.\n\nShared conflict ledger snippet.\n\nhttps://example.org/link-confA",
            research_area="DePIN",
            evidence_snippets=["Shared conflict ledger snippet."],
            collected_at="2026-08-24T10:00:03+00:00",
        )

        # These fixtures represent evidence collected in this pipeline run.
        # Production normalization deliberately excludes records from other runs.
        from tests.batch2_test_helpers import write_test_collector_artifacts, write_test_intermediate_artifact
        write_test_collector_artifacts(root, run_id, kwargs["agent_names"])
        for artifact in intermediate_dir.glob("*.md"):
            artifact_text = artifact.read_text(encoding="utf-8")
            marker = f"- funding_call_id: {call_id}\n"
            if marker in artifact_text and "- run_id:" not in artifact_text:
                artifact_text = artifact_text.replace(
                    marker, marker + f"- run_id: {run_id}\n", 1
                )
                artifact.write_text(artifact_text, encoding="utf-8")

        return {"tasks": [
            {"agent_name": agent, "status": "success", "outputs_count": len(list((root / "intermediate" / agent).glob("*.md")))}
            for agent in sorted(set(DOWNSTREAM_FUNDING_CONTEXT_AGENTS))
        ]}

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
