from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.agent_registry import DOWNSTREAM_FUNDING_CONTEXT_AGENTS
from core.batch_runner import execute_batch_run
from core.file_lock import atomic_write_json
from core.funding_selection import FundingCallContext, has_substantive_eligibility_evidence
from core.mission_gate import relevance as mission_relevance
from core.normalization import (
    _normalize_intermediate_sections,
    canonicalize_url,
    normalize_intermediate_artifact,
    save_normalized_collection_records,
)
from core.source_registry import build_source_refresh_plan, sync_source_registry


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


def prepare_batch2_source_registry(workflow_root: Path, *, force: bool = True) -> dict[str, Any]:
    """Refresh only the 11 Batch-2 agents; leave Batch-1 registry entries untouched."""
    expected_agents = sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS)
    if len(expected_agents) != 11:
        raise RuntimeError(
            f"Expected exactly 11 downstream collection agents, found {len(expected_agents)}."
        )

    plan_root = Path(workflow_root) / "source_refresh_plans"
    plan_root.mkdir(parents=True, exist_ok=True)
    per_agent_plans: dict[str, dict[str, Any]] = {}
    registry_sync = None
    total_plan_entries = 0
    total_due_entries = 0
    total_skipped_entries = 0
    combined_entries: list[dict[str, Any]] = []

    # Filtered sync preserves all other agents' registry entries. An unfiltered
    # sync here would rewrite/deprecate Batch-1 funding-source entries.
    for agent in expected_agents:
        registry_sync = sync_source_registry(agent_filter=agent)
        plan_path = plan_root / f"{agent}.json"
        refresh_plan = build_source_refresh_plan(
            force=bool(force),
            agent_filter=agent,
            output_path=plan_path,
        )
        per_agent_plan_path = Path(refresh_plan.refresh_plan_path)
        per_agent_payload = json.loads(per_agent_plan_path.read_text(encoding="utf-8"))
        entries = per_agent_payload.get("entries")
        if not isinstance(entries, list):
            raise ValueError(f"Refresh plan for {agent} does not contain an entries list.")
        combined_entries.extend(entries)
        per_agent_plans[agent] = {
            "total_entries": int(refresh_plan.total_entries),
            "due_entries": int(refresh_plan.due_entries),
            "skipped_entries": int(refresh_plan.skipped_entries),
            "path": str(per_agent_plan_path.resolve()),
        }
        total_plan_entries += int(refresh_plan.total_entries)
        total_due_entries += int(refresh_plan.due_entries)
        total_skipped_entries += int(refresh_plan.skipped_entries)

    combined_plan_path = Path(workflow_root) / "source_refresh_plan.json"
    atomic_write_json(
        combined_plan_path,
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "scope": "Batch-2 downstream collection agents only",
            "filters": {
                "agent_filter": None,
                "agent_filters": expected_agents,
                "due_only": False,
                "force": bool(force),
            },
            "stats": {
                "total_entries": total_plan_entries,
                "due_entries": total_due_entries,
                "skipped_entries": total_skipped_entries,
            },
            "entries": sorted(
                combined_entries,
                key=lambda entry: (
                    str(entry.get("agent_name") or ""),
                    str(entry.get("source_id") or ""),
                ),
            ),
            "agents": expected_agents,
            "agent_plans": per_agent_plans,
        },
    )

    return {
        "status": "ready",
        "agents_synced": expected_agents,
        # Preserve the original report keys for existing consumers.
        "total_entries": int(registry_sync.total_entries if registry_sync else 0),
        "active_entries": int(registry_sync.active_entries if registry_sync else 0),
        "deprecated_entries": int(registry_sync.deprecated_entries if registry_sync else 0),
        "registry_total_entries": int(registry_sync.total_entries if registry_sync else 0),
        "registry_active_entries": int(registry_sync.active_entries if registry_sync else 0),
        "registry_deprecated_entries": int(registry_sync.deprecated_entries if registry_sync else 0),
        "refresh_plan_entries": total_plan_entries,
        "refresh_due_entries": total_due_entries,
        "refresh_skipped_entries": total_skipped_entries,
        "refresh_plan_path": str(combined_plan_path.resolve()),
        "per_agent_refresh_plan_paths": {
            agent: values["path"] for agent, values in per_agent_plans.items()
        },
    }


def minimum_healthy_collectors(total_agents: int) -> int:
    """Require a strict majority, with a three-agent floor for larger batches."""
    if total_agents <= 0:
        return 0
    return min(total_agents, max(3, total_agents // 2 + 1))


def is_safe_path_component(value: Any) -> bool:
    """Return whether a user/source-derived identifier is safe as one path component."""
    rendered = str(value or "").strip()
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", rendered))


def validate_batch1_context(context: FundingCallContext) -> list[str]:
    """Reject handoff data that does not satisfy the Batch-1 downstream contract."""
    if not isinstance(context, FundingCallContext):
        return ["input is not a FundingCallContext"]

    issues: list[str] = []
    if not str(context.call_title or "").strip():
        issues.append("funding call title is missing")
    if not is_safe_path_component(context.funding_call_id):
        issues.append("funding_call_id is not a safe path component")
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
            ("inference_provenance", "research-context inference provenance"),
            ("research_intent_evidence", "research-intent evidence"),
        ):
            value = field(name)
            if not value or (
                isinstance(value, (list, tuple, set))
                and not any(str(item or "").strip() for item in value)
            ):
                issues.append(f"research context is missing {label}")

        raw_evidence_refs = field("evidence_refs")
        if isinstance(raw_evidence_refs, str):
            evidence_refs = [raw_evidence_refs]
        elif isinstance(raw_evidence_refs, (list, tuple, set)):
            evidence_refs = list(raw_evidence_refs)
        else:
            evidence_refs = []
        valid_evidence_refs = []
        for reference in evidence_refs:
            parsed_reference = urlparse(str(reference or "").strip())
            if parsed_reference.scheme in {"http", "https"} and parsed_reference.netloc:
                valid_evidence_refs.append(str(reference).strip())
        if not valid_evidence_refs:
            issues.append(
                "research context must contain at least one usable HTTP(S) evidence reference"
            )

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
        "research_context_original_funding_call_id": data.get("research_context_original_funding_call_id"),
    }


