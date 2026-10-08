from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUTS_ROOT = PROJECT_ROOT / "outputs"
QUALITY_METRICS_PATH = OUTPUTS_ROOT / "workflow" / "quality_metrics.json"

GENERIC_TITLE_PATTERNS = {
    "home",
    "insights",
    "archive",
    "all content",
    "blog",
    "news",
    "research",
    "docs",
    "documentation",
    "featured",
    "filings",
    "writing",
}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _clean_title(value: str) -> str:
    return " ".join(str(value or "").lower().split()).strip()


def _is_generic_title(title: str) -> bool:
    cleaned = _clean_title(title)
    if not cleaned:
        return True
    if cleaned in GENERIC_TITLE_PATTERNS:
        return True
    return any(cleaned.endswith(f" {pattern}") for pattern in GENERIC_TITLE_PATTERNS)


def build_quality_metrics(outputs_root: Path = OUTPUTS_ROOT) -> dict[str, Any]:
    workflow_root = outputs_root / "workflow"
    normalized_root = outputs_root / "normalized"
    synthesis_manifest_root = outputs_root / "synthesis" / "manifest"
    clusters_root = outputs_root / "synthesis" / "clusters"

    golden_report = _read_json(workflow_root / "golden_test_report.json") or {}
    operational_views = _read_json(workflow_root / "operational_views.json") or {}
    normalized_records = _read_json(normalized_root / "records.json") or []
    dedup_report = _read_json(synthesis_manifest_root / "dedup_report.json") or {}
    promotion_summary = _read_json(synthesis_manifest_root / "promotion.json") or {}
    clusters = _read_json(clusters_root / "index.json") or []
    ideas = sorted((outputs_root / "ideas").rglob("*.md")) if (outputs_root / "ideas").exists() else []
    proposals = sorted((outputs_root / "proposals").rglob("*.md")) if (outputs_root / "proposals").exists() else []

    golden_results = [item for item in golden_report.get("results", []) if isinstance(item, dict)]
    agent_run_cases = [item for item in golden_results if str(item.get("type", "")) == "agent_run"]
    quality_gate_cases = [item for item in golden_results if str(item.get("type", "")) == "quality_gate"]
    recursive_cases = [item for item in golden_results if str(item.get("type", "")) == "recursive_links"]

    accepted_precision_passes = sum(1 for item in agent_run_cases if bool(item.get("passed")))
    generic_rejection_passes = sum(1 for item in quality_gate_cases if bool(item.get("passed")))
    recursive_passes = sum(1 for item in recursive_cases if bool(item.get("passed")))

    generic_titles_in_records = sum(
        1
        for item in normalized_records
        if isinstance(item, dict) and _is_generic_title(str(item.get("title", "")))
    )
    corroborated_clusters = sum(
        1
        for item in clusters
        if isinstance(item, dict) and len({str(layer).strip() for layer in item.get("supporting_layers", []) if str(layer).strip()}) >= 2
    )

    dedup_counts = dedup_report.get("counts", {}) if isinstance(dedup_report, dict) else {}
    promotion_counts = promotion_summary.get("counts", {}) if isinstance(promotion_summary, dict) else {}
    pending_synthesis_count = int(((operational_views.get("queues") or {}).get("pending_synthesis_count", 0)) or 0)
    normalized_record_count = len(normalized_records) if isinstance(normalized_records, list) else 0
    cluster_count = len(clusters) if isinstance(clusters, list) else 0

    proposal_ready_count = len(proposals)
    if proposal_ready_count == 0:
        proposal_ready_count = int(promotion_counts.get("high_priority_proposal_candidates", 0) or 0)

    metrics = {
        "generated_at": _iso_now(),
        "definitions": {
            "accepted_artifact_precision": "Pass rate of curated positive golden collection/synthesis cases.",
            "generic_page_rejection_rate": "Pass rate of curated generic-page rejection golden cases.",
            "recursive_extraction_pass_rate": "Pass rate of curated recursive link extraction golden cases.",
            "cross_layer_corroboration_rate": "Fraction of clusters supported by two or more layers.",
            "generic_artifact_rate": "Fraction of normalized records whose titles still look generic.",
            "high_priority_synthesis_count": "Number of syntheses promoted to high-priority proposal candidates.",
            "active_research_candidate_count": "Number of syntheses promoted to active research candidates.",
            "watchlist_count": "Number of syntheses currently on the watchlist.",
            "proposal_ready_idea_count": "Count of generated proposal artifacts, or high-priority proposal candidates if proposal artifacts are absent.",
            "pending_synthesis_ratio": "Pending synthesis items divided by normalized record count.",
            "duplicate_pressure": "Combined exact and soft duplicate count divided by normalized record count.",
        },
        "metrics": {
            "accepted_artifact_precision": {
                "value": _ratio(accepted_precision_passes, len(agent_run_cases)),
                "numerator": accepted_precision_passes,
                "denominator": len(agent_run_cases),
            },
            "generic_page_rejection_rate": {
                "value": _ratio(generic_rejection_passes, len(quality_gate_cases)),
                "numerator": generic_rejection_passes,
                "denominator": len(quality_gate_cases),
            },
            "recursive_extraction_pass_rate": {
                "value": _ratio(recursive_passes, len(recursive_cases)),
                "numerator": recursive_passes,
                "denominator": len(recursive_cases),
            },
            "cross_layer_corroboration_rate": {
                "value": _ratio(corroborated_clusters, cluster_count),
                "numerator": corroborated_clusters,
                "denominator": cluster_count,
            },
            "generic_artifact_rate": {
                "value": _ratio(generic_titles_in_records, normalized_record_count),
                "numerator": generic_titles_in_records,
                "denominator": normalized_record_count,
            },
            "high_priority_synthesis_count": {
                "value": int(promotion_counts.get("high_priority_proposal_candidates", 0) or 0),
            },
            "active_research_candidate_count": {
                "value": int(promotion_counts.get("active_research_candidates", 0) or 0),
            },
            "watchlist_count": {
                "value": int(promotion_counts.get("watchlist", 0) or 0),
            },
            "proposal_ready_idea_count": {
                "value": proposal_ready_count,
                "proposals_present": len(proposals),
                "high_priority_candidates_fallback": int(promotion_counts.get("high_priority_proposal_candidates", 0) or 0),
            },
            "pending_synthesis_ratio": {
                "value": _ratio(pending_synthesis_count, normalized_record_count),
                "numerator": pending_synthesis_count,
                "denominator": normalized_record_count,
            },
            "duplicate_pressure": {
                "value": _ratio(
                    int(dedup_counts.get("exact_duplicates", 0) or 0) + int(dedup_counts.get("soft_duplicates", 0) or 0),
                    normalized_record_count,
                ),
                "numerator": int(dedup_counts.get("exact_duplicates", 0) or 0) + int(dedup_counts.get("soft_duplicates", 0) or 0),
                "denominator": normalized_record_count,
            },
        },
        "context": {
            "normalized_record_count": normalized_record_count,
            "cluster_count": cluster_count,
            "idea_artifact_count": len(ideas),
            "proposal_artifact_count": len(proposals),
            "dedup_counts": dedup_counts,
            "promotion_counts": promotion_counts,
            "pending_synthesis_count": pending_synthesis_count,
        },
    }
    return metrics


def save_quality_metrics(metrics: dict[str, Any], path: Path | None = None) -> Path:
    target = path or (OUTPUTS_ROOT / "workflow" / "quality_metrics.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def generate_and_save_quality_metrics(outputs_root: Path = OUTPUTS_ROOT, path: Path | None = None) -> Path:
    metrics = build_quality_metrics(outputs_root)
    target = path or (Path(outputs_root) / "workflow" / "quality_metrics.json")
    return save_quality_metrics(metrics, target)
