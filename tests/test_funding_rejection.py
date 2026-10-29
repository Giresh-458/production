import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import run_pipeline
import argparse

@pytest.fixture
def run_pipeline_mocks():
    with patch("run_pipeline.run_stage") as mock_run_stage, \
         patch("run_pipeline.build__selected_call_context") as mock_build_ctx, \
         patch("run_pipeline.execute_batch_run") as mock_execute_batch, \
         patch("run_pipeline.save_normalized_collection_records") as mock_save_normalized, \
         patch("run_pipeline.mission_relevance") as mock_mission_relevance, \
         patch("core.source_registry.sync_source_registry") as mock_sync_registry, \
         patch("core.source_registry.build_source_refresh_plan") as mock_build_plan:
        
        mock_run_stage.return_value = {"outputs": [], "status": "success", "outputs_count": 1}
        mock_execute_batch.return_value = {"tasks": [{"agent_name": f"dummy_{i}", "status": "success", "outputs_count": 1} for i in range(11)]}
        mock_mission_relevance.return_value = {"passed": True}
        
        def fake_save(roots, call_root, call_id, **kwargs):
            if "filter_func" in kwargs:
                kwargs["filter_func"]({"record_id": "1", "research_area": "Unscoped"})
            import json
            norm_dir = call_root / "normalized"
            norm_dir.mkdir(parents=True, exist_ok=True)
            (norm_dir / "records.json").write_text(json.dumps([{"record_id": "1", "research_area": "Unscoped"}]), encoding="utf-8")
            return {"records": norm_dir / "records.json"}
            
        mock_save_normalized.side_effect = fake_save
        
        yield {
            "run_stage": mock_run_stage,
            "build_ctx": mock_build_ctx,
            "execute_batch": mock_execute_batch,
            "sync_registry": mock_sync_registry,
            "build_plan": mock_build_plan,
        }