def _read_json_list(path: Path) -> list[Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read JSON list {path}: {exc}") from exc
    if not isinstance(value, list):
        raise ValueError(f"expected a JSON list in {path}")
    return value


def validate_normalized_handoff(
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
        call_ids = record.get("funding_call_ids")
        if not isinstance(call_ids, list) or call_ids != [funding_call_id]:
            errors.append(
                f"{label} ({record.get('record_id')}) is not isolated to exactly one funding_call_id"
            )

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

        for field in ("record_id", "evidence_id", "source_id", "content_hash", "title", "layer", "source_type", "evidence_type"):
            if not str(record.get(field) or "").strip():
                errors.append(f"{label} is missing required normalized field {field!r}")

        if not re.fullmatch(r"[0-9a-f]{16}", str(record.get("record_id") or "").lower()):
            errors.append(f"{label} ({record.get('record_id')}) has an invalid record_id format")
        if not re.fullmatch(r"[0-9a-f]{16}", str(record.get("evidence_id") or "").lower()):
            errors.append(f"{label} ({record.get('record_id')}) has an invalid evidence_id format")
        if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("content_hash") or "").lower()):
            errors.append(f"{label} ({record.get('record_id')}) has an invalid content_hash format")
        source_id = str(record.get("source_id") or "")
        if not source_id.startswith(("doi:", "url:", "hash:")):
            errors.append(f"{label} ({record.get('record_id')}) has an unsupported source_id format")

        source_call_ids = record.get("source_funding_call_ids")
        if not isinstance(source_call_ids, list) or funding_call_id not in source_call_ids:
            errors.append(
                f"{label} ({record.get('record_id')}) is missing the original funding-call attribution trace"
            )

        try:
            evidence_strength = float(record.get("evidence_strength"))
            if not math.isfinite(evidence_strength) or not 0.0 <= evidence_strength <= 1.0:
                raise ValueError("outside [0, 1]")
        except (TypeError, ValueError):
            errors.append(f"{label} ({record.get('record_id')}) has an invalid evidence_strength")

        collected_at = str(record.get("collected_at") or "").strip()
        try:
            if not collected_at:
                raise ValueError("empty timestamp")
            parsed_collected_at = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
            if parsed_collected_at.tzinfo is None or parsed_collected_at.utcoffset() is None:
                raise ValueError("timestamp must include a timezone")
        except ValueError:
            errors.append(f"{label} ({record.get('record_id')}) has an invalid or timezone-naive collected_at timestamp")

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


def validate_evidence_ledger(
    ledger_path: Path,
    records: list[dict[str, Any]],
    *,
    funding_call_id: str,
    run_id: str,
) -> list[str]:
    """Verify the SQLite ledger is healthy and contains exactly this call/run's records."""
    errors: list[str] = []
    if not ledger_path.is_file() or ledger_path.stat().st_size == 0:
        return [f"evidence ledger is missing or empty: {ledger_path}"]

    expected = {str(record.get("evidence_id") or ""): record for record in records}
    if "" in expected:
        errors.append("normalized records contain an empty evidence_id")
        expected.pop("", None)

    connection = None
    try:
        connection = sqlite3.connect(str(ledger_path))
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if not quick_check or str(quick_check[0]).lower() != "ok":
            errors.append(f"evidence ledger SQLite quick_check failed: {quick_check!r}")

        total_rows = int(connection.execute("SELECT COUNT(*) FROM canonical_evidence").fetchone()[0])
        rows = connection.execute(
            """
            SELECT evidence_id, record_id, funding_call_id, run_id, agent,
                   source_url, retrieved_at, content_hash, normalization_status, provenance
            FROM canonical_evidence
            WHERE funding_call_id = ? AND run_id = ?
            """,
            (funding_call_id, run_id),
        ).fetchall()

        if total_rows != len(records):
            errors.append(
                f"evidence ledger row count {total_rows} does not match normalized record count {len(records)}"
            )
        actual_ids = {str(row[0]) for row in rows}
        if actual_ids != set(expected):
            errors.append("evidence ledger evidence_id set does not match normalized records")

        for row in rows:
            evidence_id, record_id, call_id, row_run_id, agent, source_url, retrieved_at, content_hash, status, provenance_json = row
            record = expected.get(str(evidence_id))
            if record is None:
                continue
            comparisons = {
                "record_id": (record_id, record.get("record_id")),
                "funding_call_id": (call_id, funding_call_id),
                "run_id": (row_run_id, run_id),
                "agent": (agent, record.get("agent_name")),
                "source_url": (source_url, record.get("source_url")),
                "retrieved_at": (retrieved_at, record.get("collected_at")),
                "content_hash": (content_hash, record.get("content_hash")),
                "normalization_status": (status, "NORMALIZED"),
            }
            for field, (actual, wanted) in comparisons.items():
                if actual != wanted:
                    errors.append(
                        f"evidence ledger {field} mismatch for evidence_id {evidence_id}"
                    )
            try:
                provenance = json.loads(provenance_json)
            except (TypeError, ValueError):
                provenance = None
            if not isinstance(provenance, dict):
                errors.append(f"evidence ledger provenance is invalid for evidence_id {evidence_id}")
            else:
                provenance_comparisons = {
                    "source_url": (provenance.get("source_url"), record.get("source_url")),
                    "source_id": (provenance.get("source_id"), record.get("source_id")),
                    "agent": (provenance.get("agent"), record.get("agent_name")),
                    "funding_call_id": (provenance.get("funding_call_id"), funding_call_id),
                    "run_id": (provenance.get("run_id"), run_id),
                    "retrieved_at": (provenance.get("retrieved_at"), record.get("collected_at")),
                    "raw_record_id": (provenance.get("raw_record_id"), record.get("file_path") or record.get("record_id")),
                }
                for field, (actual, wanted) in provenance_comparisons.items():
                    if actual != wanted:
                        errors.append(
                            f"evidence ledger provenance {field} mismatch for evidence_id {evidence_id}"
                        )
                if provenance.get("funding_call_ids") != [funding_call_id]:
                    errors.append(f"evidence ledger provenance funding_call_ids are not call-scoped for evidence_id {evidence_id}")
                source_call_ids = provenance.get("source_funding_call_ids")
                if not isinstance(source_call_ids, list) or funding_call_id not in source_call_ids:
                    errors.append(f"evidence ledger original funding-call attribution is missing for evidence_id {evidence_id}")
    except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
        errors.append(f"could not validate evidence ledger {ledger_path}: {exc}")
    finally:
        if connection is not None:
            connection.close()

    return errors


