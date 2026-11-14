from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from core.agent_registry import AGENT_REGISTRY, run_registered_agent
from core.schemas import ALLOWED_MODES, ALLOWED_RESEARCH_AREAS, normalize_area
from core.funding_selection import FundingCallContext

LOGGER = logging.getLogger(__name__)

BATCH_REPORT_PATH = Path("outputs/workflow/batch_run_report.json")
FULL_COLLECTION_REPORT_PATH = Path("outputs/workflow/full_collection_run_report.json")


def _is_redirected_path(path: Path) -> bool:
    """Reject symlinks, junctions/reparse points, and paths through redirected parents."""
    try:
        if path.is_symlink():
            return True
        try:
            stat_result = path.lstat()
        except FileNotFoundError:
            return Path(os.path.abspath(path)) != path.resolve()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if int(getattr(stat_result, "st_file_attributes", 0) or 0) & reparse_flag:
            return True
        return Path(os.path.abspath(path)) != path.resolve()
    except (OSError, RuntimeError):
        return True


def _count_quarantined_markdown(path: Path) -> int:
    """Count regular Markdown files without following links inside quarantine."""
    if _is_redirected_path(path):
        return 0
    if path.is_file():
        return int(path.suffix.casefold() == ".md")
    if not path.is_dir():
        return 0
    count = 0
    for current, directory_names, file_names in os.walk(path, followlinks=False):
        current_path = Path(current)
        directory_names[:] = [
            name for name in directory_names
            if not _is_redirected_path(current_path / name)
        ]
        count += sum(
            1 for name in file_names
            if (current_path / name).suffix.casefold() == ".md"
            and not _is_redirected_path(current_path / name)
        )
    return count


def _quarantine_timed_out_output(
    task: Dict[str, Any],
    output_dir: Path,
) -> tuple[Path | None, int, str | None]:
    """Preserve timed-out artifacts outside the active intermediate tree."""
    pipeline_run_dir = (task.get("input_data") or {}).get("pipeline_run_dir")
    if not pipeline_run_dir:
        return None, 0, "pipeline_run_dir is unavailable; timed-out artifacts were left in place"

    run_root = Path(pipeline_run_dir)
    intermediate_root = run_root / "intermediate"
    workflow_root = run_root / "workflow"
    quarantine_root = workflow_root / "quarantined_intermediate" / "timeouts"
    output_absolute = Path(os.path.abspath(output_dir))
    intermediate_absolute = Path(os.path.abspath(intermediate_root))

    # Never create a quarantine through a redirected run/intermediate/workflow
    # parent. The output directory itself may be a symlink/junction: rename the
    # link itself into quarantine rather than following its target.
    for protected_root in (run_root, intermediate_root, output_dir.parent, workflow_root, quarantine_root):
        if _is_redirected_path(protected_root):
            return None, 0, f"refusing timeout quarantine through redirected path: {protected_root}"
    if not output_absolute.is_relative_to(intermediate_absolute):
        return None, 0, f"timed-out output path escapes the run intermediate root: {output_absolute}"
    if not (output_dir.exists() or output_dir.is_symlink()):
        return None, 0, None

    agent_segment = re.sub(r"[^A-Za-z0-9._-]+", "-", str(task.get("agent_name") or "agent")).strip("-.") or "agent"
    task_segment = re.sub(r"[^A-Za-z0-9._-]+", "-", str(task.get("task_id") or "task")).strip("-.") or "task"
    destination_parent = quarantine_root / agent_segment
    destination_parent.mkdir(parents=True, exist_ok=True)
    if _is_redirected_path(destination_parent):
        return None, 0, f"timeout quarantine destination is redirected: {destination_parent}"

    destination = destination_parent / task_segment
    suffix = 1
    while destination.exists() or destination.is_symlink():
        destination = destination_parent / f"{task_segment}-{suffix}"
        suffix += 1

    try:
        # Source and destination are within the same run root, so rename keeps
        # the artifact tree intact and does not follow a symlinked output root.
        os.rename(output_dir, destination)
    except OSError as exc:
        return None, 0, f"could not preserve timed-out artifacts in quarantine: {exc}"

    return destination, _count_quarantined_markdown(destination), None


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_agent_names(agent_names: Optional[Iterable[str]]) -> list[str]:
    if not agent_names:
        return sorted(AGENT_REGISTRY.keys())
    normalized: list[str] = []
    for name in agent_names:
        key = str(name).strip().lower()
        if not key:
            continue
        if key not in AGENT_REGISTRY:
            raise ValueError(f"Unknown agent name: {name}")
        normalized.append(key)
    return sorted(dict.fromkeys(normalized))


