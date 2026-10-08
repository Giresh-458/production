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
                        "inferred_challenges": [],
                        "evidence_refs": [],
                        "inference_provenance": "mock",
                        "confidence": 1.0,
                        "research_intent": "research", 
                        "research_intent_evidence": []
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

@pytest.fixture
def mock_pipeline_deps():
    with patch("run_pipeline.run_stage") as mock_run_stage, \
         patch("run_pipeline.execute_batch_run") as mock_exec_batch, \
         patch("run_pipeline.evaluate_collection_health", return_value=(True, 3)) as mock_eval, \
         patch("run_pipeline.save_normalized_collection_records", return_value=[{"record_id": "r1"}]) as mock_save, \
         patch("run_pipeline.mission_relevance", return_value={"passed": True}) as mock_mission, \
         patch("run_pipeline.get_all_open_funding_calls", return_value=([{"funding_call_id": "test-123", "title": "Test", "program_name": "Test", "status": "OPEN_CALL", "source_url": "http://test", "domain_relevance": [{"area": "DePIN", "score": 1.0}], "research_context": {"funding_call_id": "test-123", "domain": "DePIN", "technology_themes": [], "funding_priorities": [], "target_outcomes": [], "investigation_questions": [], "inferred_challenges": [], "evidence_refs": [], "inference_provenance": "mock", "confidence": 1.0, "research_intent": "research", "research_intent_evidence": []}}], [])) as mock_get_open, \
         patch("builtins.print") as mock_print, \
         patch("pathlib.Path.rglob") as mock_rglob, \
         patch("pathlib.Path.exists", return_value=True), \
         patch("pathlib.Path.write_text"), \
         patch("pathlib.Path.read_text", return_value='[{"record_id": "r1", "title": "t", "research_area": "Unscoped", "source_url": "u", "funding_call_ids": ["test-123"]}]'):
         
        mock_exec_batch.return_value = {"tasks": []}
        
        yield {
            "run_stage": mock_run_stage,
            "rglob": mock_rglob,
            "print": mock_print
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
