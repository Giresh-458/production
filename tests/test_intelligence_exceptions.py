import pytest
import json
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.intelligence import refresh_intelligence_views

@patch('core.intelligence.build_source_review_queue')
@patch('core.intelligence.ensure_recursive_review_log', return_value="a")
@patch('core.intelligence.export_source_health_summary')
@patch('core.intelligence.build_structured_output_index', return_value=[])
@patch('core.intelligence.load_manifest_entries', return_value=[])
@patch('core.intelligence.load_source_registry_entries', return_value=[])
@patch('core.intelligence.build_problem_ranking_index', return_value=[])
@patch('core.intelligence.build_promotion_index', return_value={})
@patch('core.intelligence.build_promotion_summary', return_value={})
@patch('core.intelligence.detect_evidence_relationships', return_value={})
@patch('core.intelligence.build_problem_clusters', return_value=[])
@patch('core.intelligence.build_batch_workflow_status', return_value={})
@patch('core.intelligence.build_operational_views', return_value={})
@patch('core.intelligence.generate_and_save_quality_metrics', return_value=Path("g"))
@patch('core.intelligence.save_normalized_collection_records')
@patch('core.normalization.resolve_refresh_context')
def test_refresh_intelligence_views_malformed_records(
    mock_resolve_refresh,
    mock_save_normalized,
    mock_metrics,
    mock_views,
    mock_workflow,
    mock_clusters,
    mock_relationships,
    mock_promo_summary,
    mock_promo_index,
    mock_ranking,
    mock_registry,
    mock_manifest,
    mock_index,
    mock_health,
    mock_review_log,
    mock_review_queue,
    tmp_path
):
    outputs_root = tmp_path / "test_output"
    outputs_root.mkdir()

    mock_resolve_refresh.return_value = (None, None, None)

    records_file = outputs_root / "records.json"
    records_file.write_text("{malformed_json_here}", encoding="utf-8")

    mock_save_normalized.return_value = {
        "records": records_file,
        "by_area": outputs_root / "by_area.json",
        "by_layer": outputs_root / "by_layer.json",
    }

    with pytest.raises(RuntimeError) as exc:
        refresh_intelligence_views(outputs_root)

    assert "Failed to read or parse normalized records from" in str(exc.value)

@patch('core.intelligence.build_source_review_queue')
@patch('core.intelligence.ensure_recursive_review_log', return_value="a")
@patch('core.intelligence.export_source_health_summary')
@patch('core.intelligence.build_structured_output_index', return_value=[])
@patch('core.intelligence.load_manifest_entries', return_value=[])
@patch('core.intelligence.load_source_registry_entries', return_value=[])
@patch('core.intelligence.build_problem_ranking_index', return_value=[])
@patch('core.intelligence.build_promotion_index', return_value={})
@patch('core.intelligence.build_promotion_summary', return_value={})
@patch('core.intelligence.detect_evidence_relationships', return_value={})
@patch('core.intelligence.build_problem_clusters', return_value=[])
@patch('core.intelligence.build_batch_workflow_status', return_value={})
@patch('core.intelligence.build_operational_views', return_value={})
@patch('core.intelligence.generate_and_save_quality_metrics', return_value=Path("g"))
@patch('core.intelligence.save_normalized_collection_records')
@patch('core.normalization.resolve_refresh_context')
@patch('core.intelligence.build_normalized_collection_records', return_value=[])
@patch('core.intelligence.save_source_discovery_candidates')
def test_refresh_intelligence_views_empty_records(
    mock_save_discovery,
    mock_build_normalized,
    mock_resolve_refresh,
    mock_save_normalized,
    mock_metrics,
    mock_views,
    mock_workflow,
    mock_clusters,
    mock_relationships,
    mock_promo_summary,
    mock_promo_index,
    mock_ranking,
    mock_registry,
    mock_manifest,
    mock_index,
    mock_health,
    mock_review_log,
    mock_review_queue,
    tmp_path
):
    outputs_root = tmp_path / "test_output_empty"
    outputs_root.mkdir()

    mock_resolve_refresh.return_value = (None, None, None)

    records_file = outputs_root / "records.json"
    records_file.write_text("[]", encoding="utf-8")

    mock_save_normalized.return_value = {
        "records": records_file,
        "by_area": outputs_root / "by_area.json",
        "by_layer": outputs_root / "by_layer.json",
    }

    # Should not raise exception
    refresh_intelligence_views(outputs_root)
    mock_save_discovery.assert_called_once_with([])
