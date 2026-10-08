from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.batch_runner import execute_batch_run, execute_full_collection_run
from core.intelligence import refresh_intelligence_views
from core.regression import (
    build_snapshot_diff,
    collect_regression_snapshot,
    load_latest_regression_report,
    save_regression_report,
)


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
    parser = argparse.ArgumentParser(description="Run a repeatable regression job and save a comparison report.")
    parser.add_argument("--scope", choices=["batch", "full"], default="batch")
    parser.add_argument("--profile", choices=["smoke", "regression", "refresh"], default="smoke")
    parser.add_argument("--agents", help="Comma-separated agent names for batch scope.")
    parser.add_argument("--areas", help="Comma-separated research areas.")
    parser.add_argument("--mode", choices=["configured_scan", "manual_url", "manual_text"], default="configured_scan")
    parser.add_argument("--execution-mode", choices=["sequential", "parallel"], default="sequential")
    parser.add_argument("--dry-run", action="store_true", help="Plan the regression run without executing agents.")
    parser.add_argument("--default-limit", type=int, default=None)
    parser.add_argument("--agent-limit", action="append", default=[])
    parser.add_argument("--timeout-budget", type=int, default=None)
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--analysis-mode", choices=["collect_only", "analyze"], default="collect_only")
    parser.add_argument("--input-json", default=None, help="Optional JSON object used as shared input_data.")
    parser.add_argument("--url", default=None)
    parser.add_argument("--text", default=None)
    parser.add_argument("--title", default=None)
    parser.add_argument("--source", default=None)
    parser.add_argument("--outputs-root", default="outputs")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    outputs_root = Path(args.outputs_root)
    previous_report = load_latest_regression_report()
    previous_snapshot = previous_report.get("snapshot_after") if isinstance(previous_report, dict) else None
    snapshot_before = collect_regression_snapshot()

    input_data = json.loads(args.input_json) if args.input_json else {}
    for key in ("url", "text", "title", "source"):
        value = getattr(args, key)
        if value is not None:
            input_data[key] = value

    if args.scope == "full":
        execution_report = execute_full_collection_run(
            profile=args.profile,
            execution_mode=args.execution_mode,
            dry_run=args.dry_run,
            timeout_budget_seconds=args.timeout_budget,
            max_workers=args.max_workers,
            analysis_mode=args.analysis_mode,
        )
    else:
        execution_report = execute_batch_run(
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
        )

    intelligence_refresh = None
    if not args.dry_run:
        intelligence_refresh = {
            key: str(path.resolve())
            for key, path in refresh_intelligence_views(outputs_root).items()
        }

    snapshot_after = collect_regression_snapshot()
    diff_vs_previous = build_snapshot_diff(previous_snapshot, snapshot_after)
    diff_vs_before = build_snapshot_diff(snapshot_before, snapshot_after)

    report = {
        "generated_at": snapshot_after.get("generated_at"),
        "scope": args.scope,
        "profile": args.profile if args.scope == "full" else None,
        "dry_run": bool(args.dry_run),
        "config": {
            "agents": _parse_csv(args.agents),
            "areas": _parse_csv(args.areas),
            "mode": args.mode,
            "execution_mode": args.execution_mode,
            "analysis_mode": args.analysis_mode,
            "default_limit": args.default_limit,
            "agent_limits": _parse_agent_limits(args.agent_limit),
            "timeout_budget": args.timeout_budget,
            "max_workers": args.max_workers,
        },
        "execution_report": execution_report,
        "intelligence_refresh": intelligence_refresh,
        "snapshot_before": snapshot_before,
        "snapshot_after": snapshot_after,
        "comparison": {
            "vs_previous_regression": diff_vs_previous,
            "vs_current_run_start": diff_vs_before,
        },
    }
    saved_report = save_regression_report(report)
    report["report_path"] = str(saved_report.resolve())
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