def _is_redirected_path(path: Path) -> bool:
    """Reject symlinks, Windows junctions/reparse points, and paths through them."""
    try:
        if path.is_symlink():
            return True
        try:
            stat_result = path.lstat()
        except FileNotFoundError:
            # A not-yet-created path is safe unless one of its existing parents
            # redirects elsewhere (for example, a symlinked run-root ancestor).
            return Path(os.path.abspath(path)) != path.resolve()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if int(getattr(stat_result, "st_file_attributes", 0) or 0) & reparse_flag:
            return True
        # Compare normalized absolute spelling with the resolved path. This also
        # detects a symlinked ancestor when the final path itself is not a link.
        return Path(os.path.abspath(path)) != path.resolve()
    except (OSError, RuntimeError):
        # An unreadable or looping path is unsafe for evidence discovery.
        return True


def _discover_agent_artifacts(agent_root: Path) -> tuple[list[Path], list[Path]]:
    """Discover Markdown without following symlinked files or directories.

    Symlink aliases can point into another collector's directory while still
    resolving somewhere inside the run root. Following them can misattribute,
    or even quarantine, another agent's original evidence.
    """
    markdown_files: list[Path] = []
    symlink_paths: list[Path] = []
    for current, directory_names, file_names in os.walk(agent_root, followlinks=False):
        current_path = Path(current)
        for name in list(directory_names):
            candidate = current_path / name
            if _is_redirected_path(candidate):
                symlink_paths.append(candidate)
                directory_names.remove(name)
        for name in file_names:
            candidate = current_path / name
            if _is_redirected_path(candidate):
                symlink_paths.append(candidate)
            elif candidate.suffix.casefold() == ".md" and candidate.name.casefold() != "insights.md":
                markdown_files.append(candidate)
    return sorted(markdown_files), sorted(symlink_paths)