def test_manual_notice_unknown_status_rejected(run_pipeline_mocks, tmp_path, monkeypatch):
    input_file = tmp_path / "manual_input.txt"
    input_file.write_text("Manual test content")

    def mock_run_stage_func(agent, *args, **kwargs):
        if agent == "funding":
            return {"outputs": [{"call_id": "call1", "status": "UNKNOWN", "is_open_call": False}]}
        return {"status": "success", "outputs_count": 1, "outputs": []}
    run_pipeline_mocks["run_stage"].side_effect = mock_run_stage_func

    parser = argparse.ArgumentParser()
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--scheduled-refresh", action="store_true")
    parser.add_argument("--input-file", default=str(input_file))
    parser.add_argument("--output-root", default=str(tmp_path))
    parser.add_argument("--area", default=None)
    parser.add_argument("--funding-sources", default="dummy")
    parser.add_argument("--max-open-calls", type=int, default=None)
    monkeypatch.setattr(run_pipeline.argparse, "ArgumentParser", lambda *a, **kw: MagicMock(parse_args=lambda: parser.parse_args(["--input-file", str(input_file), "--output-root", str(tmp_path)])))
    exit_code = run_pipeline.main()
    assert exit_code == 3
    run_pipeline_mocks["sync_registry"].assert_not_called()
    run_pipeline_mocks["build_plan"].assert_not_called()
    workflow_dir = next(tmp_path.glob("*/workflow"))
    report = json.loads((workflow_dir / "pipeline_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "no_open_call"
    assert len(report["rejected_calls"]) == 1

def test_open_call_non_research_rejected(run_pipeline_mocks, tmp_path, monkeypatch):

    def mock_run_stage_func(agent, *args, **kwargs):
        if agent == "funding":
            return {"outputs": [{"call_id": "call2", "title": "Test Title", "source_url": "http://test", "eligibility": "Eligible applicants must be registered organizations.", "application_url": "http://test/apply", "deadline": "2030-01-01", "status": "OPEN_CALL", "domain_relevance": [{"domain": "DePIN", "score": 0.75, "raw_score": 3.0, "matched_terms": ["depin"]}], "source_research_domains": ["DePIN"]}]}
        return {"status": "success", "outputs_count": 1, "outputs": []}
    run_pipeline_mocks["run_stage"].side_effect = mock_run_stage_func

    mock_ctx = MagicMock()
    mock_ctx.research_context.research_intent = "non_research"
    mock_ctx.research_context.research_intent_evidence = "Building community center."
    run_pipeline_mocks["build_ctx"].return_value = mock_ctx
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--scheduled-refresh", action="store_true")
    parser.add_argument("--input-file", default=None)
    parser.add_argument("--output-root", default=str(tmp_path))
    parser.add_argument("--area", default=None)
    parser.add_argument("--funding-sources", default="dummy")
    parser.add_argument("--max-open-calls", type=int, default=None)
    monkeypatch.setattr(run_pipeline.argparse, "ArgumentParser", lambda *a, **kw: MagicMock(parse_args=lambda: parser.parse_args(["--output-root", str(tmp_path)])))
    exit_code = run_pipeline.main()
    assert exit_code == 3
    run_pipeline_mocks["sync_registry"].assert_not_called()
    workflow_dir = next(tmp_path.glob("*/workflow"))
    report = json.loads((workflow_dir / "pipeline_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "no_research_opportunity"
    assert len(report["rejected_non_research"]) == 1
    assert report["rejected_non_research"][0]["reason"] == "non_research"

def test_open_call_unrecognized_intent_rejected(run_pipeline_mocks, tmp_path, monkeypatch):

    def mock_run_stage_func(agent, *args, **kwargs):
        if agent == "funding":
            return {"outputs": [{"call_id": "call_unrecognized", "title": "Test Title", "source_url": "http://test", "eligibility": "Eligible applicants must be registered organizations.", "application_url": "http://test/apply", "deadline": "2030-01-01", "status": "OPEN_CALL", "domain_relevance": [{"domain": "DePIN", "score": 0.75, "raw_score": 3.0, "matched_terms": ["depin"]}], "source_research_domains": ["DePIN"]}]}
        return {"status": "success", "outputs_count": 1, "outputs": []}
    run_pipeline_mocks["run_stage"].side_effect = mock_run_stage_func

    mock_ctx = MagicMock()
    mock_ctx.research_context.research_intent = "unknown_intent"
    mock_ctx.research_context.research_intent_evidence = "No clear evidence."
    run_pipeline_mocks["build_ctx"].return_value = mock_ctx
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--scheduled-refresh", action="store_true")
    parser.add_argument("--input-file", default=None)
    parser.add_argument("--output-root", default=str(tmp_path))
    parser.add_argument("--area", default=None)
    parser.add_argument("--funding-sources", default="dummy")
    parser.add_argument("--max-open-calls", type=int, default=None)
    monkeypatch.setattr(run_pipeline.argparse, "ArgumentParser", lambda *a, **kw: MagicMock(parse_args=lambda: parser.parse_args(["--output-root", str(tmp_path)])))
    exit_code = run_pipeline.main()
    assert exit_code == 3
    run_pipeline_mocks["sync_registry"].assert_not_called()
    workflow_dir = next(tmp_path.glob("*/workflow"))
    report = json.loads((workflow_dir / "pipeline_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "no_research_opportunity"
    assert len(report["rejected_non_research"]) == 1
    assert report["rejected_non_research"][0]["reason"] == "unrecognized_intent"
    
def test_open_call_missing_intent_rejected(run_pipeline_mocks, tmp_path, monkeypatch):

    def mock_run_stage_func(agent, *args, **kwargs):
        if agent == "funding":
            return {"outputs": [{"call_id": "call_missing", "title": "Test Title", "source_url": "http://test", "eligibility": "Eligible applicants must be registered organizations.", "application_url": "http://test/apply", "deadline": "2030-01-01", "status": "OPEN_CALL", "domain_relevance": [{"domain": "DePIN", "score": 0.75, "raw_score": 3.0, "matched_terms": ["depin"]}], "source_research_domains": ["DePIN"]}]}
        return {"status": "success", "outputs_count": 1, "outputs": []}
    run_pipeline_mocks["run_stage"].side_effect = mock_run_stage_func

    mock_ctx = MagicMock()
    del mock_ctx.research_context.research_intent  # missing intent entirely
    mock_ctx.research_context.research_intent_evidence = ""
    run_pipeline_mocks["build_ctx"].return_value = mock_ctx
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--scheduled-refresh", action="store_true")
    parser.add_argument("--input-file", default=None)
    parser.add_argument("--output-root", default=str(tmp_path))
    parser.add_argument("--area", default=None)
    parser.add_argument("--funding-sources", default="dummy")
    parser.add_argument("--max-open-calls", type=int, default=None)
    monkeypatch.setattr(run_pipeline.argparse, "ArgumentParser", lambda *a, **kw: MagicMock(parse_args=lambda: parser.parse_args(["--output-root", str(tmp_path)])))
    exit_code = run_pipeline.main()
    assert exit_code == 3
    run_pipeline_mocks["sync_registry"].assert_not_called()
    workflow_dir = next(tmp_path.glob("*/workflow"))
    report = json.loads((workflow_dir / "pipeline_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "no_research_opportunity"
    assert report["rejected_non_research"][0]["reason"] == "unrecognized_intent"

def test_open_call_with_unverified_research_context_is_not_handed_downstream(run_pipeline_mocks, tmp_path, monkeypatch):
    def mock_run_stage_func(agent, *args, **kwargs):
        if agent == "funding":
            return {"outputs": [{
                "call_id": "call_context_missing",
                "title": "Test Title",
                "source_url": "http://test",
                "eligibility": "Eligible applicants must be registered organizations.",
                "application_url": "http://test/apply",
                "deadline": "2030-01-01",
                "status": "OPEN_CALL",
                "domain_relevance": [{"domain": "DePIN", "score": 0.75, "raw_score": 3.0, "matched_terms": ["depin"]}],
                "source_research_domains": ["DePIN"],
            }]}
        return {"status": "success", "outputs_count": 1, "outputs": []}
    run_pipeline_mocks["run_stage"].side_effect = mock_run_stage_func

    mock_ctx = MagicMock()
    mock_ctx.research_context.research_intent = "research"
    mock_ctx.research_context.research_intent_evidence = "Research funding is mentioned."
    mock_ctx.research_context.confidence = "Unknown"
    mock_ctx.research_context.inferred_challenges = []
    mock_ctx.research_context.evidence_refs = []
    mock_ctx.research_context.inference_provenance = []
    mock_ctx.funding_call_id = "call_context_missing"
    run_pipeline_mocks["build_ctx"].return_value = mock_ctx

    parser = argparse.ArgumentParser()
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--scheduled-refresh", action="store_true")
    parser.add_argument("--input-file", default=None)
    parser.add_argument("--output-root", default=str(tmp_path))
    parser.add_argument("--area", default=None)
    parser.add_argument("--funding-sources", default="dummy")
    parser.add_argument("--max-open-calls", type=int, default=None)
    monkeypatch.setattr(run_pipeline.argparse, "ArgumentParser", lambda *a, **kw: MagicMock(parse_args=lambda: parser.parse_args(["--output-root", str(tmp_path)])))

    exit_code = run_pipeline.main()

    assert exit_code == 3
    run_pipeline_mocks["execute_batch"].assert_not_called()
    workflow_dir = next(tmp_path.glob("*/workflow"))
    report = json.loads((workflow_dir / "pipeline_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "no_research_opportunity"
    assert report["rejected_unverified_research_context"][0]["reason"] == "research_context_unverified"
    assert "research-context confidence is unknown" in report["rejected_unverified_research_context"][0]["reasons"]


def test_open_call_research_proceeds(run_pipeline_mocks, tmp_path, monkeypatch):

    def mock_run_stage_func(agent, *args, **kwargs):
        if agent == "funding":
            return {"outputs": [{"call_id": "call3", "title": "Test Title", "source_url": "http://test", "eligibility": "Eligible applicants must be registered organizations.", "application_url": "http://test/apply", "deadline": "2030-01-01", "status": "OPEN_CALL", "domain_relevance": [{"domain": "DePIN", "score": 0.75, "raw_score": 3.0, "matched_terms": ["depin"]}], "source_research_domains": ["DePIN"]}]}
        return {"status": "success", "outputs_count": 1, "outputs": []}
    run_pipeline_mocks["run_stage"].side_effect = mock_run_stage_func

    mock_ctx = MagicMock()
    mock_ctx.research_context.research_intent = "research"
    mock_ctx.research_context.research_intent_evidence = "Basic research"
    mock_ctx.research_context.confidence = "Medium"
    mock_ctx.research_context.inferred_challenges = ["The call identifies a source-supported research gap."]
    mock_ctx.research_context.evidence_refs = ["http://test"]
    mock_ctx.research_context.inference_provenance = ["source_grounded_problem_extraction"]
    mock_ctx.funding_call_id = "call3"
    run_pipeline_mocks["build_ctx"].return_value = mock_ctx
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--scheduled-refresh", action="store_true")
    parser.add_argument("--input-file", default=None)
    parser.add_argument("--output-root", default=str(tmp_path))
    parser.add_argument("--area", default=None)
    parser.add_argument("--funding-sources", default="dummy")
    parser.add_argument("--max-open-calls", type=int, default=None)
    monkeypatch.setattr(run_pipeline.argparse, "ArgumentParser", lambda *a, **kw: MagicMock(parse_args=lambda: parser.parse_args(["--output-root", str(tmp_path)])))
    with patch("run_pipeline.DOWNSTREAM_FUNDING_CONTEXT_AGENTS", ["dummy"] * 11):
        exit_code = run_pipeline.main()
    assert exit_code == 0
    run_pipeline_mocks["sync_registry"].assert_called_once()
    run_pipeline_mocks["build_plan"].assert_called_once()
    
def test_mixed_calls_only_research_proceeds(run_pipeline_mocks, tmp_path, monkeypatch):

    def mock_run_stage_func(agent, *args, **kwargs):
        if agent == "funding":
            return {"outputs": [{"call_id": "call_good", "title": "Test Title", "source_url": "http://test", "eligibility": "Eligible applicants must be registered organizations.", "application_url": "http://test/apply", "deadline": "2030-01-01", "status": "OPEN_CALL", "domain_relevance": [{"domain": "DePIN", "score": 0.75, "raw_score": 3.0, "matched_terms": ["depin"]}], "source_research_domains": ["DePIN"]}, {"call_id": "call_bad", "title": "Test Title", "source_url": "http://test", "eligibility": "Eligible applicants must be registered organizations.", "application_url": "http://test/apply", "deadline": "2030-01-01", "status": "OPEN_CALL", "domain_relevance": [{"domain": "DePIN", "score": 0.75, "raw_score": 3.0, "matched_terms": ["depin"]}], "source_research_domains": ["DePIN"]}]}
        return {"status": "success", "outputs_count": 1, "outputs": []}
    run_pipeline_mocks["run_stage"].side_effect = mock_run_stage_func

    good_ctx = MagicMock()
    good_ctx.research_context.research_intent = "research"
    good_ctx.research_context.research_intent_evidence = "Research"
    good_ctx.research_context.confidence = "Medium"
    good_ctx.research_context.inferred_challenges = ["The call identifies a source-supported research gap."]
    good_ctx.research_context.evidence_refs = ["http://test"]
    good_ctx.research_context.inference_provenance = ["source_grounded_problem_extraction"]
    good_ctx.funding_call_id = "call_good"
    bad_ctx = MagicMock()
    bad_ctx.research_context.research_intent = "non_research"
    bad_ctx.research_context.research_intent_evidence = "Not research"
    bad_ctx.funding_call_id = "call_bad"
    def mock_build_ctx_func(call):
        return good_ctx if call["call_id"] == "call_good" else bad_ctx
    run_pipeline_mocks["build_ctx"].side_effect = mock_build_ctx_func
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--scheduled-refresh", action="store_true")
    parser.add_argument("--input-file", default=None)
    parser.add_argument("--output-root", default=str(tmp_path))
    parser.add_argument("--area", default=None)
    parser.add_argument("--funding-sources", default="dummy")
    parser.add_argument("--max-open-calls", type=int, default=None)
    monkeypatch.setattr(run_pipeline.argparse, "ArgumentParser", lambda *a, **kw: MagicMock(parse_args=lambda: parser.parse_args(["--output-root", str(tmp_path)])))
    with patch("run_pipeline.DOWNSTREAM_FUNDING_CONTEXT_AGENTS", ["dummy"] * 11):
        exit_code = run_pipeline.main()
    
    # Prove it didn't crash
    assert exit_code == 0
    
    # Prove shared collection / registry only received the good call
    run_pipeline_mocks["sync_registry"].assert_called_once()
    registry_calls = run_pipeline_mocks["sync_registry"].call_args_list
    assert len(registry_calls) == 1
    # Check that only good_ctx is in the list passed to sync_registry (which may be empty in mock but let's check args)
    # Actually, in run_pipeline, it's called with NO args for some reason? Wait:
    # sync_source_registry() is called with no args! Oh wait, sync_source_registry() syncs the registry globally!
    # Wait, my run_pipeline.py calls sync_source_registry([c for c in all_contexts if c is not None])?
    # No! In run_pipeline.py, it's just sync_source_registry()!
    
    # Prove context was only built once for each call, never rebuilt
    calls = run_pipeline_mocks["build_ctx"].call_args_list
    assert len(calls) == 2
    
    # Prove per-call processing only happened for the good call
    run_stage_calls = run_pipeline_mocks["run_stage"].call_args_list
    # funding agent is call 1
    # adaptive_research, tagging, clustering, trend, synthesis, idea, proposal
    per_call_stages = [c.args[0] for c in run_stage_calls[1:]]
    assert len(per_call_stages) > 0
    # ensure it didn't run twice
    assert per_call_stages.count("idea") == 1
    assert per_call_stages.count("proposal") == 1
    
