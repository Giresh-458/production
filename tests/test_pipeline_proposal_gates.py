import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import run_pipeline

def create_base_mock_stage(proposal_mock):
    def fake_run_stage(agent, mode, area, payload, context=None):
        if agent == "funding":
            return {
                "status": "success",
                "outputs": [{
                    "funding_call_id": "test-123",
                    "title": "Test",
                    "program_name": "Test",
                    "status": "OPEN_CALL",
                    "source_url": "http://test",
                    "domain_relevance": [{"area": "DePIN", "score": 1.0}],
                    "research_context": {
                        "funding_call_id": "test-123",
                        "domain": "DePIN",
                        "technology_themes": [],
                        "funding_priorities": [],
                        "target_outcomes": [],
                        "investigation_questions": [],
                        "inferred_challenges": ["The official call identifies a source-supported research challenge."],
                        "evidence_refs": ["http://test"],
                        "inference_provenance": ["source_grounded_problem_extraction"],
                        "confidence": "Medium",
                        "research_intent": "research",
                        "research_intent_evidence": ["The call explicitly supports research and technical validation."]
                    }
                }],
                "errors": []
            }
        elif agent == "synthesis":
            return {"status": "success", "outputs_count": 1}
        elif agent == "idea":
            return {"status": "success"}
        elif agent == "proposal":
            return proposal_mock
        return {"status": "success"}
    return fake_run_stage


def mock_save(*args, **kwargs):
    call_root = args[1]
    call_id = args[2]
    run_id = kwargs.get("target_run_id")
    norm_dir = call_root / "normalized"
    norm_dir.mkdir(parents=True, exist_ok=True)
    (call_root / "proposals").mkdir(parents=True, exist_ok=True)
    record = {
        "record_id": "r1",
        "evidence_id": "dummy",
        "title": "Controlled proposal-gate evidence",
        "research_area": "Unscoped",
        "source_url": "https://example.org/evidence",
        "funding_call_ids": [call_id],
        "funding_call_id": call_id,
        "run_id": run_id,
        "normalization_status": "NORMALIZED",
    }
    if kwargs.get("filter_func"):
        kwargs["filter_func"](record)
    (norm_dir / "records.json").write_text(json.dumps([record]), encoding="utf-8")
    (norm_dir / "by_area.json").write_text(json.dumps({"Unscoped": [record]}), encoding="utf-8")
    (norm_dir / "by_layer.json").write_text(json.dumps({"Literature": [record]}), encoding="utf-8")
    (norm_dir / "input_manifest.json").write_text("{}", encoding="utf-8")
    (norm_dir / "errors.json").write_text("[]", encoding="utf-8")
    (norm_dir / "skipped_artifacts.json").write_text("[]", encoding="utf-8")
    (norm_dir / "evidence_ledger.db").touch()
    return {
        "records": norm_dir / "records.json",
        "by_area": norm_dir / "by_area.json",
        "by_layer": norm_dir / "by_layer.json",
        "errors": norm_dir / "errors.json",
        "skipped_artifacts": norm_dir / "skipped_artifacts.json",
    }

@pytest.fixture
def mock_pipeline_deps():
    with patch("run_pipeline.run_stage") as mock_run_stage, \
         patch("run_pipeline.execute_batch_run") as mock_exec_batch, \
         patch("run_pipeline.evaluate_collection_health", return_value=(True, 3)) as mock_eval, \
         patch("run_pipeline.save_normalized_collection_records", side_effect=mock_save) as mock_save_patch, \
         patch("run_pipeline.mission_relevance", return_value={"passed": True}) as mock_mission, \
         patch("run_pipeline.get_all_open_funding_calls", return_value=([{"funding_call_id": "test-123", "title": "Test", "program_name": "Test", "status": "OPEN_CALL", "source_url": "http://test", "domain_relevance": [{"area": "DePIN", "score": 1.0}], "research_context": {"funding_call_id": "test-123", "domain": "DePIN", "technology_themes": [], "funding_priorities": [], "target_outcomes": [], "investigation_questions": [], "inferred_challenges": ["The official call identifies a source-supported research challenge."], "evidence_refs": ["http://test"], "inference_provenance": ["source_grounded_problem_extraction"], "confidence": "Medium", "research_intent": "research", "research_intent_evidence": ["The call explicitly supports research and technical validation."]}}], [])) as mock_get_open, \
         patch("builtins.print") as mock_print, \
         patch("pathlib.Path.rglob") as mock_rglob, \
         patch("core.source_registry.sync_source_registry") as mock_sync_registry, \
         patch("core.source_registry.build_source_refresh_plan") as mock_build_plan:

        mock_exec_batch.return_value = {"tasks": []}

        yield {
            "run_stage": mock_run_stage,
            "rglob": mock_rglob,
            "print": mock_print,
            "sync_registry": mock_sync_registry,
            "build_plan": mock_build_plan,
        }

