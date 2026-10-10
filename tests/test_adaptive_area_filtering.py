import pytest
import json
from unittest.mock import patch, MagicMock, ANY
from pathlib import Path
import sys

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_pipeline import main
from core.intelligence import refresh_intelligence_views

@patch('run_pipeline.run_stage')
@patch('run_pipeline.get_all_open_funding_calls')
@patch('run_pipeline.save_normalized_collection_records')
@patch('run_pipeline.mission_relevance')
@patch('run_pipeline.execute_batch_run')
@patch('run_pipeline.evaluate_collection_health')
@patch('core.source_registry.sync_source_registry')
@patch('core.source_registry.build_source_refresh_plan')
def run_pipeline_with_mocked_areas(
    mock_build_source_refresh,
    mock_sync_registry,
    mock_evaluate_health,
    mock_execute_batch,
    mock_mission_relevance,
    mock_save_normalized,
    mock_get_calls,
    mock_run_stage,
    monkeypatch,
    tmp_path,
    accepted_records,
    originally_selected_area
):
    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--output-root", str(tmp_path)])

    mock_get_calls.return_value = ([{
        "call_id": "CALL-TEST",
        "title": "Test Call",
        "status": "OPEN_CALL",
        "domain_relevance": [{"domain": originally_selected_area, "score": 0.9, "raw_score": 10.0, "matched_terms": ["a", "b", "c"]}]
    }], [])

    mock_evaluate_health.return_value = (True, 11)

    def fake_save_normalized(roots, call_root, call_id):
        norm_dir = call_root / "normalized"
        norm_dir.mkdir(parents=True, exist_ok=True)
        (norm_dir / "records.json").write_text(json.dumps(accepted_records), encoding="utf-8")
        return {"records": norm_dir / "records.json"}

    mock_save_normalized.side_effect = fake_save_normalized
    mock_mission_relevance.return_value = {"passed": True}
    mock_run_stage.return_value = {"status": "success", "outputs_count": 1, "outputs": [{}]}

    exit_code = main()

    return exit_code, mock_run_stage.call_args_list


def test_adaptive_area_filtering_only_unscoped(monkeypatch, tmp_path):
    records = [{"record_id": "1", "research_area": "Unscoped"}]
    code, calls = run_pipeline_with_mocked_areas(
        monkeypatch=monkeypatch, tmp_path=tmp_path,
        accepted_records=records, originally_selected_area="DigitalHealthCPS"
    )
    assert code == 0
    # verify tagging/clustering/etc run with None (Unscoped)
    for call in calls:
        agent = call.args[0]
        if agent in ("tagging", "clustering", "trend", "synthesis", "idea", "proposal"):
            area = call.args[2]
            assert area is None, f"{agent} should run Unscoped but got {area}"

def test_adaptive_area_filtering_single_recognized(monkeypatch, tmp_path):
    records = [
        {"record_id": "1", "research_area": "DigitalHealthCPS"},
        {"record_id": "2", "research_area": "DigitalHealthCPS"}
    ]
    code, calls = run_pipeline_with_mocked_areas(
        monkeypatch=monkeypatch, tmp_path=tmp_path,
        accepted_records=records, originally_selected_area="DigitalHealthCPS"
    )
    assert code == 0
    for call in calls:
        agent = call.args[0]
        if agent in ("tagging", "clustering", "trend", "synthesis", "idea", "proposal"):
            area = call.args[2]
            assert area == "DigitalHealthCPS", f"{agent} should run DigitalHealthCPS but got {area}"

def test_adaptive_area_filtering_mixed_areas(monkeypatch, tmp_path):
    records = [
        {"record_id": "1", "research_area": "DigitalHealthCPS"},
        {"record_id": "2", "research_area": "DID"}
    ]
    code, calls = run_pipeline_with_mocked_areas(
        monkeypatch=monkeypatch, tmp_path=tmp_path,
        accepted_records=records, originally_selected_area="DigitalHealthCPS"
    )
    assert code == 0
    for call in calls:
        agent = call.args[0]
        if agent in ("tagging", "clustering", "trend", "synthesis", "idea", "proposal"):
            area = call.args[2]
            assert area is None, f"{agent} should run Unscoped but got {area}"

def test_adaptive_area_filtering_empty(monkeypatch, tmp_path):
    code, calls = run_pipeline_with_mocked_areas(
        monkeypatch=monkeypatch, tmp_path=tmp_path,
        accepted_records=[], originally_selected_area="DigitalHealthCPS"
    )
    # The pipeline should handle empty records safely (code 4 or 0 depending on where it bails)
    # Actually, pipeline exits early with max(exit_code, 4) if no records or continues?
    # It says: "if not records: print('No evidence...'), continue"
    # So exit_code might be 0 but it won't call tagging.
    assert code == 0
    for call in calls:
        agent = call.args[0]
        assert agent not in ("tagging", "clustering", "trend", "synthesis", "idea", "proposal")


