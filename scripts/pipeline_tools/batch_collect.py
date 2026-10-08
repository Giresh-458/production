from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.batch_runner import BATCH_REPORT_PATH, execute_batch_run


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_agent_limits(values: list[str]) -> dict[str, int]:
    parsed: dict[str, int] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"Invalid --agent-limit value: {item}. Use agent=limit.")
        agent, raw_limit = item.split("=", 1)
        parsed[agent.strip().lower()] = int(raw_limit.strip())
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a batch collection job across RIF agents.")
    parser.add_argument("--agents", help="Comma-separated agent names. Defaults to all agents.")
    parser.add_argument("--areas", help="Comma-separated research areas. Defaults to all canonical areas for configured_scan.")
    parser.add_argument("--mode", choices=["configured_scan", "manual_url", "manual_text"], default="configured_scan")
    parser.add_argument("--execution-mode", choices=["sequential", "parallel"], default="sequential")
    parser.add_argument("--dry-run", action="store_true", help="Plan tasks and write a report without executing agents.")
    parser.add_argument("--default-limit", type=int, default=None, help="Default per-agent limit injected into input_data.")
    parser.add_argument(
        "--agent-limit",
        action="append",
        default=[],
        help="Per-agent limit override in the form agent=limit. Can be repeated.",
    )
    parser.add_argument("--timeout-budget", type=int, default=None, help="Overall batch timeout budget in seconds.")
    parser.add_argument("--max-workers", type=int, default=None, help="Maximum worker threads for parallel mode.")
    parser.add_argument("--analysis-mode", choices=["collect_only", "analyze"], default="collect_only")
    parser.add_argument("--input-json", default=None, help="Optional JSON object used as shared input_data for all tasks.")
    parser.add_argument("--url", default=None, help="Convenience URL input for manual_url mode.")
    parser.add_argument("--text", default=None, help="Convenience text input for manual_text mode.")
    parser.add_argument("--title", default=None, help="Convenience title input for manual_text mode.")
    parser.add_argument("--source", default=None, help="Convenience source input for manual_text mode.")
    parser.add_argument("--report-path", default=str(BATCH_REPORT_PATH), help="Where to save the batch report JSON.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_data = json.loads(args.input_json) if args.input_json else {}
    for key in ("url", "text", "title", "source"):
        value = getattr(args, key)
        if value is not None:
            input_data[key] = value
    report = execute_batch_run(
        agent_names=_parse_csv(args.agents) or None,
        area_names=_parse_csv(args.areas) or None,
        mode=args.mode,
        base_input_data=input_data or None,
        execution_mode=args.execution_mode,
        dry_run=args.dry_run,
        default_limit=args.default_limit,
        per_agent_limits=_parse_agent_limits(args.agent_limit),
        timeout_budget_seconds=args.timeout_budget,
        max_workers=args.max_workers,
        analysis_mode=args.analysis_mode,
        report_path=Path(args.report_path),
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