def test_pipeline_completed_no_proposal(tmp_path, mock_pipeline_deps):
    mock_pipeline_deps["run_stage"].side_effect = create_base_mock_stage(
        {"status": "partial_success", "outputs": [], "errors": [], "warnings": ["Rejected idea"]}
    )
    mock_pipeline_deps["rglob"].return_value = []

    with patch("sys.argv", ["run_pipeline.py", "--output-root", str(tmp_path)]):
        exit_code = run_pipeline.main()

    assert exit_code == 0
    mock_pipeline_deps["print"].assert_any_call("⚠️ Proposal generation completed but all candidates were rejected by quality gates.")
    mock_pipeline_deps["print"].assert_any_call("⚠️ Zero proposals were generated because no candidate satisfied the proposal quality gate.")

def test_pipeline_failed_proposal(tmp_path, mock_pipeline_deps):
    mock_pipeline_deps["run_stage"].side_effect = create_base_mock_stage(
        {"status": "partial_success", "outputs": [], "errors": ["Runtime exception"]}
    )
    mock_pipeline_deps["rglob"].return_value = []

    with patch("sys.argv", ["run_pipeline.py", "--output-root", str(tmp_path)]):
        exit_code = run_pipeline.main()

    assert exit_code == 5
    mock_pipeline_deps["print"].assert_any_call("❌ Proposal generation failed internally and produced no output.")

def test_pipeline_completed_with_proposal(tmp_path, mock_pipeline_deps):
    mock_pipeline_deps["run_stage"].side_effect = create_base_mock_stage(
        {"status": "success", "outputs": ["proposal.md"], "errors": []}
    )
    mock_pipeline_deps["rglob"].return_value = [Path("proposal.md")]

    with patch("sys.argv", ["run_pipeline.py", "--output-root", str(tmp_path)]):
        exit_code = run_pipeline.main()

    assert exit_code == 0
    mock_pipeline_deps["print"].assert_any_call("Total proposals generated: 1")

def test_pipeline_unresolved_idea_stops_proposal(tmp_path, mock_pipeline_deps):
    mock_pipeline_deps["run_stage"].side_effect = create_base_mock_stage(
        {"status": "partial_success", "outputs": [], "errors": [], "warnings": ["Rejected idea"]}
    )
    # If idea is unresolved, idea stage might return partial_success but proposal rejects it.
    mock_pipeline_deps["rglob"].return_value = []

    with patch("sys.argv", ["run_pipeline.py", "--output-root", str(tmp_path)]):
        exit_code = run_pipeline.main()

    assert exit_code == 0
    mock_pipeline_deps["print"].assert_any_call("⚠️ Zero proposals were generated because no candidate satisfied the proposal quality gate.")


def test_pipeline_proposal_gates_does_not_mutate_default_registry(tmp_path, mock_pipeline_deps):
    from core.source_registry import SOURCE_REGISTRY_PATH
    initial_bytes = SOURCE_REGISTRY_PATH.read_bytes()
    initial_mtime = SOURCE_REGISTRY_PATH.stat().st_mtime_ns

    mock_pipeline_deps["run_stage"].side_effect = create_base_mock_stage(
        {"status": "success", "outputs": ["proposal.md"], "errors": []}
    )
    mock_pipeline_deps["rglob"].return_value = [Path("proposal.md")]

    with patch("sys.argv", ["run_pipeline.py", "--output-root", str(tmp_path)]):
        exit_code = run_pipeline.main()

    assert exit_code == 0
    assert mock_pipeline_deps["sync_registry"].call_count == 1
    assert mock_pipeline_deps["build_plan"].call_count == 1
    assert SOURCE_REGISTRY_PATH.stat().st_mtime_ns == initial_mtime
    assert SOURCE_REGISTRY_PATH.read_bytes() == initial_bytes
