from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.agent_registry import DOWNSTREAM_FUNDING_CONTEXT_AGENTS
from core.batch_runner import execute_batch_run
from core.file_lock import atomic_write_json
from core.funding_selection import FundingCallContext, has_substantive_eligibility_evidence
from core.mission_gate import relevance as mission_relevance
from core.normalization import save_normalized_collection_records


def evaluate_collection_health(
    collection_tasks: list[dict[str, Any]],
    min_required_agents: int,
    expected_agents: set[str] | None = None,
) -> tuple[bool, int]:
    """Count only expected collectors that returned usable persisted artifacts."""
    healthy_agents: set[str] = set()
    for task in collection_tasks:
        if not isinstance(task, dict):
            continue
        agent_name = str(task.get("agent_name") or "").strip()
        if not agent_name or (expected_agents is not None and agent_name not in expected_agents):
            continue
        status = str(task.get("status", "")).lower()
        try:
            outputs_count = int(task.get("outputs_count", 0) or 0)
        except (TypeError, ValueError):
            outputs_count = 0
        if status in {"success", "partial_success", "partial_limit"} and outputs_count > 0:
            healthy_agents.add(agent_name)
    return len(healthy_agents) >= min_required_agents, len(healthy_agents)


def minimum_healthy_collectors(total_agents: int) -> int:
    """Require a strict majority, with a three-agent floor for larger batches."""
    if total_agents <= 0:
        return 0
    return min(total_agents, max(3, total_agents // 2 + 1))


def validate_batch1_context(context: FundingCallContext) -> list[str]:
    """Reject handoff data that does not satisfy the Batch-1 downstream contract."""
    if not isinstance(context, FundingCallContext):
        return ["input is not a FundingCallContext"]

    issues: list[str] = []
    if str(context.status or "").strip().upper() != "OPEN_CALL":
        issues.append("funding call is not confirmed currently open")
    source_parts = urlparse(str(context.source_url or "").strip())
    application_parts = urlparse(str(context.application_url or "").strip())
    has_source_url = source_parts.scheme in {"http", "https"} and bool(source_parts.netloc)
    has_application_url = application_parts.scheme in {"http", "https"} and bool(application_parts.netloc)
    if not (has_source_url or has_application_url):
        issues.append("funding call is missing a usable HTTP(S) source or application URL")
    if not has_substantive_eligibility_evidence(context.eligibility):
        issues.append("funding call is missing substantive applicant eligibility evidence")

    research_context = context.research_context
    if research_context is None:
        issues.append("funding call is missing its validated research context")
    else:
        def field(name: str) -> Any:
            if isinstance(research_context, dict):
                return research_context.get(name)
            return getattr(research_context, name, None)

        if str(field("funding_call_id") or "").strip() != context.funding_call_id:
            issues.append("research-context funding_call_id does not match the selected funding call")
        if str(field("research_intent") or "").strip().casefold() != "research":
            issues.append("research intent is not explicitly verified as research")
        if str(field("confidence") or "").strip().casefold() in {"", "unknown", "none", "n/a", "unverified"}:
            issues.append("research-context confidence is unknown")
        for name, label in (
            ("inferred_challenges", "source-grounded research problem signal"),
            ("evidence_refs", "research-context evidence references"),
            ("inference_provenance", "research-context inference provenance"),
            ("research_intent_evidence", "research-intent evidence"),
        ):
            if not field(name):
                issues.append(f"research context is missing {label}")

    return issues


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
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read JSON list {path}: {exc}") from exc
    if not isinstance(value, list):
        raise ValueError(f"expected a JSON list in {path}")
    return value


def _validate_normalized_handoff(
    records: list[Any],
    *,
    funding_call_id: str,
    run_id: str,
    expected_agents: set[str],
) -> list[str]:
    """Validate each record before declaring the per-call Batch-3 handoff ready."""
    errors: list[str] = []
    for index, record in enumerate(records):
        label = f"record[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{label} is not a JSON object")
            continue

        if record.get("run_id") != run_id:
            errors.append(f"{label} ({record.get('record_id')}) has a mismatched run_id")
        if record.get("funding_call_id") != funding_call_id:
            errors.append(f"{label} ({record.get('record_id')}) has a mismatched funding_call_id")

        source_url = str(record.get("source_url") or "").strip()
        parsed_source = urlparse(source_url)
        if parsed_source.scheme not in {"http", "https"} or not parsed_source.netloc:
            errors.append(f"{label} ({record.get('record_id')}) has no usable HTTP(S) source URL")

        if record.get("normalization_status") != "NORMALIZED":
            errors.append(
                f"{label} ({record.get('record_id')}) is marked "
                f"{record.get('normalization_status')!r}"
            )

        agent_name = str(record.get("agent_name") or "").strip()
        if agent_name not in expected_agents:
            errors.append(f"{label} ({record.get('record_id')}) has an unknown collection agent: {agent_name!r}")

        for field in ("record_id", "evidence_id", "source_id", "content_hash", "title"):
            if not str(record.get(field) or "").strip():
                errors.append(f"{label} is missing required normalized field {field!r}")

        collected_at = str(record.get("collected_at") or "").strip()
        try:
            if not collected_at:
                raise ValueError("empty timestamp")
            datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"{label} ({record.get('record_id')}) has an invalid collected_at timestamp")

        evidence_values = [
            record.get("problem_statement"),
            record.get("context_summary"),
        ]
        for field in ("evidence_snippets", "excerpt_windows"):
            values = record.get(field)
            if values is None:
                continue
            if isinstance(values, list):
                evidence_values.extend(values)
            else:
                errors.append(f"{label} ({record.get('record_id')}) field {field!r} must be a list")
                if isinstance(values, str):
                    evidence_values.append(values)
        if not any(str(value or "").strip() for value in evidence_values):
            errors.append(f"{label} ({record.get('record_id')}) contains no usable evidence text")

        mission_gate = record.get("mission_relevance")
        if not isinstance(mission_gate, dict) or mission_gate.get("passed") is not True:
            errors.append(f"{label} ({record.get('record_id')}) is missing a passed mission-relevance decision")

    return errors


