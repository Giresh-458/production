from __future__ import annotations

import threading

from core import batch_runner
from core.funding_context import (
    current_funding_prompt_context,
    current_funding_queries,
)
from core.funding_selection import FundingCallContext, ScoredCall, ValidationResult
from core.source_registry import current_force_source_refresh, current_source_area


def _context(call_id: str) -> FundingCallContext:
    return FundingCallContext.from_scored_call(
        ScoredCall(
            call_id=call_id,
            score=1.0,
            record={"call_id": call_id, "title": f"Program {call_id}"},
            validation=ValidationResult(True, []),
        ),
        mode="test",
    )


def _success_response() -> dict:
    return {
        "status": "success",
        "items_processed": 1,
        "items_saved": 1,
        "outputs": [{"title": "test output"}],
        "outputs_count": 1,
        "errors": [],
        "warnings": [],
        "metadata": {},
    }


def test_sequential_agent_failure_is_recorded_and_later_tasks_continue(
    tmp_path, monkeypatch
):
    calls = []

    def fake_run_registered_agent(agent_name, **kwargs):
        calls.append(agent_name)
        if agent_name == "company":
            raise RuntimeError("simulated collector failure")
        return _success_response()

    monkeypatch.setattr(batch_runner, "run_registered_agent", fake_run_registered_agent)

    report = batch_runner.execute_batch_run(
        agent_names=["company", "expert"],
        area_names=[None],
        mode="configured_scan",
        execution_mode="sequential",
        dry_run=False,
        report_path=tmp_path / "batch.json",
    )

    assert calls == ["company", "expert"]
    assert [task["status"] for task in report["tasks"]] == ["error", "success"]
    assert "simulated collector failure" in report["tasks"][0]["errors"][0]
    assert report["stats"]["error_tasks"] == 1
    assert report["stats"]["completed_tasks"] == 2


def test_contextvars_are_restored_when_funding_context_setup_fails(
    tmp_path, monkeypatch
):
    context = _context("CALL-CONTEXT-FAIL")
    original_prompt = current_funding_prompt_context.set("outer-prompt")
    original_queries = current_funding_queries.set(["outer-query"])
    original_refresh = current_force_source_refresh.set(False)
    original_area = current_source_area.set("RWA")

    def fail_context(*args, **kwargs):
        raise RuntimeError("context builder failed")

    monkeypatch.setattr(
        "core.funding_context.build_agent_specific_funding_context", fail_context
    )
    try:
        report = batch_runner.execute_batch_run(
            agent_names=["company"],
            area_names=["DePIN"],
            mode="configured_scan",
            execution_mode="sequential",
            funding_contexts=[context],
            dry_run=False,
            report_path=tmp_path / "context-failure.json",
        )

        assert report["tasks"][0]["status"] == "error"
        assert current_funding_prompt_context.get() == "outer-prompt"
        assert current_funding_queries.get() == ["outer-query"]
        assert current_force_source_refresh.get() is False
        assert current_source_area.get() == "RWA"
    finally:
        current_source_area.reset(original_area)
        current_force_source_refresh.reset(original_refresh)
        current_funding_queries.reset(original_queries)
        current_funding_prompt_context.reset(original_prompt)


def test_parallel_tasks_keep_funding_context_and_run_attribution_isolated(
    tmp_path, monkeypatch
):
    contexts = [_context("CALL-A"), _context("CALL-B")]
    observed = {}
    lock = threading.Lock()

    monkeypatch.setattr(
        "core.funding_context.build_agent_specific_funding_context",
        lambda agent, context: f"{agent}:{context.funding_call_id}",
    )
    monkeypatch.setattr(
        "core.funding_context.generate_funding_queries",
        lambda context: [context.funding_call_id],
    )

    def fake_run_registered_agent(agent_name, **kwargs):
        with lock:
            observed[agent_name] = {
                "prompt": current_funding_prompt_context.get(),
                "queries": current_funding_queries.get(),
                "run_id": kwargs["input_data"]["run_id"],
            }
        return _success_response()

    monkeypatch.setattr(batch_runner, "run_registered_agent", fake_run_registered_agent)

    report = batch_runner.execute_batch_run(
        agent_names=["company", "expert"],
        area_names=[None],
        mode="configured_scan",
        execution_mode="parallel",
        max_workers=2,
        timeout_budget_seconds=20,
        funding_contexts=contexts,
        base_input_data={"run_id": "PIPELINE-RUN-1"},
        dry_run=False,
        report_path=tmp_path / "parallel.json",
    )

    assert set(observed) == {"company", "expert"}
    for agent_name, details in observed.items():
        assert f"{agent_name}:CALL-A" in details["prompt"]
        assert f"{agent_name}:CALL-B" in details["prompt"]
        assert details["queries"] == ["CALL-A", "CALL-B"]
        assert details["run_id"] == "PIPELINE-RUN-1"

    assert report["config"]["funding_call_ids"] == ["CALL-A", "CALL-B"]
    assert report["config"]["run_id"] == "PIPELINE-RUN-1"
    assert all(task["funding_call_ids"] == ["CALL-A", "CALL-B"] for task in report["tasks"])
    assert all(task["run_id"] == "PIPELINE-RUN-1" for task in report["tasks"])
    assert report["worker_lifecycle"]["active_executor_workers_after_batch_return"] == 0


def test_parallel_deadline_returns_without_waiting_for_unresponsive_worker(
    tmp_path, monkeypatch
):
    import time

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_agent(agent_name, **kwargs):
        started.set()
        release.wait(timeout=5)
        finished.set()
        return _success_response()

    monkeypatch.setattr(batch_runner, "run_registered_agent", slow_agent)
    begin = time.monotonic()
    try:
        report = batch_runner.execute_batch_run(
            agent_names=["company"],
            area_names=[None],
            mode="configured_scan",
            execution_mode="parallel",
            max_workers=1,
            timeout_budget_seconds=1,
            dry_run=False,
            report_path=tmp_path / "deadline.json",
        )
        elapsed = time.monotonic() - begin

        assert started.is_set()
        assert elapsed < 2.5
        assert report["stats"]["timed_out_tasks"] == 1
        assert report["tasks"][0]["status"] == "timeout_budget_exceeded"
        assert report["worker_lifecycle"]["active_executor_workers_after_batch_return"] == 1
        assert "cannot be forcibly stopped" in report["tasks"][0]["warnings"][0]
    finally:
        release.set()
        assert finished.wait(timeout=2)
