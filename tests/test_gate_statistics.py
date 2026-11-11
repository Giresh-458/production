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
def test_duplicate_mission_gate_statistics(
    mock_prepare_registry,
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
    mock_prepare_registry.return_value = {"agents_synced": sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS), "refresh_due_entries": 0}

    selected_call = {
        "selected": {
            "call_id": call_id,
            "status": "OPEN_CALL",
            "title": "Quantum Networking",
            "organization": "QOrg",
            "source_url": f"https://funding.example.org/calls/{call_id}",
            "application_url": f"https://funding.example.org/calls/{call_id}",
            "eligibility": "Eligible applicants: research universities and technology organizations may apply.",
            "research_area": "DePIN", "domain_relevance": [{"domain": "DePIN", "score": 1.0}],
            "research_context": {
                "funding_call_id": call_id, "research_intent": "research",
                "domain": "DePIN",
                "technology_themes": ["quantum", "entanglement"], "domain_relevance": [{"domain": "DePIN", "score": 1.0}],
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

    # Mock execute_batch_run to create intermediate files
    def fake_execute_batch(**kwargs):
        root = Path(kwargs["base_input_data"]["pipeline_run_dir"])
        run_id = kwargs["base_input_data"]["run_id"]
        intermediate_dir = root / "intermediate" / "literature"
        intermediate_dir.mkdir(parents=True, exist_ok=True)

        # 1. Accepted evidence (matches 'quantum') - copy 1
        write_test_intermediate_artifact(
            root,
            run_id=run_id,
            funding_call_id=call_id,
            title="Quantum entanglement",
            source_url="https://example.org/quant",
            content="Quantum entanglement networks.\n\nQuantum entanglement networks.",
            research_area="DePIN",
            evidence_snippets=["Quantum entanglement networks."],
            collected_at="2026-08-24T10:00:00+00:00",
        )

        # 2. Accepted evidence (matches 'quantum') - copy 2 (DUPLICATE source and title -> same evidence_id)
        write_test_intermediate_artifact(
            root,
            run_id=run_id,
            funding_call_id=call_id,
            title="Quantum entanglement",
            source_url="https://example.org/quant",
            content="Quantum entanglement networks part 2.\n\nQuantum entanglement networks.",
            research_area="DePIN",
            evidence_snippets=["Quantum entanglement networks."],
            collected_at="2026-08-24T10:00:01+00:00",
        )

        # 3. Rejected evidence (no matching substantive keywords)
        write_test_intermediate_artifact(
            root,
            run_id=run_id,
            funding_call_id=call_id,
            title="Classic networking",
            source_url="https://example.org/class",
            content="Classic networking networks.\n\nClassic networking networks.",
            research_area="DePIN",
            evidence_snippets=["Classic networking networks."],
            collected_at="2026-08-24T10:00:02+00:00",
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