def execute_batch2(
    *,
    funding_contexts: list[FundingCallContext],
    output_root: Path | str = Path("outputs/batch2_runs"),
    run_id: str | None = None,
    batch1_run_id: str | None = None,
    execution_mode: str = "parallel",
    max_workers: int = 6,
    timeout_budget_seconds: int = 1500,
    default_limit: int | None = None,
    force_refresh: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run all 11 downstream collectors, normalize per call, and stop before Batch-3.

    This deliberately uses collect_only mode. It does not invoke the Batch-3
    tagging, clustering, or trend agents.
    """
    contexts = list(funding_contexts or [])
    if not contexts:
        raise ValueError("At least one validated Batch-1 FundingCallContext is required.")
    for index, context in enumerate(contexts, start=1):
        context_issues = validate_batch1_context(context)
        if context_issues:
            raise ValueError(
                f"FundingCallContext #{index} failed Batch-1 handoff validation: "
                + "; ".join(context_issues)
            )
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
    # Atomic exclusive creation prevents two processes from sharing a run root,
    # even when both start while the directory is still empty.
    try:
        run_root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise FileExistsError(
            f"Batch-2 output directory already exists: {run_root}. "
            "Use a new --run-id so artifacts from separate executions cannot mix."
        ) from exc
    workflow_root = run_root / "workflow"
    workflow_root.mkdir(parents=True, exist_ok=False)
    atomic_write_json(
        workflow_root / "funding_contexts.json",
        {
            "batch1_run_id": batch1_run_id,
            "batch2_run_id": resolved_run_id,
            "funding_contexts": [context.to_dict() for context in contexts],
        },
    )

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
            "allow_llm_analysis": False,
        },
        execution_mode=execution_mode,
        dry_run=dry_run,
        default_limit=default_limit,
        timeout_budget_seconds=timeout_budget_seconds,
        max_workers=max_workers,
        analysis_mode="collect_only",
        report_path=workflow_root / "shared_downstream_collection_plan.json",
        funding_contexts=contexts,
    )

    tasks = collection_report.get("tasks", []) if isinstance(collection_report, dict) else []
    if dry_run:
        report = {
            "run_id": resolved_run_id,
            "batch1_run_id": batch1_run_id,
            "status": "dry_run",
            "dry_run": True,
            "output_root": str(run_root.resolve()),
            "scope": "Batch-2 collection and normalization only",
            "batch3_started": False,
            "normalization_started": False,
            "next_stage": "Batch-2 execution was planned only; no collection or normalization ran",
            "collection": collection_report,
            "funding_calls": [
                {
                    "funding_call_id": context.funding_call_id,
                    "title": context.call_title or context.program_name,
                    "status": "not_run_dry_run",
                }
                for context in contexts
            ],
        }
        atomic_write_json(workflow_root / "batch2_report.json", report)
        report["report_path"] = str((workflow_root / "batch2_report.json").resolve())
        return report

    task_agent_names = {
        str(task.get("agent_name") or "").strip()
        for task in tasks if isinstance(task, dict)
    }
    scheduled_agents = task_agent_names & expected_agents
    unexpected_agents = sorted(task_agent_names - expected_agents)
    missing_agents = sorted(expected_agents - scheduled_agents)
    min_required = minimum_healthy_collectors(len(expected_agents))
    healthy, healthy_count = evaluate_collection_health(tasks, min_required, expected_agents)
    collector_health = {
        "scheduled_agents": sorted(scheduled_agents),
        "missing_agents": missing_agents,
        "unexpected_agents": unexpected_agents,
        "healthy_agents": healthy_count,
        "total_agents": len(expected_agents),
        "required_healthy_agents": min_required,
    }

    report: dict[str, Any] = {
        "run_id": resolved_run_id,
        "batch1_run_id": batch1_run_id,
        "status": "running",
        "output_root": str(run_root.resolve()),
        "scope": "Batch-2 collection and normalization only",
        "batch3_started": False,
        "next_stage": "Batch-3 NLP processing (not executed by this runner)",
        "collector_health": collector_health,
        "collection": collection_report,
        "funding_calls": [],
    }

    if missing_agents or unexpected_agents or not healthy:
        report["status"] = "blocked_collection_health"
        report["blockers"] = []
        if missing_agents:
            report["blockers"].append(
                {"code": "MISSING_COLLECTOR_TASKS", "agents": missing_agents}
            )
        if unexpected_agents:
            report["blockers"].append(
                {"code": "UNEXPECTED_COLLECTOR_TASKS", "agents": unexpected_agents}
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
                "batch1_run_id": batch1_run_id,
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
        normalization_errors: list[Any] = []
        try:
            records = _read_json_list(normalized_paths["records"])
        except ValueError as exc:
            records = []
            normalization_errors.append(str(exc))
        try:
            normalization_errors.extend(_read_json_list(normalized_paths["errors"]))
        except ValueError as exc:
            normalization_errors.append(str(exc))
        try:
            skipped_artifacts = _read_json_list(normalized_paths["skipped_artifacts"])
        except ValueError as exc:
            skipped_artifacts = []
            normalization_errors.append(str(exc))

        required_normalized_outputs = {
            "input_manifest": call_root / "normalized" / "input_manifest.json",
            "evidence_ledger": call_root / "normalized" / "evidence_ledger.db",
            "by_area": normalized_paths["by_area"],
            "by_layer": normalized_paths["by_layer"],
        }
        for output_name, output_path in required_normalized_outputs.items():
            if not output_path.exists():
                normalization_errors.append(
                    f"required normalization output is missing: {output_name}"
                )

        handoff_errors = _validate_normalized_handoff(
            records,
            funding_call_id=call_id,
            run_id=resolved_run_id,
            expected_agents=expected_agents,
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
            "details": gate_details,
        }
        atomic_write_json(call_workflow / "mission_relevance_gate.json", gate_report)

        report["funding_calls"].append(
            {
                "funding_call_id": call_id,
                "title": context.call_title or context.program_name,
                "status": call_status,
                "normalized_records": len(records),
                "normalization_errors": normalization_errors,
                "skipped_artifacts": skipped_artifacts,
                "skipped_artifact_count": len(skipped_artifacts),
                "handoff_errors": handoff_errors,
                "normalized_paths": {
                    name: str(path.resolve()) for name, path in normalized_paths.items()
                },
                "input_manifest": str(required_normalized_outputs["input_manifest"].resolve()),
                "evidence_ledger": str(required_normalized_outputs["evidence_ledger"].resolve()),
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
