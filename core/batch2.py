from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.agent_registry import DOWNSTREAM_FUNDING_CONTEXT_AGENTS
from core.batch_runner import execute_batch_run
from core.file_lock import atomic_write_json
from core.funding_selection import FundingCallContext
from core.mission_gate import relevance as mission_relevance
from core.normalization import save_normalized_collection_records


def evaluate_collection_health(
    collection_tasks: list[dict[str, Any]],
    min_required_agents: int,
) -> tuple[bool, int]:
    """Count unique collectors that returned usable persisted artifacts."""
    healthy_agents: set[str] = set()
    for task in collection_tasks:
        agent_name = str(task.get("agent_name") or "").strip()
        status = str(task.get("status", "")).lower()
        outputs_count = int(task.get("outputs_count", 0) or 0)
        if agent_name and status in {"success", "partial_success", "partial_limit"} and outputs_count > 0:
            healthy_agents.add(agent_name)
    return len(healthy_agents) >= min_required_agents, len(healthy_agents)


def minimum_healthy_collectors(total_agents: int) -> int:
    """Require a strict majority, with a three-agent floor for larger batches."""
    if total_agents <= 0:
        return 0
    return min(total_agents, max(3, total_agents // 2 + 1))


def _selected_call_record(context: FundingCallContext) -> dict[str, Any]:
    """Serialize a FundingCallContext in the record shape used by refresh reconstruction."""
    data = context.to_dict()
    return {
        "call_id": data.get("call_id") or context.funding_call_id,
        "title": data.get("call_title"),
        "program_name": data.get("program_name"),
        "organization": data.get("funding_body"),
        "funding_body": data.get("funding_body"),
        "status": data.get("status"),
        "opening_date": data.get("opening_date"),
        "deadline": data.get("deadline"),
        "funding_amount": data.get("funding_amount"),
        "currency": data.get("currency"),
        "duration": data.get("duration"),
        "eligible_costs": data.get("eligible_costs"),
        "eligibility": data.get("eligibility"),
        "geography": data.get("geography"),
        "trl": data.get("trl"),
        "research_priorities": data.get("research_priorities") or [],
        "required_partners": data.get("required_partners"),
        "deliverables": data.get("deliverables"),
        "evaluation_criteria": data.get("evaluation_criteria"),
        "application_url": data.get("application_url"),
        "source_url": data.get("source_url"),
        "source_title": data.get("source_name"),
        "published_at": data.get("publication_date"),
        "last_verified_at": data.get("last_updated"),
        "focus_area": data.get("research_area"),
        "research_context": data.get("research_context"),
    }


def _read_json_list(path: Path) -> list[Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def execute_batch2(
    *,
    funding_contexts: list[FundingCallContext],
    output_root: Path | str = Path("outputs/batch2_runs"),
    run_id: str | None = None,
    execution_mode: str = "parallel",
    max_workers: int = 6,
    timeout_budget_seconds: int = 1500,
    default_limit: int | None = None,
    force_refresh: bool = True,
) -> dict[str, Any]:
    """Run all 11 downstream collectors, normalize per call, and stop before Batch-3.

    This deliberately uses collect_only mode. It does not invoke the Batch-3
    tagging, clustering, or trend agents.
    """
    contexts = list(funding_contexts or [])
    if not contexts:
        raise ValueError("At least one validated Batch-1 FundingCallContext is required.")
    if execution_mode not in {"parallel", "sequential"}:
        raise ValueError("execution_mode must be 'parallel' or 'sequential'.")
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1.")
    if timeout_budget_seconds < 1:
        raise ValueError("timeout_budget_seconds must be at least 1.")

    call_ids = [str(context.funding_call_id or "").strip() for context in contexts]
    if any(not call_id for call_id in call_ids):
        raise ValueError("Every FundingCallContext must contain a non-empty funding_call_id.")
    if len(set(call_ids)) != len(call_ids):
        raise ValueError("FundingCallContext funding_call_id values must be unique.")
    for call_id in call_ids:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", call_id):
            raise ValueError(f"Unsafe funding_call_id for output directory: {call_id!r}")

    resolved_run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", resolved_run_id):
        raise ValueError(f"Unsafe run_id for output directory: {resolved_run_id!r}")

    run_root = Path(output_root) / resolved_run_id
    if run_root.exists() and any(run_root.iterdir()):
        raise FileExistsError(
            f"Batch-2 output directory already contains data: {run_root}. "
            "Use a new --run-id so artifacts from separate executions cannot mix."
        )
    workflow_root = run_root / "workflow"
    workflow_root.mkdir(parents=True, exist_ok=True)

    expected_agents = set(DOWNSTREAM_FUNDING_CONTEXT_AGENTS)
    if len(expected_agents) != 11:
        raise RuntimeError(
            f"Expected exactly 11 downstream collection agents, found {len(expected_agents)}."
        )

    collection_report = execute_batch_run(
        agent_names=sorted(expected_agents),
        area_names=[None],
        mode="configured_scan",
        base_input_data={
            "analysis_mode": "collect_only",
            "run_id": resolved_run_id,
            "pipeline_run_dir": str(run_root),
            "force_refresh": bool(force_refresh),
            "allow_llm_refinement": False,
        },
        execution_mode=execution_mode,
        dry_run=False,
        default_limit=default_limit,
        timeout_budget_seconds=timeout_budget_seconds,
        max_workers=max_workers,
        analysis_mode="collect_only",
        report_path=workflow_root / "shared_downstream_collection_plan.json",
        funding_contexts=contexts,
    )

    tasks = collection_report.get("tasks", []) if isinstance(collection_report, dict) else []
    scheduled_agents = {str(task.get("agent_name") or "") for task in tasks}
    missing_agents = sorted(expected_agents - scheduled_agents)
    min_required = minimum_healthy_collectors(len(expected_agents))
    healthy, healthy_count = evaluate_collection_health(tasks, min_required)
    collector_health = {
        "scheduled_agents": sorted(scheduled_agents),
        "missing_agents": missing_agents,
        "healthy_agents": healthy_count,
        "total_agents": len(expected_agents),
        "required_healthy_agents": min_required,
    }

    report: dict[str, Any] = {
        "run_id": resolved_run_id,
        "status": "running",
        "output_root": str(run_root.resolve()),
        "scope": "Batch-2 collection and normalization only",
        "batch3_started": False,
        "next_stage": "Batch-3 NLP processing (not executed by this runner)",
        "collector_health": collector_health,
        "collection": collection_report,
        "funding_calls": [],
    }

    if missing_agents or not healthy:
        report["status"] = "blocked_collection_health"
        report["blockers"] = []
        if missing_agents:
            report["blockers"].append(
                {"code": "MISSING_COLLECTOR_TASKS", "agents": missing_agents}
            )
        if not healthy:
            report["blockers"].append(
                {
                    "code": "INSUFFICIENT_HEALTHY_COLLECTORS",
                    "healthy_agents": healthy_count,
                    "required_healthy_agents": min_required,
                }
            )
        atomic_write_json(workflow_root / "batch2_report.json", report)
        report["report_path"] = str((workflow_root / "batch2_report.json").resolve())
        return report

    for context in contexts:
        call_id = context.funding_call_id
        call_root = run_root / "calls" / call_id
        call_workflow = call_root / "workflow"
        call_workflow.mkdir(parents=True, exist_ok=True)

        # Keep the same selected-call wrapper consumed by normalization refresh
        # and the existing Batch-3 agents.
        atomic_write_json(
            call_workflow / "selected_funding_call.json",
            {
                "run_id": resolved_run_id,
                "selected": _selected_call_record(context),
                "rejected_alternatives": [],
            },
        )

        accepted_ids: set[str] = set()
        rejected_ids: set[str] = set()
        gate_details: list[dict[str, Any]] = []

        def apply_mission_gate(record: dict[str, Any]) -> bool:
            gate = mission_relevance(record, context)
            record["mission_relevance"] = gate
            evidence_id = str(record.get("evidence_id") or "")
            if gate.get("passed"):
                accepted_ids.add(evidence_id)
                return True
            rejected_ids.add(evidence_id)
            gate_details.append(
                {
                    "record_id": record.get("record_id"),
                    "title": record.get("title"),
                    "source_url": record.get("source_url"),
                    **gate,
                }
            )
            return False

        normalized_paths = save_normalized_collection_records(
            [run_root / "intermediate", call_root / "intermediate"],
            call_root,
            call_id,
            filter_func=apply_mission_gate,
            target_run_id=resolved_run_id,
        )
        records = _read_json_list(normalized_paths["records"])
        normalization_errors = _read_json_list(normalized_paths["errors"])

        handoff_errors: list[str] = []
        for record in records:
            if record.get("run_id") != resolved_run_id:
                handoff_errors.append(
                    f"record {record.get('record_id')} has a mismatched run_id"
                )
            if record.get("funding_call_id") != call_id:
                handoff_errors.append(
                    f"record {record.get('record_id')} has a mismatched funding_call_id"
                )
            if not str(record.get("source_url") or "").startswith(("http://", "https://")):
                handoff_errors.append(
                    f"record {record.get('record_id')} has no usable HTTP(S) source URL"
                )
            if record.get("normalization_status") != "NORMALIZED":
                handoff_errors.append(
                    f"record {record.get('record_id')} is marked {record.get('normalization_status')!r}"
                )

        input_count = len(accepted_ids) + len(rejected_ids)
        call_status = "ready_for_batch3"
        if normalization_errors or handoff_errors:
            call_status = "blocked_normalization"
        elif not records:
            call_status = "no_relevant_evidence" if input_count else "no_usable_evidence"

        gate_report = {
            "funding_call_id": call_id,
            "run_id": resolved_run_id,
            "input_records": input_count,
            "accepted_records": len(accepted_ids),
            "rejected_records": len(rejected_ids),
            "details": gate_details[:500],
        }
        atomic_write_json(call_workflow / "mission_relevance_gate.json", gate_report)

        report["funding_calls"].append(
            {
                "funding_call_id": call_id,
                "title": context.call_title or context.program_name,
                "status": call_status,
                "normalized_records": len(records),
                "normalization_errors": normalization_errors,
                "handoff_errors": handoff_errors,
                "normalized_paths": {
                    name: str(path.resolve()) for name, path in normalized_paths.items()
                },
                "mission_relevance_gate": gate_report,
                "output_root": str(call_root.resolve()),
            }
        )

    call_statuses = {item["status"] for item in report["funding_calls"]}
    if call_statuses == {"ready_for_batch3"}:
        report["status"] = "ready_for_batch3"
    elif "blocked_normalization" in call_statuses:
        report["status"] = "blocked_normalization"
    else:
        report["status"] = "incomplete_no_handoff"

    atomic_write_json(workflow_root / "batch2_report.json", report)
    report["report_path"] = str((workflow_root / "batch2_report.json").resolve())
    return report