def audit_collector_artifacts(
    run_root: Path,
    collection_tasks: list[dict[str, Any]],
    *,
    expected_agents: set[str],
    run_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Audit persisted current-run artifacts rather than trusting response counters.

    Invalid artifacts are moved into workflow/quarantined_intermediate (never
    deleted) and cannot enter normalization or the evidence ledger.
    """
    if not isinstance(collection_tasks, list):
        raise TypeError("collector task report must be a list")
    tasks_by_agent: dict[str, list[dict[str, Any]]] = {}
    unexpected_tasks: list[str] = []
    malformed_tasks: list[dict[str, Any]] = []
    for index, task in enumerate(collection_tasks):
        if not isinstance(task, dict):
            malformed_tasks.append({
                "task_index": index,
                "reason": "collector_task_entry_is_not_an_object",
                "actual_type": type(task).__name__,
            })
            continue
        agent = str(task.get("agent_name") or "").strip()
        if not agent:
            malformed_tasks.append({
                "task_index": index,
                "reason": "collector_task_agent_name_missing",
            })
            continue
        if agent not in expected_agents:
            unexpected_tasks.append(agent)
            continue
        tasks_by_agent.setdefault(agent, []).append(task)

    if _is_redirected_path(run_root):
        raise ValueError("Batch-2 run root must not be a symlink or redirected path")
    run_root = run_root.resolve()
    intermediate_root = run_root / "intermediate"
    workflow_root = run_root / "workflow"
    quarantine_root = workflow_root / "quarantined_intermediate"
    resolved_run_root = run_root
    for directory, label in (
        (intermediate_root, "intermediate output"),
        (workflow_root, "workflow"),
        (quarantine_root, "artifact quarantine"),
    ):
        if _is_redirected_path(directory):
            raise ValueError(f"{label} root must not be a symlink or redirected path")
        if directory.exists() and not directory.resolve().is_relative_to(resolved_run_root):
            raise ValueError(f"{label} root resolves outside the Batch-2 run directory")
    quarantine_root.mkdir(parents=True, exist_ok=True)
    agent_audits: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    duplicate_tasks = sorted(agent for agent, items in tasks_by_agent.items() if len(items) != 1)
    missing_task_agents = sorted(expected_agents - set(tasks_by_agent))

    for agent in sorted(expected_agents):
        agent_root = intermediate_root / agent
        task_items = tasks_by_agent.get(agent, [])
        task = task_items[0] if len(task_items) == 1 else None
        agent_root_is_symlink = _is_redirected_path(agent_root)
        agent_root_escapes = (
            not agent_root_is_symlink
            and agent_root.exists()
            and not agent_root.resolve().is_relative_to(intermediate_root.resolve())
        )
        agent_root_unsafe = agent_root_is_symlink or agent_root_escapes
        if agent_root_is_symlink:
            files = []
            symlink_paths = [agent_root]
        elif agent_root.is_dir() and not agent_root_unsafe:
            files, symlink_paths = _discover_agent_artifacts(agent_root)
        else:
            files, symlink_paths = [], []
        valid_files: list[Path] = []
        manual_input_paths: list[str] = []
        agent_issues: list[dict[str, Any]] = []
        if agent_root_is_symlink:
            agent_issues.append({
                "agent_name": agent,
                "reasons": ["agent_output_directory_is_symlink"],
                "output_directory": str(agent_root.absolute()),
            })
        elif agent_root_escapes:
            agent_issues.append({
                "agent_name": agent,
                "reasons": ["agent_output_directory_escapes_intermediate_root"],
                "output_directory": str(agent_root.resolve()),
            })

        # Move only the symlink itself into quarantine. Never move a path
        # reached through a symlinked parent: that can move another agent's
        # original evidence instead of the alias.
        for symlink_path in symlink_paths:
            relative = (
                Path("__agent_output_directory_symlink__")
                if symlink_path == agent_root
                else symlink_path.relative_to(agent_root)
            )
            destination = quarantine_root / agent / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                destination = destination.with_name(
                    f"{destination.name}-{len(quarantined) + 1}"
                )
            quarantine_error = None
            try:
                symlink_path.rename(destination)
            except OSError as exc:
                quarantine_error = str(exc)
            symlink_reasons = (
                ["agent_output_directory_is_symlink"]
                if symlink_path == agent_root
                else ["symlinked_agent_artifact_path"]
            )
            if quarantine_error:
                symlink_reasons.append("artifact_quarantine_failed")
            item = {
                "agent_name": agent,
                "original_path": str(symlink_path.absolute()),
                "quarantined_path": str(destination.absolute()) if quarantine_error is None else None,
                "quarantine_error": quarantine_error,
                "reasons": symlink_reasons,
            }
            quarantined.append(item)
            agent_issues.append(item)

        task_status = str(task.get("status") or "").strip().lower() if task is not None else ""

        for path in files:
            reasons: list[str] = []
            manual_input_source = False
            if task_status == "timeout_budget_exceeded":
                reasons.append("collector_exceeded_batch_timeout")
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(agent_root.resolve()):
                    reasons.append("artifact_path_escapes_agent_output_directory")
                artifact_text = path.read_text(encoding="utf-8")
                parsed_artifact = _normalize_intermediate_sections(path)
                shared_header = parsed_artifact.get("shared_header", {})
                provenance = parsed_artifact.get("provenance", {})
                machine_payload_match = re.search(
                    r"(?ms)^## Machine-Readable Payload[ \t]*\n^\x60\x60\x60json[ \t]*\n(.*?)\n^\x60\x60\x60[ \t]*(?:\n|$)",
                    artifact_text,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                machine_payload: dict[str, Any] = {}
                if not machine_payload_match:
                    reasons.append("artifact_machine_readable_payload_missing")
                else:
                    try:
                        loaded_payload = json.loads(machine_payload_match.group(1))
                        if isinstance(loaded_payload, dict):
                            machine_payload = loaded_payload
                        else:
                            reasons.append("artifact_machine_readable_payload_not_an_object")
                    except (json.JSONDecodeError, TypeError):
                        reasons.append("artifact_machine_readable_payload_invalid_json")
                machine_header = machine_payload.get("shared_header", {})
                machine_provenance = machine_payload.get("provenance", {})
                if not isinstance(machine_header, dict):
                    machine_header = {}
                    reasons.append("artifact_machine_payload_shared_header_invalid")
                if not isinstance(machine_provenance, dict):
                    machine_provenance = {}
                    reasons.append("artifact_machine_payload_provenance_invalid")
                for field in (
                    "title", "source", "layer", "research_area", "agent_name",
                    "agent_version", "collection_mode", "funding_call_id", "run_id",
                    "collected_at",
                ):
                    if str(machine_header.get(field) or "").strip() != str(shared_header.get(field) or "").strip():
                        reasons.append(f"artifact_markdown_and_machine_payload_{field}_mismatch")
                machine_call_ids = machine_header.get("funding_call_ids", [])
                markdown_call_ids = shared_header.get("funding_call_ids", "")
                if isinstance(machine_call_ids, str):
                    machine_call_ids = [item.strip() for item in machine_call_ids.split(",") if item.strip()]
                if isinstance(markdown_call_ids, str):
                    markdown_call_ids = [item.strip() for item in markdown_call_ids.split(",") if item.strip()]
                if not isinstance(machine_call_ids, list):
                    machine_call_ids = []
                    reasons.append("artifact_machine_payload_funding_call_ids_invalid")
                if not isinstance(markdown_call_ids, list):
                    markdown_call_ids = []
                    reasons.append("artifact_markdown_funding_call_ids_invalid")
                if [str(item).strip() for item in machine_call_ids] != [str(item).strip() for item in markdown_call_ids]:
                    reasons.append("artifact_markdown_and_machine_payload_funding_call_ids_mismatch")
                for field in (
                    "source_url", "collected_at", "agent_name", "agent_version",
                    "collection_mode", "document_identity", "content_hash",
                ):
                    if str(machine_provenance.get(field) or "").strip() != str(provenance.get(field) or "").strip():
                        reasons.append(f"artifact_markdown_and_machine_payload_provenance_{field}_mismatch")
                machine_evidence = machine_payload.get("evidence_bundle")
                markdown_evidence = parsed_artifact.get("evidence_bundle")
                if not isinstance(machine_evidence, dict):
                    reasons.append("artifact_machine_payload_evidence_bundle_missing_or_invalid")
                elif not isinstance(markdown_evidence, dict):
                    reasons.append("artifact_markdown_evidence_bundle_missing_or_invalid")
                else:
                    if str(machine_evidence.get("page_title") or "").strip() != str(markdown_evidence.get("page_title") or "").strip():
                        reasons.append("artifact_markdown_and_machine_payload_evidence_page_title_mismatch")
                    for field in (
                        "section_headings", "key_paragraphs", "bullet_lists",
                        "quoted_passages", "links_found", "named_entities_or_programs",
                        "evidence_snippets", "excerpt_windows",
                    ):
                        machine_items = machine_evidence.get(field, [])
                        markdown_items = markdown_evidence.get(field, [])
                        if not isinstance(machine_items, list) or not isinstance(markdown_items, list):
                            reasons.append(f"artifact_evidence_bundle_{field}_invalid")
                            continue
                        if [str(item).strip() for item in machine_items] != [str(item).strip() for item in markdown_items]:
                            reasons.append(f"artifact_markdown_and_machine_payload_evidence_{field}_mismatch")
                if str(shared_header.get("agent_name") or "").strip() != agent:
                    reasons.append("artifact_agent_name_header_missing_or_mismatched")
                if str(shared_header.get("run_id") or "").strip() != run_id:
                    reasons.append("artifact_run_id_header_missing_or_mismatched")

                header_source = str(shared_header.get("source") or "").strip()
                provenance_source = str(provenance.get("source_url") or "").strip()
                header_parsed = urlparse(header_source)
                provenance_parsed = urlparse(provenance_source)
                collection_mode = str(shared_header.get("collection_mode") or "").strip().casefold()
                manual_input_source = (
                    collection_mode == "manual_text"
                    and header_parsed.scheme == "manual"
                    and bool(header_parsed.netloc)
                    and bool(header_parsed.path.strip("/"))
                    and not header_parsed.query
                    and not header_parsed.fragment
                    and provenance_source == header_source
                    and provenance_parsed.scheme == "manual"
                    and bool(provenance_parsed.netloc)
                    and bool(provenance_parsed.path.strip("/"))
                    and not provenance_parsed.query
                    and not provenance_parsed.fragment
                )
                if not manual_input_source:
                    if header_parsed.scheme not in {"http", "https"} or not header_parsed.netloc:
                        reasons.append("artifact_header_source_url_invalid")
                    if provenance_parsed.scheme not in {"http", "https"} or not provenance_parsed.netloc:
                        reasons.append("artifact_provenance_source_url_missing_or_invalid")
                    if (
                        header_parsed.scheme in {"http", "https"}
                        and header_parsed.netloc
                        and provenance_parsed.scheme in {"http", "https"}
                        and provenance_parsed.netloc
                    ):
                        header_canonical = canonicalize_url(header_source)
                        provenance_canonical = canonicalize_url(provenance_source)
                        if header_canonical != provenance_canonical:
                            reasons.append("artifact_header_and_provenance_source_url_conflict")
                if str(provenance.get("agent_name") or "").strip() != agent:
                    reasons.append("artifact_provenance_agent_name_missing_or_mismatched")
                if str(provenance.get("collected_at") or "").strip() != str(shared_header.get("collected_at") or "").strip():
                    reasons.append("artifact_header_and_provenance_timestamp_conflict")
                document_identity = str(provenance.get("document_identity") or "").strip()
                machine_source = str(machine_header.get("source") or "").strip()
                machine_title = str(machine_header.get("title") or "").strip()
                expected_document_identity = hashlib.sha256(
                    f"{machine_source}|{machine_title}".encode("utf-8")
                ).hexdigest()
                if not document_identity:
                    reasons.append("artifact_provenance_document_identity_missing")
                elif document_identity != expected_document_identity:
                    reasons.append("artifact_provenance_document_identity_mismatch")

                provenance_content_hash = str(provenance.get("content_hash") or "").strip()
                raw_content = str(machine_payload.get("raw_content") or "")
                markdown_raw_content = str(parsed_artifact.get("raw_content") or "")
                normalize_source_text = lambda value: str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
                if normalize_source_text(markdown_raw_content) != normalize_source_text(raw_content):
                    reasons.append("artifact_markdown_and_machine_payload_raw_content_mismatch")
                if not provenance_content_hash:
                    reasons.append("artifact_provenance_content_hash_missing")
                elif not raw_content:
                    reasons.append("artifact_machine_payload_raw_content_missing_for_hash_verification")
                else:
                    expected_content_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
                    if provenance_content_hash != expected_content_hash:
                        reasons.append("artifact_provenance_content_hash_mismatch")

                record = normalize_intermediate_artifact(path)
                if str(record.get("agent_name") or "").strip() != agent:
                    reasons.append("artifact_agent_name_mismatch")
                if str(record.get("run_id") or "").strip() != run_id:
                    reasons.append("artifact_run_id_mismatch")
                source_url = str(record.get("source_url") or "").strip()
                parsed_source = urlparse(source_url)
                if not manual_input_source and (
                    parsed_source.scheme not in {"http", "https"} or not parsed_source.netloc
                ):
                    reasons.append("artifact_source_url_invalid")
                collected_at = datetime.fromisoformat(
                    str(record.get("collected_at") or "").replace("Z", "+00:00")
                )
                if collected_at.tzinfo is None or collected_at.utcoffset() is None:
                    reasons.append("artifact_collected_at_timezone_missing")
                evidence_values = [
                    record.get("problem_statement"),
                    record.get("context_summary"),
                    *(record.get("evidence_snippets") or []),
                    *(record.get("excerpt_windows") or []),
                ]
                if not any(str(value or "").strip() for value in evidence_values):
                    reasons.append("artifact_has_no_evidence_text")
            except Exception as exc:
                reasons.append(f"artifact_parse_failed:{type(exc).__name__}:{exc}")

            if reasons:
                relative = path.relative_to(agent_root)
                destination = quarantine_root / agent / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    destination = destination.with_name(
                        f"{destination.stem}-{len(quarantined) + 1}{destination.suffix}"
                    )
                quarantine_error = None
                try:
                    shutil.move(str(path), str(destination))
                except OSError as exc:
                    quarantine_error = str(exc)
                    reasons.append("artifact_quarantine_failed")
                item = {
                    "agent_name": agent,
                    "original_path": str(path.resolve()),
                    "quarantined_path": str(destination.resolve()) if quarantine_error is None else None,
                    "quarantine_error": quarantine_error,
                    "reasons": reasons,
                }
                quarantined.append(item)
                agent_issues.append(item)
                continue

            valid_files.append(path)
            if manual_input_source:
                manual_input_paths.append(str(path.resolve()))

        if len(task_items) > 1:
            agent_issues.append({
                "agent_name": agent,
                "reasons": ["duplicate_collector_task_entries"],
                "task_count": len(task_items),
            })
        if task is not None:
            reported_paths = task.get("output_paths")
            if reported_paths is not None and not isinstance(reported_paths, list):
                agent_issues.append({
                    "agent_name": agent,
                    "reasons": ["collector_output_paths_must_be_a_list"],
                })
            elif isinstance(reported_paths, list):
                resolved_reported_paths: set[Path] = set()
                for raw_output_path in reported_paths:
                    try:
                        output_path = Path(str(raw_output_path)).resolve()
                        resolved_reported_paths.add(output_path)
                        if not output_path.is_relative_to(agent_root.resolve()):
                            agent_issues.append({
                                "agent_name": agent,
                                "output_path": str(raw_output_path),
                                "reasons": ["reported_output_path_escapes_agent_output_directory"],
                            })
                        elif not output_path.is_file():
                            agent_issues.append({
                                "agent_name": agent,
                                "output_path": str(raw_output_path),
                                "reasons": ["reported_output_path_does_not_exist"],
                            })
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        agent_issues.append({
                            "agent_name": agent,
                            "output_path": str(raw_output_path),
                            "reasons": [f"reported_output_path_invalid:{exc}"],
                        })
                observed_paths = {
                    Path(item["original_path"])
                    for item in quarantined
                    if item.get("agent_name") == agent and item.get("original_path")
                }
                observed_paths.update(path.resolve() for path in valid_files)
                if resolved_reported_paths != observed_paths:
                    agent_issues.append({
                        "agent_name": agent,
                        "reasons": ["reported_output_paths_do_not_match_observed_markdown_artifacts"],
                        "reported_paths": sorted(str(path) for path in resolved_reported_paths),
                        "observed_paths": sorted(str(path) for path in observed_paths),
                    })
            try:
                reported_count = int(task.get("outputs_count", 0) or 0)
            except (TypeError, ValueError):
                reported_count = 0
            if reported_count != len(valid_files):
                mismatch = {
                    "agent_name": agent,
                    "reasons": ["collector_output_count_does_not_match_persisted_artifacts"],
                    "reported_outputs_count": reported_count,
                    "persisted_valid_artifact_count": len(valid_files),
                }
                agent_issues.append(mismatch)
            task["reported_outputs_count"] = reported_count
            task["persisted_artifact_count"] = len(valid_files)
            task["outputs_count"] = len(valid_files)
            task["artifact_audit_issues"] = agent_issues
            task["warnings"] = list(task.get("warnings") or [])
            if manual_input_paths:
                task["warnings"].append(
                    "Manual-text artifacts are user-supplied inputs, not externally verified sources; "
                    "their normalized records remain NEEDS_REVIEW and cannot satisfy the Batch-3 handoff."
                )
            task["errors"] = list(task.get("errors") or [])
            status = str(task.get("status") or "").strip().lower()
            if valid_files and status not in {"success", "partial_success", "partial_limit"}:
                task["status"] = "partial_success"
                task["warnings"].append(
                    "Valid current-run intermediate artifacts were persisted despite a non-success task status."
                )
            elif not valid_files and status in {"success", "partial_success", "partial_limit"}:
                task["status"] = "error"
                task["errors"].append(
                    "Collector reported usable outputs but no valid current-run intermediate artifacts were persisted."
                )

        if agent_issues:
            issues.extend(agent_issues)
        agent_audits.append({
            "agent_name": agent,
            "task_entries": len(task_items),
            "persisted_valid_artifacts": len(valid_files),
            "manual_input_artifacts": len(manual_input_paths),
            "manual_input_paths": manual_input_paths,
            "quarantined_artifacts": sum(1 for item in agent_issues if "quarantined_path" in item),
            "issues": agent_issues,
        })

    critical_manifest_reasons = {
        "agent_output_directory_escapes_intermediate_root",
        "agent_output_directory_is_symlink",
        "symlinked_agent_artifact_path",
        "collector_output_paths_must_be_a_list",
        "reported_output_path_escapes_agent_output_directory",
        "reported_output_path_does_not_exist",
    }
    manifest_issues = []
    for issue in issues:
        reasons = set(issue.get("reasons") or [])
        if reasons & critical_manifest_reasons or any(
            reason.startswith("reported_output_path_invalid:")
            for reason in reasons
        ):
            manifest_issues.append(issue)
            continue
        if "reported_output_paths_do_not_match_observed_markdown_artifacts" in reasons:
            # A collector may fail after persisting valid artifacts and therefore
            # return no output paths. That is a partial-success case, not a
            # manifest escape. Quarantined files remain part of the observed set;
            # a non-empty manifest that omits or invents paths is critical.
            if issue.get("reported_paths"):
                manifest_issues.append(issue)

    audit = {
        "status": "passed" if not issues and not unexpected_tasks and not malformed_tasks and not missing_task_agents else "completed_with_issues",
        "run_id": run_id,
        "expected_agents": sorted(expected_agents),
        "agents": agent_audits,
        "missing_task_agents": missing_task_agents,
        "duplicate_task_agents": duplicate_tasks,
        "malformed_task_entries": malformed_tasks,
        "unexpected_task_agents": sorted(set(unexpected_tasks)),
        "quarantined_artifacts": quarantined,
        "quarantine_failures": [
            item for item in quarantined if item.get("quarantine_error")
        ],
        "manifest_issues": manifest_issues,
        "issues": issues,
    }
    return collection_tasks, audit


def execute_batch2(
    *,
    funding_contexts: list[FundingCallContext],
    output_root: Path | str = Path("outputs/batch2_runs"),
    run_id: str | None = None,
    batch1_run_id: str | None = None,
    batch1_acceptance: dict[str, Any] | None = None,
    context_validation: dict[str, Any] | None = None,
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
    nested_acceptance = (
        batch1_acceptance.get("acceptance")
        if isinstance(batch1_acceptance, dict) else None
    )
    global_acceptance_valid = (
        isinstance(batch1_acceptance, dict)
        and batch1_acceptance.get("accepted") is True
        and str(batch1_acceptance.get("status") or "").upper() == "ACCEPTED"
        and batch1_acceptance.get("source_coverage_verified") is True
        and isinstance(nested_acceptance, dict)
        and nested_acceptance.get("accepted") is True
        and str(nested_acceptance.get("status") or "").upper() == "ACCEPTED"
        and isinstance(nested_acceptance.get("blockers"), list)
        and nested_acceptance.get("blockers") == []
        and nested_acceptance.get("blocker_count") == 0
    )
    if global_acceptance_valid:
        attested_batch1_run_id = str(batch1_acceptance.get("batch1_run_id") or "").strip()
        if not attested_batch1_run_id:
            raise ValueError("Batch-1 acceptance attestation is missing batch1_run_id.")
        if batch1_run_id and str(batch1_run_id).strip() != attested_batch1_run_id:
            raise ValueError("Batch-1 acceptance attestation run ID does not match batch1_run_id.")
        batch1_run_id = attested_batch1_run_id
    else:
        # Independent Batch-2 execution may use individually validated Batch-1
        # contexts even when an unrelated configured source blocks the global
        # Batch-1 scan. This does not grant global Batch-1 acceptance and does
        # not alter the full pipeline's global acceptance gate.
        if not isinstance(context_validation, dict):
            raise ValueError(
                "Batch-2 requires either an ACCEPTED global Batch-1 attestation "
                "or an independently validated selected-context handoff."
            )
        validated_ids = context_validation.get("validated_funding_call_ids")
        actual_ids = sorted(str(context.funding_call_id or "").strip() for context in contexts)
        if (
            str(context_validation.get("status") or "").upper() != "VALIDATED"
            or context_validation.get("scope") != "selected_contexts_only"
            or not str(context_validation.get("batch1_run_id") or "").strip()
            or not isinstance(validated_ids, list)
            or sorted(str(value).strip() for value in validated_ids) != actual_ids
            or len(set(actual_ids)) != len(actual_ids)
            or (batch1_run_id and str(batch1_run_id).strip() != str(context_validation.get("batch1_run_id")).strip())
        ):
            raise ValueError(
                "Batch-2 selected-context validation does not match the supplied Batch-1 run and funding-call IDs."
            )
        batch1_run_id = str(context_validation["batch1_run_id"]).strip()
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

    resolved_run_id = str(run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")).strip()
    if not is_safe_path_component(resolved_run_id):
        raise ValueError("run_id must be a safe path component containing only letters, numbers, dots, underscores, or hyphens")

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

    source_registry_report: dict[str, Any] = {"status": "skipped_dry_run"}
    if not dry_run:
        try:
            source_registry_report = prepare_batch2_source_registry(
                workflow_root,
                force=bool(force_refresh),
            )
        except Exception as exc:
            report = {
                "run_id": resolved_run_id,
                "batch1_run_id": batch1_run_id,
                "status": "blocked_source_registry",
                "dry_run": False,
                "output_root": str(run_root.resolve()),
                "scope": "Batch-2 collection and normalization only",
                "batch3_started": False,
                "source_registry": {"status": "error", "error": str(exc)},
                "blockers": [{"code": "SOURCE_REGISTRY_PREPARATION_FAILED", "message": str(exc)}],
                "funding_calls": [
                    {"funding_call_id": context.funding_call_id, "status": "not_started"}
                    for context in contexts
                ],
            }
            atomic_write_json(workflow_root / "batch2_report.json", report)
            report["report_path"] = str((workflow_root / "batch2_report.json").resolve())
            return report

    try:
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
            # The coordinator synchronized the registry once before worker launch.
            "defer_registry_sync": True,
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
    except Exception as exc:
        report = {
            "run_id": resolved_run_id,
            "batch1_run_id": batch1_run_id,
            "status": "blocked_collection_execution",
            "output_root": str(run_root.resolve()),
            "scope": "Batch-2 collection and normalization only",
            "batch3_started": False,
            "source_registry": source_registry_report,
            "blockers": [{"code": "COLLECTION_EXECUTION_EXCEPTION", "message": str(exc)}],
            "funding_calls": [
                {"funding_call_id": context.funding_call_id, "status": "not_started"}
                for context in contexts
            ],
        }
        atomic_write_json(workflow_root / "batch2_report.json", report)
        report["report_path"] = str((workflow_root / "batch2_report.json").resolve())
        return report

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
            "source_registry": source_registry_report,
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

    try:
        tasks, artifact_audit = audit_collector_artifacts(
            run_root,
            tasks,
            expected_agents=expected_agents,
            run_id=resolved_run_id,
        )
    except Exception as exc:
        audit_error = {
            "status": "error",
            "run_id": resolved_run_id,
            "error": str(exc),
        }
        atomic_write_json(workflow_root / "collector_artifact_audit.json", audit_error)
        report = {
            "run_id": resolved_run_id,
            "batch1_run_id": batch1_run_id,
            "status": "blocked_artifact_audit",
            "output_root": str(run_root.resolve()),
            "scope": "Batch-2 collection and normalization only",
            "batch3_started": False,
            "source_registry": source_registry_report,
            "collection": collection_report,
            "collector_artifact_audit": audit_error,
            "blockers": [{"code": "COLLECTOR_ARTIFACT_AUDIT_FAILED", "message": str(exc)}],
            "funding_calls": [
                {"funding_call_id": context.funding_call_id, "status": "not_started"}
                for context in contexts
            ],
        }
        atomic_write_json(workflow_root / "batch2_report.json", report)
        report["report_path"] = str((workflow_root / "batch2_report.json").resolve())
        return report

    collection_report["tasks"] = tasks
    atomic_write_json(workflow_root / "shared_downstream_collection_plan.json", collection_report)

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
        "collector_artifact_audit": artifact_audit,
        "source_registry": source_registry_report,
        "collection": collection_report,
        "funding_calls": [],
    }

    if (
        missing_agents
        or unexpected_agents
        or artifact_audit["duplicate_task_agents"]
        or artifact_audit["malformed_task_entries"]
        or artifact_audit["quarantine_failures"]
        or artifact_audit["manifest_issues"]
        or not healthy
    ):
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
        if artifact_audit["duplicate_task_agents"]:
            report["blockers"].append(
                {"code": "DUPLICATE_COLLECTOR_TASKS", "agents": artifact_audit["duplicate_task_agents"]}
            )
        if artifact_audit["malformed_task_entries"]:
            report["blockers"].append(
                {"code": "MALFORMED_COLLECTOR_TASKS", "tasks": artifact_audit["malformed_task_entries"]}
            )
        if artifact_audit["quarantine_failures"]:
            report["blockers"].append(
                {"code": "ARTIFACT_QUARANTINE_FAILED", "artifacts": artifact_audit["quarantine_failures"]}
            )
        if artifact_audit["manifest_issues"]:
            report["blockers"].append(
                {"code": "COLLECTOR_OUTPUT_MANIFEST_MISMATCH", "issues": artifact_audit["manifest_issues"]}
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

        handoff_errors = validate_normalized_handoff(
            records,
            funding_call_id=call_id,
            run_id=resolved_run_id,
            expected_agents=expected_agents,
        )
        ledger_errors = validate_evidence_ledger(
            required_normalized_outputs["evidence_ledger"],
            records,
            funding_call_id=call_id,
            run_id=resolved_run_id,
        )
        normalization_errors.extend(ledger_errors)

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
                "evidence_ledger_errors": ledger_errors,
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
