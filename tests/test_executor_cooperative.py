import pytest
from datetime import datetime, UTC, timedelta
from typing import Any, Dict
from concurrent.futures import ThreadPoolExecutor
import time
from core.batch_runner import execute_batch_run
from core.crawl_context import CrawlContext, current_crawl_context

def dummy_slow_task(*args, **kwargs):
    # This task loops and checks the crawl context deadline, simulating a crawler
    ctx = current_crawl_context.get(None)
    iterations = 0
    while True:
        if ctx and ctx.check_limits():
            break
        iterations += 1
        time.sleep(0.1)
        if iterations > 20: # Should be stopped by deadline well before this
            break
    return {"status": "success", "iterations": iterations}

@pytest.fixture(autouse=True)
def patch_agent_registry(monkeypatch):
    monkeypatch.setattr("core.batch_runner.run_registered_agent", dummy_slow_task)

def test_executor_cooperative_cancellation():
    start_time = time.time()
    
    # Run a batch with a tiny 1-second timeout
    result = execute_batch_run(
        agent_names=["literature", "funding"],
        mode="configured_scan",
        execution_mode="parallel",
        timeout_budget_seconds=1,
        max_workers=2
    )
    
    duration = time.time() - start_time
    
    # If the executor blocked indefinitely (e.g. because future.cancel() didn't kill threads
    # and threads didn't cooperatively exit), the duration would be >> 1s (like > 2s).
    # Since our dummy task respects CrawlContext deadline, it should exit in ~1 second.
    assert duration < 1.9, f"Executor blocked for {duration} seconds, cooperative cancellation failed!"
    
    assert result["status"] in ("completed", "completed_with_errors")

def dummy_non_cooperative_task(*args, **kwargs):
    # This task loops IGNORING the crawl context deadline
    iterations = 0
    while iterations < 20: # 2.0 seconds
        iterations += 1
        time.sleep(0.1)
    return {"status": "success", "iterations": iterations}

def test_executor_non_cooperative_hangs(monkeypatch):
    monkeypatch.setattr("core.batch_runner.run_registered_agent", dummy_non_cooperative_task)
    start_time = time.time()
    
    # Run a batch with a tiny 0.5-second timeout
    result = execute_batch_run(
        agent_names=["literature", "funding"],
        mode="configured_scan",
        execution_mode="parallel",
        timeout_budget_seconds=0.5,
        max_workers=2
    )
    
    duration = time.time() - start_time
    
    # Because ThreadPoolExecutor.__exit__ (wait=True) waits for all running threads,
    # and future.cancel() does NOT kill them, it will hang for the full 2.0 seconds.
    assert duration >= 1.9, f"Expected non-cooperative executor to hang for ~2.0s, but finished in {duration}s"
    assert result["status"] == "completed_with_errors"