@patch('core.intelligence.save_normalized_collection_records')
@patch('core.intelligence.build_normalized_collection_records')
@patch('core.intelligence.save_source_discovery_candidates')
@patch('core.intelligence.build_source_review_queue')
@patch('core.intelligence.ensure_recursive_review_log')
@patch('core.intelligence.export_source_health_summary')
@patch('core.intelligence.build_structured_output_index')
@patch('core.intelligence.load_manifest_entries')
@patch('core.intelligence.load_source_registry_entries')
@patch('core.intelligence.build_problem_ranking_index')
@patch('core.intelligence.build_promotion_index')
@patch('core.intelligence.build_promotion_summary')
@patch('core.intelligence.detect_evidence_relationships')
@patch('core.intelligence.build_problem_clusters')
@patch('core.intelligence.build_batch_workflow_status')
@patch('core.intelligence.build_operational_views')
@patch('core.intelligence.generate_and_save_quality_metrics')
def test_refresh_intelligence_views_call_specific(
    mock_metrics, mock_op_views, mock_status, mock_clusters, mock_dedup,
    mock_promo_sum, mock_promo_idx, mock_rank_idx, mock_load_registry,
    mock_load_manifest, mock_build_structured, mock_export_source,
    mock_ensure_recursive, mock_build_review, mock_save_discovery,
    mock_build_normalized, mock_save_normalized, tmp_path
):
    dummy_file = tmp_path / "dummy_records.json"
    dummy_file.write_text("[]", encoding="utf-8")
    mock_save_normalized.return_value = {"records": dummy_file, "by_area": Path("b"), "by_layer": Path("c")}
    mock_build_normalized.return_value = []
    mock_save_discovery.return_value = MagicMock(discovery_path="d")
    mock_build_review.return_value = MagicMock(review_queue_path="e")
    mock_ensure_recursive.return_value = "f"

    mock_export_source_inst = MagicMock()
    mock_export_source_inst.summary_path = "g"
    mock_export_source.return_value = mock_export_source_inst

    mock_build_structured.return_value = []
    mock_load_manifest.return_value = []
    mock_load_registry.return_value = []
    mock_rank_idx.return_value = []
    mock_promo_idx.return_value = {}
    mock_promo_sum.return_value = {}
    mock_dedup.return_value = {}
    mock_clusters.return_value = []
    mock_status.return_value = {}
    mock_op_views.return_value = {}
    mock_metrics.return_value = Path("h")

    # Call-specific root
    outputs_root = tmp_path / "outputs" / "pipeline_run" / "run1" / "calls" / "CALL-123"
    workflow_dir = outputs_root / "workflow"
    workflow_dir.mkdir(parents=True)
    import json
    (workflow_dir / "selected_funding_call.json").write_text(json.dumps({"selected": {"title": "Test", "research_area": "Test"}}), encoding="utf-8")

    refresh_intelligence_views(outputs_root)

    expected_intermediate = [
        tmp_path / "outputs" / "pipeline_run" / "run1" / "intermediate",
        outputs_root / "intermediate"
    ]

    mock_save_normalized.assert_called_once_with(expected_intermediate, outputs_root, "CALL-123", filter_func=ANY)
    mock_build_normalized.assert_called_once_with(expected_intermediate, "CALL-123")


@patch('core.intelligence.save_normalized_collection_records')
@patch('core.intelligence.build_normalized_collection_records')
@patch('core.intelligence.save_source_discovery_candidates')
@patch('core.intelligence.build_source_review_queue')
@patch('core.intelligence.ensure_recursive_review_log')
@patch('core.intelligence.export_source_health_summary')
@patch('core.intelligence.build_structured_output_index')
@patch('core.intelligence.load_manifest_entries')
@patch('core.intelligence.load_source_registry_entries')
@patch('core.intelligence.build_problem_ranking_index')
@patch('core.intelligence.build_promotion_index')
@patch('core.intelligence.build_promotion_summary')
@patch('core.intelligence.detect_evidence_relationships')
@patch('core.intelligence.build_problem_clusters')
@patch('core.intelligence.build_batch_workflow_status')
@patch('core.intelligence.build_operational_views')
@patch('core.intelligence.generate_and_save_quality_metrics')
def test_refresh_intelligence_views_run_level(
    mock_metrics, mock_op_views, mock_status, mock_clusters, mock_dedup,
    mock_promo_sum, mock_promo_idx, mock_rank_idx, mock_load_registry,
    mock_load_manifest, mock_build_structured, mock_export_source,
    mock_ensure_recursive, mock_build_review, mock_save_discovery,
    mock_build_normalized, mock_save_normalized, tmp_path
):
    dummy_file = tmp_path / "dummy_records.json"
    dummy_file.write_text("[]", encoding="utf-8")
    mock_save_normalized.return_value = {"records": dummy_file, "by_area": Path("b"), "by_layer": Path("c")}
    mock_build_normalized.return_value = []
    mock_save_discovery.return_value = MagicMock(discovery_path="d")
    mock_build_review.return_value = MagicMock(review_queue_path="e")
    mock_ensure_recursive.return_value = "f"

    mock_export_source_inst = MagicMock()
    mock_export_source_inst.summary_path = "g"
    mock_export_source.return_value = mock_export_source_inst

    mock_build_structured.return_value = []
    mock_load_manifest.return_value = []
    mock_load_registry.return_value = []
    mock_rank_idx.return_value = []
    mock_promo_idx.return_value = {}
    mock_promo_sum.return_value = {}
    mock_dedup.return_value = {}
    mock_clusters.return_value = []
    mock_status.return_value = {}
    mock_op_views.return_value = {}
    mock_metrics.return_value = Path("h")

    # Ordinary root
    outputs_root = tmp_path / "outputs" / "pipeline_run" / "run1"

    refresh_intelligence_views(outputs_root)

    expected_intermediate = [outputs_root / "intermediate"]

    mock_save_normalized.assert_called_once_with(expected_intermediate, outputs_root, None, filter_func=ANY)
    mock_build_normalized.assert_called_once_with(expected_intermediate, None)
