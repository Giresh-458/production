from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGRESSION_DIR = PROJECT_ROOT / "outputs" / "workflow" / "regression"
REGRESSION_LATEST_PATH = REGRESSION_DIR / "latest.json"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _count_list_file(path: Path) -> int:
    payload = _read_json(path)
    return len(payload) if isinstance(payload, list) else 0


def _nested_get(mapping: dict[str, Any] | None, *keys: str, default: Any = 0) -> Any:
    current: Any = mapping or {}
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def collect_regression_snapshot(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    outputs_root = project_root / "outputs"
    workflow_root = outputs_root / "workflow"
    synthesis_manifest_root = outputs_root / "synthesis" / "manifest"
    clusters_root = outputs_root / "synthesis" / "clusters"
    normalized_root = outputs_root / "normalized"

    operational_views = _read_json(workflow_root / "operational_views.json") or {}
    workflow_status = _read_json(workflow_root / "status.json") or {}
    promotion_summary = _read_json(synthesis_manifest_root / "promotion.json") or {}
    ranking_entries = _read_json(synthesis_manifest_root / "ranking.json") or []
    dedup_report = _read_json(synthesis_manifest_root / "dedup_report.json") or {}

    return {
        "generated_at": _iso_now(),
        "counts": {
            "normalized_records": _count_list_file(normalized_root / "records.json"),
            "clusters": _count_list_file(clusters_root / "index.json"),
            "synthesis_entries": _count_list_file(synthesis_manifest_root / "index.json"),
            "ranking_entries": len(ranking_entries) if isinstance(ranking_entries, list) else 0,
            "pending_synthesis": len(_nested_get(workflow_status, "pending_synthesis", default=[])),
            "candidate_sources": int(_nested_get(operational_views, "queues", "candidate_source_count", default=0) or 0),
            "pending_review": int(_nested_get(operational_views, "queues", "pending_review_count", default=0) or 0),
            "failed_fetches": int(_nested_get(operational_views, "failures", "failed_source_fetch_count", default=0) or 0),
            "watchlist": int(_nested_get(promotion_summary, "counts", "watchlist", default=0) or 0),
            "active_research_candidates": int(_nested_get(promotion_summary, "counts", "active_research_candidates", default=0) or 0),
            "high_priority_proposal_candidates": int(
                _nested_get(promotion_summary, "counts", "high_priority_proposal_candidates", default=0) or 0
            ),
            "exact_duplicates": int(_nested_get(dedup_report, "counts", "exact_duplicates", default=0) or 0),
            "soft_duplicates": int(_nested_get(dedup_report, "counts", "soft_duplicates", default=0) or 0),
        }
    }


def build_snapshot_diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    previous_counts = (previous or {}).get("counts", {}) if isinstance(previous, dict) else {}
    current_counts = current.get("counts", {}) if isinstance(current, dict) else {}
    deltas: dict[str, Any] = {}
    for key, current_value in current_counts.items():
        previous_value = previous_counts.get(key, 0)
        deltas[key] = {
            "previous": previous_value,
            "current": current_value,
            "delta": current_value - previous_value if isinstance(current_value, (int, float)) and isinstance(previous_value, (int, float)) else None,
        }
    return {
        "has_previous_snapshot": isinstance(previous, dict),
        "previous_generated_at": (previous or {}).get("generated_at") if isinstance(previous, dict) else None,
        "current_generated_at": current.get("generated_at"),
        "deltas": deltas,
    }


def save_regression_report(report: dict[str, Any], *, regression_dir: Path = REGRESSION_DIR) -> Path:
    regression_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = regression_dir / f"regression_report_{timestamp}.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    REGRESSION_LATEST_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report_path


def load_latest_regression_report(path: Path = REGRESSION_LATEST_PATH) -> dict[str, Any] | None:
    payload = _read_json(path)
    return payload if isinstance(payload, dict) else None
