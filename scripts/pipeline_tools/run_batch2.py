from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.agent_registry import DOWNSTREAM_FUNDING_CONTEXT_AGENTS
from core.batch_runner import execute_batch_run
from core.file_lock import atomic_write_json
from core.funding_selection import FundingCallContext
from core.mission_gate import relevance as mission_relevance
from core.normalization import save_normalized_collection_records
from run_pipeline import evaluate_collection_health, minimum_healthy_collectors


def _safe_component(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not value or value in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError(f"{label} must contain only letters, numbers, dots, underscores or hyphens")
    return value


def load_funding_context(path: Path) -> FundingCallContext:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Funding context file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Funding context file is not valid JSON: {exc}") from exc

    if isinstance(payload, dict) and isinstance(payload.get("funding_context"), dict):
        payload = payload["funding_context"]
    if not isinstance(payload, dict):
        raise ValueError("Funding context JSON must contain a serialized FundingCallContext object")

    try:
        context = FundingCallContext.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Funding context does not match the FundingCallContext schema: {exc}") from exc

    _safe_component(context.funding_call_id, "funding_call_id")
    if not str(context.call_title or "").strip():
        raise ValueError("Funding context is missing call_title")
    if not (str(context.application_url or "").strip() or str(context.source_url or "").strip()):
        raise ValueError("Funding context must contain application_url or source_url")

    research_context = context.research_context
    if research_context is None or str(getattr(research_context, "research_intent", "")).strip().lower() != "research":
        raise ValueError("Funding context must contain a verified research-intent context")
    for field_name in ("inferred_challenges", "evidence_refs", "inference_provenance", "research_intent_evidence"):
        if not getattr(research_context, field_name, None):
            raise ValueError(f"Funding context research_context is missing {field_name}")

    return context


def _selected_record_for_refresh(context: FundingCallContext) -> dict[str, Any]:
    research_context = asdict(context.research_context) if context.research_context is not None else None
    return {
        "funding_body": context.funding_body,
        "program_name": context.program_name,
        "call_id": context.call_id,
        "title": context.call_title,
        "status": context.status,
        "opening_date": context.opening_date,
        "deadline": context.deadline,
        "funding_amount": context.funding_amount,
        "currency": context.currency,
        "duration": context.duration,
        "eligible_costs": context.eligible_costs,
        "eligibility": context.eligibility,
        "geography": context.geography,
        "trl": context.trl,
        "research_priorities": context.research_priorities,
        "required_partners": context.required_partners,
        "deliverables": context.deliverables,
        "evaluation_criteria": context.evaluation_criteria,
        "application_url": context.application_url,
        "source_url": context.source_url,
        "source_title": context.source_name,
        "published_at": context.publication_date,
        "last_verified_at": context.last_updated,
        "focus_area": context.research_area,
        "research_context": research_context,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run RIF Batch-2 from a previously saved, validated Batch-1 FundingCallContext."
    )
    parser.add_argument("--funding-context", required=True, help="Path to a JSON-serialized FundingCallContext")
    parser.add_argument("--output-root", default="outputs/pipeline_run", help="Base directory for run-scoped outputs")
    parser.add_argument("--run-id", default=None, help="Optional run ID; generated when omitted")
    parser.add_argument("--execution-mode", choices=("sequential", "parallel"), default="parallel")
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--timeout-budget", type=int, default=1500)
    parser.add_argument("--min-healthy-agents", type=int, default=6)
    parser.add_argument("--force-refresh", action="store_true", help="Bypass configured-source refresh/cache policy")
    parser.add_argument("--dry-run", action="store_true", help="Validate context and plan the 11 collectors without executing them")
    return parser


def run_batch2(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    context = load_funding_context(Path(args.funding_context))
    if args.max_workers < 1:
        raise ValueError("--max-workers must be at least 1")
    if args.timeout_budget < 1:
        raise ValueError("--timeout-budget must be at least 1")
    if not 1 <= args.min_healthy_agents <= len(DOWNSTREAM_FUNDING_CONTEXT_AGENTS):
        raise ValueError(f"--min-healthy-agents must be between 1 and {len(DOWNSTREAM_FUNDING_CONTEXT_AGENTS)}")

    run_id = _safe_component(
        args.run_id or f"batch2-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}",
        "run_id",
    )
    output_root = Path(args.output_root).expanduser().resolve()
    run_root = output_root / run_id
    call_root = run_root / "calls" / context.funding_call_id
    workflow_dir = call_root / "workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)

    selected_payload = {
        "run_id": run_id,
        "selected": _selected_record_for_refresh(context),
        "selected_funding_call_id": context.funding_call_id,
        "funding_context": context.to_dict(),
        "rejected_alternatives": [],
    }
    atomic_write_json(workflow_dir / "selected_funding_call.json", selected_payload)

    collection_report = execute_batch_run(
        agent_names=sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS),
        area_names=[None],
        mode="configured_scan",
        base_input_data={
            "analysis_mode": "collect_only",
            "run_id": run_id,
            "pipeline_run_dir": str(run_root),
            "force_refresh": bool(args.force_refresh),
            "allow_llm_refinement": False,
            "allow_llm_analysis": False,
        },
        execution_mode=args.execution_mode,
        max_workers=args.max_workers,
        timeout_budget_seconds=args.timeout_budget,
        dry_run=bool(args.dry_run),
        report_path=run_root / "workflow" / "batch2_collection_report.json",
        funding_context=context,
    )

    if args.dry_run:
        report = {
            "run_id": run_id,
            "status": "dry_run",
            "funding_call_id": context.funding_call_id,
            "output_root": str(run_root),
            "collection": collection_report,
            "normalization": {"status": "not_run"},
        }
        atomic_write_json(run_root / "workflow" / "batch2_report.json", report)
        return 0, report

    tasks = collection_report.get("tasks", [])
    required = max(args.min_healthy_agents, minimum_healthy_collectors(len(DOWNSTREAM_FUNDING_CONTEXT_AGENTS)))
    healthy, healthy_count = evaluate_collection_health(tasks, required)
    report: dict[str, Any] = {
        "run_id": run_id,
        "funding_call_id": context.funding_call_id,
        "output_root": str(run_root),
        "collection": {
            "status": "success" if healthy else "failed_quality_gate",
            "healthy_agents": healthy_count,
            "total_agents": len(DOWNSTREAM_FUNDING_CONTEXT_AGENTS),
            "required_healthy_agents": required,
            "tasks": tasks,
            "report_path": collection_report.get("report_path"),
        },
        "normalization": {"status": "not_run"},
    }

    if not healthy:
        report["status"] = "collection_failed"
        atomic_write_json(run_root / "workflow" / "batch2_report.json", report)
        return 3, report

    accepted_ids: set[str] = set()
    rejected: list[dict[str, Any]] = []

    def apply_mission_gate(record: dict[str, Any]) -> bool:
        gate = mission_relevance(record, context)
        record["mission_relevance"] = gate
        evidence_id = str(record.get("evidence_id") or "")
        if gate.get("passed"):
            if evidence_id:
                accepted_ids.add(evidence_id)
            return True
        rejected.append({
            "evidence_id": evidence_id,
            "record_id": record.get("record_id"),
            "title": record.get("title"),
            "source_url": record.get("source_url"),
            **gate,
        })
        return False

    try:
        normalized_paths = save_normalized_collection_records(
            [run_root / "intermediate", call_root / "intermediate"],
            call_root,
            context.funding_call_id,
            filter_func=apply_mission_gate,
            target_run_id=run_id,
        )
        records_path = normalized_paths["records"]
        records = json.loads(records_path.read_text(encoding="utf-8"))
        errors = json.loads(normalized_paths["errors"].read_text(encoding="utf-8"))
        ledger_path = call_root / "normalized" / "evidence_ledger.db"
        ledger_count = 0
        if ledger_path.exists():
            with sqlite3.connect(ledger_path) as conn:
                ledger_count = int(conn.execute("SELECT COUNT(*) FROM canonical_evidence").fetchone()[0])
        if not records:
            raise RuntimeError("Normalization produced zero mission-relevant records")
        if errors:
            raise RuntimeError(f"Normalization reported {len(errors)} error(s); inspect normalized/errors.json")
        if ledger_count != len(records):
            raise RuntimeError(
                f"Evidence ledger count mismatch: JSON has {len(records)} records but ledger has {ledger_count}"
            )

        report["normalization"] = {
            "status": "success",
            "records_count": len(records),
            "mission_relevance_accepted": len(accepted_ids),
            "mission_relevance_rejected": len(rejected),
            "records_path": str(records_path),
            "errors_path": str(normalized_paths["errors"]),
            "ledger_path": str(ledger_path),
            "ledger_records": ledger_count,
        }
        report["status"] = "success"
        report["batch3_handoff"] = "validated; execution stops before Batch-3"
        atomic_write_json(workflow_dir / "mission_relevance_gate.json", {
            "input_accepted_ids": sorted(accepted_ids),
            "rejected_count": len(rejected),
            "rejected": rejected[:500],
        })
        atomic_write_json(run_root / "workflow" / "batch2_report.json", report)
        return 0, report
    except Exception as exc:
        report["normalization"] = {
            "status": "failed",
            "error": str(exc),
            "diagnostics": str(call_root / "normalized" / "errors.json"),
        }
        report["status"] = "normalization_failed"
        atomic_write_json(run_root / "workflow" / "batch2_report.json", report)
        return 4, report


def main() -> int:
    args = build_parser().parse_args()
    try:
        code, report = run_batch2(args)
    except Exception as exc:
        print(f"Batch-2 input/configuration error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
