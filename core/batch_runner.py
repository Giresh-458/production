from __future__ import annotations

import json
import logging
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
    metadata = response.get("metadata", {}) if isinstance(response, dict) else {}
    return {
        "task_id": task["task_id"],
        "agent_name": task["agent_name"],
        "mode": task["mode"],
        "area": task["area"],
        "status": response.get("status", "error"),
        "items_processed": int(response.get("items_processed", 0) or 0),
        "items_saved": int(response.get("items_saved", 0) or 0),
        "outputs_count": len(outputs),
        "errors": list(response.get("errors", []) or []),
        "warnings": list(response.get("warnings", []) or []),
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
    run_id = started_at.isoformat()

    if dry_run:
        report = {
            "generated_at": run_id,
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
                "funding_call_id": funding_context.funding_call_id if funding_context else None,
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
        LOGGER.info("Batch task starting: %s", task["task_id"])
        inp = dict(task.get("input_data") or {})
        inp["run_id"] = run_id
        # Parallel collectors must not race on the shared source registry.
        # Registry synchronization is performed by the batch coordinator.
        if execution_mode == "parallel":
            inp["defer_registry_sync"] = True
        
        pipeline_run_dir = inp.get("pipeline_run_dir")
        if pipeline_run_dir:
            inp["output_dir"] = str(Path(pipeline_run_dir) / "intermediate" / task["agent_name"])

        from core.source_registry import current_force_source_refresh, current_source_area
        token_refresh = current_force_source_refresh.set(bool(inp.get("force_refresh", False)))
        token_area = current_source_area.set(task.get("area"))
        token_ctx = None
        token_q = None
        
        # Combine contexts if multiple are provided, otherwise use single
        contexts_to_process = funding_contexts if funding_contexts else ([funding_context] if funding_context else [])
        
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
            token_q = current_funding_queries.set(list(set(combined_queries)))
            
        inp["deadline"] = deadline.timestamp() if deadline else None

        try:
            from core.crawl_context import CrawlContext, current_crawl_context
            ctx = CrawlContext(deadline=deadline.timestamp() if deadline else None)
            token_crawl = current_crawl_context.set(ctx)
            
            response = run_registered_agent(
                agent_name=task["agent_name"],
                mode=task["mode"],
                area=task["area"],
                input_data=inp,
                funding_context=funding_context,
                funding_contexts=funding_contexts,
            )
            if ctx.stop_reasons:
                response["stop_reasons"] = list(ctx.stop_reasons)
                if response.get("status", "success") == "success":
                    response["status"] = "partial_limit"
            response["metadata"] = response.get("metadata", {})
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

        return _summarize_response(task, response)

    _wl_started = 0
    _wl_completed = 0
    _worker_active_after = 0
    _executor_shutdown_started = None
    _executor_shutdown_finished = None

    if execution_mode == "sequential":
        for index, task in enumerate(tasks):
            if deadline and datetime.now(UTC) >= deadline:
                for pending_task in tasks[index:]:
                    task_results.append({
                        "task_id": pending_task["task_id"],
                        "agent_name": pending_task["agent_name"],
                        "mode": pending_task["mode"],
                        "area": pending_task["area"],
                        "status": "timeout_budget_exceeded",
                        "items_processed": 0,
                        "items_saved": 0,
                        "outputs_count": 0,
                        "errors": [f"batch timeout budget of {timeout_budget_seconds}s exceeded before task start"],
                        "warnings": [],
                        "duration_seconds": 0,
                    })
                break
            task_results.append(run_task(task))
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
                timeout = None
                if deadline:
                    remaining = (deadline - datetime.now(UTC)).total_seconds()
                    if remaining <= 0:
                        for future in list(pending):
                            task = future_to_task[future]
                            future.cancel()
                            task_results.append({
                                "task_id": task["task_id"],
                                "agent_name": task["agent_name"],
                                "mode": task["mode"],
                                "area": task["area"],
                                "status": "timeout_budget_exceeded",
                                "items_processed": 0,
                                "items_saved": 0,
                                "outputs_count": 0,
                                "errors": [f"batch timeout budget of {timeout_budget_seconds}s exceeded before completion"],
                                "warnings": [],
                                "duration_seconds": 0,
                            })
                        pending.clear()
                        break
                    timeout = remaining

                done, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
                if not done and pending and deadline and datetime.now(UTC) >= deadline:
                    continue
                for future in done:
                    try:
                        task_results.append(future.result())
                    except Exception as exc:
                        task = future_to_task[future]
                        task_results.append({
                            "task_id": task["task_id"],
                            "agent_name": task["agent_name"],
                            "mode": task["mode"],
                            "area": task["area"],
                            "status": "error",
                            "items_processed": 0,
                            "items_saved": 0,
                            "outputs_count": 0,
                            "errors": [f"batch runner exception: {exc}"],
                            "warnings": [],
                            "duration_seconds": 0,
                        })
            with _wl_lock:
                _worker_active_after = _wl_started - _wl_completed

            _executor_shutdown_started = _time.time()
        # ThreadPoolExecutor.__exit__ calls shutdown(wait=True) here
        _executor_shutdown_finished = _time.time()

    finished_at = datetime.now(UTC)
    status_counts: Dict[str, int] = {}
    agent_counts: Dict[str, int] = {}
    completed = 0
    timed_out = 0
    error_tasks = 0
    total_outputs = 0
    total_saved = 0
    total_processed = 0

    for item in task_results:
        status = str(item.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
        agent_name = str(item.get("agent_name", "unknown"))
        agent_counts[agent_name] = agent_counts.get(agent_name, 0) + 1
        total_outputs += int(item.get("outputs_count", 0) or 0)
        total_saved += int(item.get("items_saved", 0) or 0)
        total_processed += int(item.get("items_processed", 0) or 0)
        if status == "timeout_budget_exceeded":
            timed_out += 1
        elif status == "error":
            error_tasks += 1
            completed += 1
        else:
            completed += 1

    report = {
        "generated_at": finished_at.isoformat(),
        "status": "completed_with_errors" if error_tasks or timed_out else "completed",
        "config": {
            "mode": mode,
            "execution_mode": execution_mode,
            "dry_run": False,
            "analysis_mode": analysis_mode,
            "default_limit": default_limit,
            "per_agent_limits": per_agent_limits or {},
            "timeout_budget_seconds": timeout_budget_seconds,
            "max_workers": max_workers,
        },
        "metadata": {
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
        },
        "stats": {
            "planned_tasks": len(tasks),
            "completed_tasks": completed,
            "timed_out_tasks": timed_out,
            "error_tasks": error_tasks,
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
