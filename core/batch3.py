"""Batch-3: NLP processing (tagging -> clustering -> trends) with a validated contract.

Batch-3 consumes one call's Batch-2 normalized handoff
(``<run>/calls/<funding_call_id>/normalized/``) and writes
``processed/tags``, ``processed/clusters`` and ``processed/trends``. The stage
invocations are identical to the full pipeline's. A stage status of
``partial_success`` is not sufficient on its own: the outputs must cover every
handed-off record, keep call/run identity, and leave the Batch-2 handoff intact.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from core.schemas import ALLOWED_RESEARCH_AREAS, normalize_area

HANDOFF_FILES = (
    "records.json", "by_area.json", "by_layer.json", "errors.json",
    "skipped_artifacts.json", "input_manifest.json", "evidence_ledger.db",
)


def batch3_stage_plan(call_root: Path) -> list[tuple[str, dict[str, Any]]]:
    """The exact stage sequence and payloads used by the full pipeline."""
    return [
        ("tagging", {"outputs_root": str(call_root), "output_dir": str(call_root / "processed" / "tags")}),
        ("clustering", {
            "outputs_root": str(call_root),
            "output_dir": str(call_root / "processed" / "clusters"),
            "threshold": 0.20,
            "llm_ambiguity_resolution": False,
        }),
        ("trend", {"outputs_root": str(call_root), "output_dir": str(call_root / "processed" / "trends")}),
    ]


def processing_area(records: list[dict[str, Any]]) -> str | None:
    """Single canonical area when all records share one; otherwise process unscoped."""
    areas = set()
    for record in records:
        area = normalize_area(record.get("research_area"))
        areas.add(area if area in ALLOWED_RESEARCH_AREAS else "Unscoped")
    if len(areas) == 1 and "Unscoped" not in areas:
        return next(iter(areas))
    return None


def _fingerprints(normalized_dir: Path) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in HANDOFF_FILES:
        path = normalized_dir / name
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return result


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_batch3_outputs(call_root: Path, funding_call_id: str) -> list[str]:
    """Return contract violations for one call's Batch-3 outputs (empty when valid)."""
    issues: list[str] = []
    try:
        records = _load_json(call_root / "normalized" / "records.json")
    except Exception as exc:
        return [f"normalized handoff unreadable: {exc}"]
    if not isinstance(records, list) or not records:
        return ["normalized handoff contains no records"]
    record_ids = [str(r.get("record_id") or "") for r in records if isinstance(r, dict)]
    expected = set(record_ids)
    run_ids = {str(r.get("run_id")) for r in records if isinstance(r, dict) and r.get("run_id")}

    try:
        tagged = _load_json(call_root / "processed" / "tags" / "tagged_records.json")
    except Exception as exc:
        tagged = None
        issues.append(f"tagged records unreadable: {exc}")
    if isinstance(tagged, list):
        tagged_ids = [str(t.get("record_id") or "") for t in tagged if isinstance(t, dict)]
        if sorted(tagged_ids) != sorted(record_ids):
            issues.append(
                f"tagging does not cover the handoff exactly once ({len(tagged_ids)} tagged / {len(record_ids)} records)"
            )
        if any(t.get("funding_call_id") != funding_call_id for t in tagged if isinstance(t, dict)):
            issues.append("tagged record carries a different funding_call_id")
        tagged_runs = {str(t.get("run_id")) for t in tagged if isinstance(t, dict) and t.get("run_id")}
        if run_ids and tagged_runs != run_ids:
            issues.append("tagged records do not preserve the handoff run_id")
    elif tagged is not None:
        issues.append("tagged records file is not a list")

    try:
        clusters_raw = _load_json(call_root / "processed" / "clusters" / "clusters.json")
        clusters = clusters_raw if isinstance(clusters_raw, list) else clusters_raw.get("clusters")
    except Exception as exc:
        clusters = None
        issues.append(f"clusters unreadable: {exc}")
    if isinstance(clusters, list):
        if not clusters:
            issues.append("clustering produced no clusters")
        members = [
            str(m.get("record_id") if isinstance(m, dict) else m)
            for cluster in clusters for m in (cluster.get("members") or [])
        ]
        if sorted(members) != sorted(record_ids):
            issues.append(
                f"clusters do not cover the handoff exactly once ({len(members)} members / {len(record_ids)} records)"
            )
        if any(cluster.get("funding_call_id") != funding_call_id for cluster in clusters):
            issues.append("cluster carries a different funding_call_id")
    elif clusters is not None:
        issues.append("clusters file has no cluster list")

    trend_files = sorted((call_root / "processed" / "trends").glob("*trends.json"))
    if not trend_files:
        issues.append("trend output is missing")
    for trend_file in trend_files:
        try:
            trend = _load_json(trend_file)
        except Exception as exc:
            issues.append(f"{trend_file.name} unreadable: {exc}")
            continue
        if trend.get("funding_call_id") != funding_call_id:
            issues.append(f"{trend_file.name} carries a different funding_call_id")
        counts = trend.get("counts") or {}
        if counts.get("records") != len(expected):
            issues.append(f"{trend_file.name} counts {counts.get('records')} records, handoff has {len(expected)}")
        if isinstance(clusters, list) and counts.get("clusters") != len(clusters):
            issues.append(f"{trend_file.name} counts {counts.get('clusters')} clusters, clustering produced {len(clusters)}")
    return issues


def run_batch3_for_call(
    call_root: Path,
    funding_context: Any,
    run_stage: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Run tagging, clustering and trends for one call and validate the result."""
    call_root = Path(call_root)
    funding_call_id = str(getattr(funding_context, "funding_call_id", "") or call_root.name)
    normalized_dir = call_root / "normalized"
    report: dict[str, Any] = {"funding_call_id": funding_call_id, "output_root": str(call_root.resolve()), "stages": {}}
    try:
        records = _load_json(normalized_dir / "records.json")
    except Exception as exc:
        report.update(status="blocked_missing_handoff", issues=[f"normalized handoff unreadable: {exc}"])
        return report
    if not isinstance(records, list) or not records:
        report.update(status="blocked_missing_handoff", issues=["normalized handoff contains no records"])
        return report

    before = _fingerprints(normalized_dir)
    area = processing_area(records)
    report["processing_area"] = area or "Unscoped"
    for agent, payload in batch3_stage_plan(call_root):
        try:
            result = run_stage(agent, "configured_scan", area, payload, funding_context)
        except Exception as exc:
            report["stages"][agent] = {"status": "error", "error": str(exc)}
            report.update(status="blocked_processing", issues=[f"{agent} raised: {exc}"])
            return report
        report["stages"][agent] = {
            "status": result.get("status"),
            "outputs_count": result.get("outputs_count", len(result.get("outputs") or [])),
            "warnings": list(result.get("warnings") or [])[:20],
            "errors": list(result.get("errors") or [])[:20],
        }
        if str(result.get("status", "")).lower() not in {"success", "partial_success"}:
            report.update(status="blocked_processing", issues=[f"{agent} status={result.get('status')}"])
            return report

    issues = validate_batch3_outputs(call_root, funding_call_id)
    if _fingerprints(normalized_dir) != before:
        issues.append("Batch-3 modified the Batch-2 normalized handoff")
    report["issues"] = issues
    report["status"] = "ready_for_batch4" if not issues else "blocked_validation"
    return report