def _normalize_area_names(area_names: Optional[Iterable[str]], mode: str) -> list[Optional[str]]:
    if mode == "configured_scan":
        if not area_names:
            return sorted(ALLOWED_RESEARCH_AREAS)
        normalized: list[Optional[str]] = []
        for area in area_names:
            # A funding-context collection scan may intentionally be unscoped.
            # None means: do not apply the fixed taxonomy as a source filter;
            # the shared funding context remains the routing authority.
            if area is None or not str(area).strip():
                normalized.append(None)
                continue
            resolved = normalize_area(area)
            if resolved not in ALLOWED_RESEARCH_AREAS:
                raise ValueError(f"Unsupported research area: {area}")
            normalized.append(resolved)
        return list(dict.fromkeys(normalized))

    if not area_names:
        return [None]
    normalized_optional: list[Optional[str]] = []
    for area in area_names:
        resolved = normalize_area(area)
        if resolved is not None and resolved not in ALLOWED_RESEARCH_AREAS:
            raise ValueError(f"Unsupported research area: {area}")
        normalized_optional.append(resolved)
    return list(dict.fromkeys(normalized_optional))


def _merge_input_data(
    *,
    base_input_data: Optional[Dict[str, Any]],
    agent_name: str,
    default_limit: Optional[int],
    per_agent_limits: Optional[Dict[str, int]],
    analysis_mode: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(base_input_data or {})
    payload.setdefault("analysis_mode", analysis_mode)

    limit_value = None
    if per_agent_limits and agent_name in per_agent_limits:
        limit_value = per_agent_limits[agent_name]
    elif default_limit is not None:
        limit_value = default_limit

    if limit_value is not None:
        payload.setdefault("limit", limit_value)
        payload.setdefault("batch_limit", limit_value)

    return payload


def build_batch_tasks(
    *,
    agent_names: Optional[Iterable[str]] = None,
    area_names: Optional[Iterable[str]] = None,
    mode: str = "configured_scan",
    base_input_data: Optional[Dict[str, Any]] = None,
    default_limit: Optional[int] = None,
    per_agent_limits: Optional[Dict[str, int]] = None,
    analysis_mode: str = "collect_only",
) -> list[Dict[str, Any]]:
    if mode not in ALLOWED_MODES:
        raise ValueError(f"Unsupported batch mode: {mode}")

    agents = _normalize_agent_names(agent_names)
    areas = _normalize_area_names(area_names, mode)
    tasks: list[Dict[str, Any]] = []
    for agent_name in agents:
        input_data = _merge_input_data(
            base_input_data=base_input_data,
            agent_name=agent_name,
            default_limit=default_limit,
            per_agent_limits=per_agent_limits,
            analysis_mode=analysis_mode,
        )
        for area in areas:
            task_id = f"{agent_name}:{mode}:{area or 'none'}"
            tasks.append(
                {
                    "task_id": task_id,
                    "agent_name": agent_name,
                    "mode": mode,
                    "area": area,
                    "input_data": dict(input_data),
                }
            )
    return tasks


def _summarize_response(task: Dict[str, Any], response: Dict[str, Any]) -> Dict[str, Any]:
    outputs = response.get("outputs", []) if isinstance(response, dict) else []
    if not isinstance(outputs, list):
        outputs = []
    metadata = response.get("metadata", {}) if isinstance(response, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}
    raw_warnings = response.get("warnings", []) if isinstance(response, dict) else []
    warnings = list(raw_warnings) if isinstance(raw_warnings, (list, tuple)) else (
        [str(raw_warnings)] if raw_warnings else []
    )
    source_urls: set[str] = set()
    for output in outputs if isinstance(outputs, list) else []:
        if isinstance(output, dict):
            source_url = str(output.get("source") or output.get("source_url") or "").strip()
            if source_url.startswith(("http://", "https://")):
                source_urls.add(source_url)
    for warning in warnings:
        for match in re.findall(r"https?://[^\s)\]>,]+", str(warning)):
            source_urls.add(match.rstrip(".,;"))
    return {
        "task_id": task["task_id"],
        "agent_name": task["agent_name"],
        "mode": task["mode"],
        "area": task["area"],
        "status": response.get("status", "error"),
        "items_processed": int(response.get("items_processed", 0) or 0),
        "items_saved": int(response.get("items_saved", 0) or 0),
        "outputs_count": len(outputs),
        "output_paths": sorted({
            str(item.get("markdown_path") or item.get("path") or "").strip()
            for item in outputs
            if isinstance(item, dict)
            and str(item.get("markdown_path") or item.get("path") or "").strip()
        }),
        "errors": list(response.get("errors", []) or []),
        "warnings": warnings,
        "source_urls": sorted(source_urls),
        "duration_seconds": metadata.get("duration_seconds", 0),
        "stop_reasons": list(response.get("stop_reasons", metadata.get("stop_reasons", [])) or []),
        "metadata": {
            "pages_fetched": metadata.get("pages_fetched", 0),
            "documents_collected": metadata.get("documents_collected", 0),
            "http_requests": metadata.get("http_requests", 0),
            "browser_escalations": metadata.get("browser_escalations", 0),
            "browser_attempts": metadata.get("browser_attempts", 0),
            "browser_successes": metadata.get("browser_successes", 0),
            "browser_failures": metadata.get("browser_failures", 0),
            "playwright_used": metadata.get("playwright_used", 0),
            "js_shells_detected": metadata.get("js_shells_detected", 0),
            "exceptions": metadata.get("exceptions", []),
            "source_limits": metadata.get("source_limits", []),
            "duration_seconds": metadata.get("duration_seconds", 0),
            "start_time": metadata.get("start_time"),
            "end_time": metadata.get("end_time"),
        }
    }


from core.file_lock import atomic_write_json

def _save_batch_report(report: Dict[str, Any], report_path: Path = BATCH_REPORT_PATH) -> Path:
    atomic_write_json(report_path, report)
    return report_path


def execute_batch_run(
    *,
    agent_names: Optional[Iterable[str]] = None,
    area_names: Optional[Iterable[str]] = None,
    mode: str = "configured_scan",
    base_input_data: Optional[Dict[str, Any]] = None,
    execution_mode: str = "sequential",
    dry_run: bool = False,
    default_limit: Optional[int] = None,
    per_agent_limits: Optional[Dict[str, int]] = None,
    timeout_budget_seconds: Optional[int] = None,
    max_workers: Optional[int] = None,
    analysis_mode: str = "collect_only",
    report_path: Path = BATCH_REPORT_PATH,
    funding_context: Optional[FundingCallContext] = None,
    funding_contexts: Optional[list[FundingCallContext]] = None,
) -> Dict[str, Any]:
    if execution_mode not in {"sequential", "parallel"}:
        raise ValueError("execution_mode must be one of sequential, parallel")

    started_at = datetime.now(UTC)
    registry_sync_errors: list[str] = []
    registry_synced = False
    if execution_mode == "parallel" and mode == "configured_scan" and not dry_run:
        from core.source_registry import sync_source_registry
        try:
            sync_source_registry()
            registry_synced = True
        except Exception as exc:
            registry_sync_errors.append(
                f"source registry synchronization failed before parallel collection: {exc}"
            )

    tasks = build_batch_tasks(
        agent_names=agent_names,
        area_names=area_names,
        mode=mode,
        base_input_data=base_input_data,
        default_limit=default_limit,
        per_agent_limits=per_agent_limits,
        analysis_mode=analysis_mode,
    )

    deadline = started_at + timedelta(seconds=timeout_budget_seconds) if timeout_budget_seconds else None
    task_results: list[Dict[str, Any]] = []
    run_id = str((base_input_data or {}).get("run_id") or started_at.isoformat())
    contexts_to_process = funding_contexts if funding_contexts else ([funding_context] if funding_context else [])
    funding_call_ids = sorted({
        str(context.funding_call_id).strip()
        for context in contexts_to_process
        if str(getattr(context, "funding_call_id", "") or "").strip()
    })
    tasks_per_agent: dict[str, int] = {}
    for task in tasks:
        agent_name = str(task.get("agent_name") or "")
        tasks_per_agent[agent_name] = tasks_per_agent.get(agent_name, 0) + 1

    def task_output_dir(task: Dict[str, Any]) -> Path | None:
        pipeline_run_dir = (task.get("input_data") or {}).get("pipeline_run_dir")
        if not pipeline_run_dir:
            return None
        output_dir = Path(pipeline_run_dir) / "intermediate" / task["agent_name"]
        if tasks_per_agent.get(task["agent_name"], 0) > 1:
            area_segment = re.sub(
                r"[^A-Za-z0-9._-]+",
                "-",
                str(task.get("area") or "none"),
            ).strip("-") or "none"
            output_dir = output_dir / area_segment
        return output_dir

    task_output_dirs = {task["task_id"]: task_output_dir(task) for task in tasks}

    def timeout_result(
        task: Dict[str, Any],
        *,
        reason: str,
        result: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        output_dir = task_output_dirs.get(task["task_id"])
        quarantined_output_dir = None
        quarantined_artifact_count = 0
        quarantine_error = None
        if output_dir is not None:
            (
                quarantined_output_dir,
                quarantined_artifact_count,
                quarantine_error,
            ) = _quarantine_timed_out_output(task, output_dir)
        timed_out = dict(result or {})
        reported_outputs = timed_out.get("outputs")
        if not isinstance(reported_outputs, list):
            reported_outputs = []
        raw_reported_paths = timed_out.get("output_paths")
        if isinstance(raw_reported_paths, (list, tuple)):
            reported_output_paths = [str(value) for value in raw_reported_paths if str(value)]
        elif isinstance(raw_reported_paths, str) and raw_reported_paths.strip():
            reported_output_paths = [raw_reported_paths.strip()]
        else:
            reported_output_paths = []
        if not reported_output_paths:
            reported_output_paths = [
                str(item.get("markdown_path") or item.get("path") or "")
                for item in reported_outputs
                if isinstance(item, dict) and (item.get("markdown_path") or item.get("path"))
            ]
        try:
            reported_outputs_count = int(timed_out.get("outputs_count", len(reported_outputs)) or 0)
        except (TypeError, ValueError):
            reported_outputs_count = len(reported_outputs) if isinstance(reported_outputs, list) else 0
        try:
            reported_items_processed = int(timed_out.get("items_processed", 0) or 0)
        except (TypeError, ValueError):
            reported_items_processed = 0
        try:
            reported_items_saved = int(timed_out.get("items_saved", 0) or 0)
        except (TypeError, ValueError):
            reported_items_saved = 0
        timed_out.update({
            "source_urls": [],
            "task_id": task["task_id"],
            "run_id": (task.get("input_data") or {}).get("run_id") or run_id,
            "funding_call_ids": funding_call_ids,
            "agent_name": task["agent_name"],
            "mode": task["mode"],
            "area": task["area"],
            "status": "timeout_budget_exceeded",
            "reported_items_processed": reported_items_processed,
            "reported_items_saved": reported_items_saved,
            "reported_outputs_count": reported_outputs_count,
            "reported_output_paths": reported_output_paths,
            "items_processed": 0,
            "items_saved": 0,
            "outputs_count": 0,
            "output_paths": [],
            "errors": list(timed_out.get("errors") or []) + [reason],
            "warnings": list(timed_out.get("warnings") or []),
            "duration_seconds": timed_out.get("duration_seconds", 0),
            "finished_at": timed_out.get("finished_at") or datetime.now(UTC).isoformat(),
            "output_dir": str(Path(os.path.abspath(output_dir))) if output_dir is not None else None,
            "quarantined_output_dir": (
                str(Path(os.path.abspath(quarantined_output_dir)))
                if quarantined_output_dir is not None else None
            ),
            "quarantined_artifact_count": quarantined_artifact_count,
        })
        if quarantine_error:
            timed_out["artifact_quarantine_error"] = quarantine_error
            timed_out["errors"].append(
                f"timed-out task artifacts were preserved in place because quarantine failed: {quarantine_error}"
            )
        return timed_out

    if dry_run:
        report = {
            "generated_at": started_at.isoformat(),
            "status": "dry_run",
            "config": {
                "mode": mode,
                "execution_mode": execution_mode,
                "dry_run": True,
                "analysis_mode": analysis_mode,
                "default_limit": default_limit,
                "per_agent_limits": per_agent_limits or {},
                "timeout_budget_seconds": timeout_budget_seconds,
                "max_workers": max_workers,
                "run_id": run_id,
                "funding_call_id": funding_context.funding_call_id if funding_context else None,
                "funding_call_ids": funding_call_ids,
            },
            "stats": {
                "planned_tasks": len(tasks),
                "completed_tasks": 0,
                "timed_out_tasks": 0,
                "error_tasks": 0,
            },
            "tasks": tasks,
        }
        saved = _save_batch_report(report, report_path)
        report["report_path"] = str(saved.resolve())
        return report

    def run_task(task: Dict[str, Any]) -> Dict[str, Any]:
        if deadline and datetime.now(UTC) >= deadline:
            return timeout_result(
                task,
                reason=f"batch timeout budget of {timeout_budget_seconds}s exceeded before task start",
            )
        LOGGER.info("Batch task starting: %s", task["task_id"])
        inp = dict(task.get("input_data") or {})
        # Preserve the orchestrator's run ID when present so persisted collector
        # artifacts match the selected-call manifest and normalization boundary.
        # Standalone batch runs still receive the batch runner's generated ID.
        if not inp.get("run_id"):
            inp["run_id"] = run_id
        # Parallel collectors must not race on the shared source registry.
        # Registry synchronization is performed by the batch coordinator.
        if execution_mode == "parallel":
            inp["defer_registry_sync"] = True
        
        pipeline_run_dir = inp.get("pipeline_run_dir")
        output_dir = task_output_dirs.get(task["task_id"])
        if pipeline_run_dir and output_dir is not None:
            inp["output_dir"] = str(output_dir)

        from core.source_registry import current_force_source_refresh, current_source_area
        token_refresh = current_force_source_refresh.set(bool(inp.get("force_refresh", False)))
        token_area = current_source_area.set(task.get("area"))
        token_ctx = None
        token_q = None
        
        # Contexts are combined once for the agent's prompt and query routing.
        
        if contexts_to_process:
            from core.funding_context import build_agent_specific_funding_context, generate_funding_queries, current_funding_prompt_context, current_funding_queries
            combined_prompt_ctx = "\n\n---\n\n".join(
                build_agent_specific_funding_context(task["agent_name"], ctx) 
                for ctx in contexts_to_process
            )
            combined_queries = []
            for ctx in contexts_to_process:
                combined_queries.extend(generate_funding_queries(ctx))
                
            token_ctx = current_funding_prompt_context.set(combined_prompt_ctx)
            token_q = current_funding_queries.set(list(dict.fromkeys(combined_queries)))
            
        inp["deadline"] = deadline.timestamp() if deadline else None

        try:
            from core.crawl_context import CrawlContext, current_crawl_context
            ctx = CrawlContext(deadline=deadline.timestamp() if deadline else None)
            token_crawl = current_crawl_context.set(ctx)
            
            try:
                response = run_registered_agent(
                    agent_name=task["agent_name"],
                    mode=task["mode"],
                    area=task["area"],
                    input_data=inp,
                    funding_context=funding_context,
                    funding_contexts=funding_contexts,
                )
            except Exception as exc:
                LOGGER.exception("Batch task agent invocation failed: %s", task["task_id"])
                response = {
                    "status": "error",
                    "items_processed": 0,
                    "items_saved": 0,
                    "outputs": [],
                    "errors": [f"agent invocation failed: {exc}"],
                    "warnings": [],
                    "metadata": {},
                }
            if not isinstance(response, dict):
                response = {
                    "status": "error",
                    "items_processed": 0,
                    "items_saved": 0,
                    "outputs": [],
                    "errors": ["agent returned a non-object response"],
                    "warnings": [],
                    "metadata": {},
                }
            if ctx.stop_reasons:
                response["stop_reasons"] = list(ctx.stop_reasons)
                if response.get("status", "success") == "success":
                    response["status"] = "partial_limit"
            response_metadata = response.get("metadata")
            if not isinstance(response_metadata, dict):
                response_metadata = {}
            response["metadata"] = response_metadata
            response["metadata"]["pages_fetched"] = ctx.pages_fetched
            response["metadata"]["documents_collected"] = ctx.documents_collected
            response["metadata"]["playwright_used"] = ctx.playwright_used
            response["metadata"]["playwright_fallback_failed"] = ctx.playwright_fallback_failed
            response["metadata"]["browser_attempts"] = ctx.browser_attempts
            response["metadata"]["browser_successes"] = ctx.browser_successes
            response["metadata"]["browser_failures"] = ctx.browser_failures
            response["metadata"]["js_shells_detected"] = ctx.js_shells_detected
            response["metadata"]["http_requests"] = ctx.http_requests
            response["metadata"]["browser_escalations"] = ctx.browser_escalations
            response["metadata"]["exceptions"] = ctx.exceptions
            response["metadata"]["source_limits"] = ctx.source_limits_telemetry
        finally:
            from core.crawl_context import current_crawl_context
            if 'token_crawl' in locals():
                current_crawl_context.reset(token_crawl)
            if contexts_to_process:
                from core.funding_context import current_funding_prompt_context, current_funding_queries
                if token_ctx: current_funding_prompt_context.reset(token_ctx)
                if token_q: current_funding_queries.reset(token_q)
            current_force_source_refresh.reset(token_refresh)
            current_source_area.reset(token_area)

        summary = _summarize_response(task, response)
        summary["run_id"] = inp.get("run_id")
        summary["funding_call_ids"] = funding_call_ids
        summary["finished_at"] = datetime.now(UTC).isoformat()
        summary["output_dir"] = str(output_dir.resolve()) if output_dir is not None else None
        return summary

    _wl_started = 0
    _wl_completed = 0
    _worker_active_after = 0
    _executor_shutdown_started = None
    _executor_shutdown_finished = None

    if execution_mode == "sequential":
        for index, task in enumerate(tasks):
            if deadline and datetime.now(UTC) >= deadline:
                for pending_task in tasks[index:]:
                    task_results.append(timeout_result(
                        pending_task,
                        reason=f"batch timeout budget of {timeout_budget_seconds}s exceeded before task start",
                    ))
                break
            result = run_task(task)
            finished_at = datetime.fromisoformat(result["finished_at"].replace("Z", "+00:00"))
            if deadline and finished_at > deadline and result.get("status") != "timeout_budget_exceeded":
                result = timeout_result(
                    task,
                    result=result,
                    reason=f"batch timeout budget of {timeout_budget_seconds}s exceeded before task completion",
                )
            task_results.append(result)
    else:
        workers = max_workers or min(max(len(tasks), 1), 8)
        _wl_lock = threading.Lock()
        _wl_started = 0
        _wl_completed = 0

        def tracked_run_task(task):
            nonlocal _wl_started, _wl_completed
            with _wl_lock:
                _wl_started += 1
            try:
                return run_task(task)
            finally:
                with _wl_lock:
                    _wl_completed += 1

        import time as _time
        _executor_shutdown_started = None
        _executor_shutdown_finished = None

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_task = {executor.submit(tracked_run_task, task): task for task in tasks}
            pending = set(future_to_task.keys())
            while pending:
                if deadline:
                    remaining = (deadline - datetime.now(UTC)).total_seconds()
                    if remaining <= 0:
                        cancelled = set()
                        for future in list(pending):
                            if future.cancel():
                                task = future_to_task[future]
                                task_results.append(timeout_result(
                                    task,
                                    reason=f"batch timeout budget of {timeout_budget_seconds}s exceeded before task start",
                                ))
                                cancelled.add(future)
                        pending.difference_update(cancelled)
                        if not pending:
                            break
                        # Running threads cannot be force-killed safely. Their
                        # CrawlContext deadline asks them to stop; wait for their
                        # actual completion so late artifacts can be removed.
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    else:
                        done, pending = wait(
                            pending,
                            timeout=remaining,
                            return_when=FIRST_COMPLETED,
                        )
                        if not done:
                            continue
                else:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)

                for future in done:
                    task = future_to_task[future]
                    try:
                        result = future.result()
                        finished_at = datetime.fromisoformat(
                            result["finished_at"].replace("Z", "+00:00")
                        )
                        if deadline and finished_at > deadline and result.get("status") != "timeout_budget_exceeded":
                            result = timeout_result(
                                task,
                                result=result,
                                reason=f"batch timeout budget of {timeout_budget_seconds}s exceeded before task completion",
                            )
                        task_results.append(result)
                    except Exception as exc:
                        error_result = {
                            "task_id": task["task_id"],
                            "run_id": (task.get("input_data") or {}).get("run_id") or run_id,
                            "funding_call_ids": funding_call_ids,
                            "source_urls": [],
                            "agent_name": task["agent_name"],
                            "mode": task["mode"],
                            "area": task["area"],
                            "status": "error",
                            "items_processed": 0,
                            "items_saved": 0,
                            "outputs_count": 0,
                            "output_paths": [],
                            "errors": [f"batch runner exception: {exc}"],
                            "warnings": [],
                            "duration_seconds": 0,
                            "finished_at": datetime.now(UTC).isoformat(),
                            "output_dir": (
                                str(task_output_dirs[task["task_id"]].resolve())
                                if task_output_dirs.get(task["task_id"]) is not None else None
                            ),
                        }
                        if deadline and datetime.fromisoformat(error_result["finished_at"]) > deadline:
                            error_result = timeout_result(
                                task,
                                result=error_result,
                                reason=f"batch timeout budget of {timeout_budget_seconds}s exceeded before task completion",
                            )
                        task_results.append(error_result)
            with _wl_lock:
                _worker_active_after = _wl_started - _wl_completed

            _executor_shutdown_started = _time.time()
        # ThreadPoolExecutor.__exit__ calls shutdown(wait=True) here
        _executor_shutdown_finished = _time.time()

    # Parallel workers deliberately defer registry writes. Reconcile the
    # configured source set once, then mark only URLs that those workers
    # actually attempted; this avoids lost new sources and concurrent writes.
    registry_sync: Dict[str, Any] = {
        "status": "per_task" if execution_mode == "sequential" else "pending",
        "synced": registry_synced if execution_mode == "parallel" and mode == "configured_scan" else None,
        "attempted_sources_marked": 0,
        "errors": list(registry_sync_errors),
    }
    if execution_mode == "parallel" and mode == "configured_scan":
        from core.source_registry import (
            build_source_refresh_plan,
            mark_agent_sources_checked,
        )
        for agent_name in sorted({str(task.get("agent_name") or "") for task in tasks}):
            attempted_urls: set[str] = set()
            for item in task_results:
                if (
                    item.get("agent_name") == agent_name
                    and item.get("status") in {"success", "partial_success", "partial_limit"}
                ):
                    attempted_urls.update(item.get("source_urls") or [])
            if attempted_urls:
                try:
                    registry_sync["attempted_sources_marked"] += mark_agent_sources_checked(
                        agent_name,
                        attempted_urls=attempted_urls,
                    )
                except Exception as exc:
                    registry_sync["errors"].append(
                        f"source refresh update failed for agent '{agent_name}': {exc}"
                    )
        try:
            build_source_refresh_plan()
        except Exception as exc:
            registry_sync["errors"].append(f"source refresh plan rebuild failed: {exc}")
        registry_sync["status"] = "error" if registry_sync["errors"] else "success"
    elif execution_mode == "parallel":
        registry_sync["status"] = "not_required"

    task_order = {task["task_id"]: index for index, task in enumerate(tasks)}
    task_results.sort(key=lambda item: task_order.get(item.get("task_id"), len(task_order)))

    finished_at = datetime.now(UTC)
    status_counts: Dict[str, int] = {}
    agent_counts: Dict[str, int] = {}
    completed = 0
    timed_out = 0
    error_tasks = 0
    total_outputs = 0
    total_saved = 0
    total_processed = 0
    partial_tasks = 0

    for item in task_results:
        status = str(item.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
        agent_name = str(item.get("agent_name", "unknown"))
        agent_counts[agent_name] = agent_counts.get(agent_name, 0) + 1
        total_outputs += int(item.get("outputs_count", 0) or 0)
        total_saved += int(item.get("items_saved", 0) or 0)
        total_processed += int(item.get("items_processed", 0) or 0)
        if status in {"partial_success", "partial_limit"}:
            partial_tasks += 1
        if status == "timeout_budget_exceeded":
            timed_out += 1
        elif status == "error":
            error_tasks += 1
            completed += 1
        else:
            completed += 1

    report = {
        "generated_at": finished_at.isoformat(),
        "status": "completed_with_errors" if error_tasks or timed_out or registry_sync["errors"] else "completed",
        "config": {
            "mode": mode,
            "execution_mode": execution_mode,
            "dry_run": False,
            "analysis_mode": analysis_mode,
            "default_limit": default_limit,
            "per_agent_limits": per_agent_limits or {},
            "timeout_budget_seconds": timeout_budget_seconds,
            "max_workers": max_workers,
            "run_id": run_id,
            "funding_call_ids": funding_call_ids,
        },
        "metadata": {
            "run_id": run_id,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
        },
        "stats": {
            "planned_tasks": len(tasks),
            "completed_tasks": completed,
            "timed_out_tasks": timed_out,
            "error_tasks": error_tasks,
            "partial_tasks": partial_tasks,
            "total_items_processed": total_processed,
            "total_items_saved": total_saved,
            "total_outputs": total_outputs,
            "status_counts": status_counts,
            "agent_task_counts": agent_counts,
        },
        "worker_lifecycle": {
            "tasks_started": _wl_started if execution_mode == "parallel" else len(task_results),
            "tasks_completed": _wl_completed if execution_mode == "parallel" else len(task_results),
            "executor_max_workers": max_workers,
            "active_executor_workers_after_batch_return": _worker_active_after if execution_mode == "parallel" else 0,
            "executor_shutdown_started": _executor_shutdown_started if execution_mode == "parallel" else None,
            "executor_shutdown_finished": _executor_shutdown_finished if execution_mode == "parallel" else None,
        },
        "registry_sync": registry_sync,
        "warnings": list(registry_sync["errors"]),
        "tasks": task_results,
    }
    saved = _save_batch_report(report, report_path)
    report["report_path"] = str(saved.resolve())
    return report


def execute_full_collection_run(
    *,
    profile: str = "smoke",
    execution_mode: str = "sequential",
    dry_run: bool = False,
    timeout_budget_seconds: Optional[int] = None,
    max_workers: Optional[int] = None,
    analysis_mode: str = "collect_only",
    report_path: Path = FULL_COLLECTION_REPORT_PATH,
    funding_context: Optional[FundingCallContext] = None,
    funding_contexts: Optional[list[FundingCallContext]] = None,
) -> Dict[str, Any]:
    normalized_profile = str(profile).strip().lower() or "smoke"
    if normalized_profile not in {"smoke", "regression", "refresh"}:
        raise ValueError("profile must be one of smoke, regression, refresh")

    default_limit: Optional[int]
    if normalized_profile == "smoke":
        default_limit = 1
        timeout_budget_seconds = timeout_budget_seconds if timeout_budget_seconds is not None else 600
    elif normalized_profile == "regression":
        default_limit = 2
        timeout_budget_seconds = timeout_budget_seconds if timeout_budget_seconds is not None else 1800
    else:
        default_limit = None
        timeout_budget_seconds = timeout_budget_seconds if timeout_budget_seconds is not None else 3600

    from core.agent_registry import COLLECTION_AGENTS
    target_agents = list(COLLECTION_AGENTS - {"funding"})

    report = execute_batch_run(
        agent_names=target_agents,
        mode="configured_scan",
        execution_mode=execution_mode,
        dry_run=dry_run,
        default_limit=default_limit,
        timeout_budget_seconds=timeout_budget_seconds,
        max_workers=max_workers,
        analysis_mode=analysis_mode,
        report_path=report_path,
        funding_context=funding_context,
        funding_contexts=funding_contexts,
    )
    report["run_mode"] = "full_collection_run"
    report["profile"] = normalized_profile

    saved = _save_batch_report(report, report_path)
    report["report_path"] = str(saved.resolve())
    return report
