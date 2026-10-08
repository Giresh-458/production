from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from core.batch_runner import execute_batch_run
from core.funding_selection import ApplicantProfile, execute_selection, FundingCallContext

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a funding-driven batch collection.")
    parser.add_argument("--mode", choices=["autonomous", "review"], default="autonomous", help="Funding selection mode")
    parser.add_argument("--interactive-choice", default=None, help="Call ID for review mode choice")
    parser.add_argument("--dry-run", action="store_true", help="Plan tasks and write a report without executing agents.")
    parser.add_argument("--execution-mode", choices=["sequential", "parallel"], default="sequential")
    return parser


def format_report(batch_report: dict, funding_context: FundingCallContext) -> str:
    """Format the required execution report."""
    run_id = batch_report.get("generated_at", "unknown-run")
    
    tasks = batch_report.get("tasks", [])
    
    agents_attempted = len(tasks)
    agents_succeeded = sum(1 for t in tasks if t.get("status") == "success")
    agents_failed = sum(1 for t in tasks if t.get("status") in ("error", "timeout_budget_exceeded"))
    
    sources_attempted = sum(t.get("items_processed", 0) for t in tasks)
    sources_succeeded = sum(t.get("items_saved", 0) for t in tasks)
    sources_failed = sources_attempted - sources_succeeded
    
    records_collected = sum(t.get("outputs_count", 0) for t in tasks)
    
    per_agent_counts = {}
    for task in tasks:
        agent = task.get("agent_name", "unknown")
        count = task.get("outputs_count", 0)
        per_agent_counts[agent] = count

    lines = [
        "COLLECTION EXECUTION REPORT",
        "===========================",
        f"run_id: {run_id}",
        f"funding_call_id: {funding_context.funding_call_id}",
        f"selection_mode: {funding_context.selection_mode}",
        f"funding_call_title: {funding_context.call_title}",
        "",
        f"agents_attempted: {agents_attempted}",
        f"agents_succeeded: {agents_succeeded}",
        f"agents_failed: {agents_failed}",
        f"sources_attempted: {sources_attempted}",
        f"sources_succeeded: {sources_succeeded}",
        f"sources_failed: {sources_failed}",
        f"records_collected: {records_collected}",
        "",
        "Per-Agent Counts:"
    ]
    
    for agent, count in sorted(per_agent_counts.items()):
        lines.append(f"{agent.ljust(20)} {count}")
        
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args()

    # 1. Selection
    profile = ApplicantProfile()
    selection_output = execute_selection(
        mode=args.mode,
        profile=profile,
        interactive_choice=args.interactive_choice
    )
    
    if selection_output.get("status") == "NO_SUITABLE_CALL" or not selection_output.get("selected_funding_call_id"):
        LOGGER.error("No suitable funding call selected. Cannot run funding-driven collection.")
        return 1
        
    # 2. Build Context
    funding_context = FundingCallContext.from_dict(selection_output)

    # 3. Collection targeting only the 12 Collection Agents
    from core.agent_registry import COLLECTION_AGENTS
    target_agents = list(COLLECTION_AGENTS - {"funding"})
    
    report = execute_batch_run(
        agent_names=target_agents,
        mode="configured_scan",
        execution_mode=args.execution_mode,
        dry_run=args.dry_run,
        funding_context=funding_context,
        report_path=Path("outputs/workflow/funded_batch_report.json")
    )

    # 4. Print Execution Report
    print(format_report(report, funding_context))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
