from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.batch_runner import FULL_COLLECTION_REPORT_PATH, execute_full_collection_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="One-command full collection runner for smoke testing, regression testing, or refresh runs."
    )
    parser.add_argument("--profile", choices=["smoke", "regression", "refresh"], default="smoke")
    parser.add_argument("--execution-mode", choices=["sequential", "parallel"], default="sequential")
    parser.add_argument("--dry-run", action="store_true", help="Plan the full run without executing agents.")
    parser.add_argument("--timeout-budget", type=int, default=None, help="Overall timeout budget in seconds.")
    parser.add_argument("--max-workers", type=int, default=None, help="Maximum worker threads for parallel mode.")
    parser.add_argument("--analysis-mode", choices=["collect_only", "analyze"], default="collect_only")
    parser.add_argument(
        "--report-path",
        default=str(FULL_COLLECTION_REPORT_PATH),
        help="Where to save the full-collection report JSON.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = execute_full_collection_run(
        profile=args.profile,
        execution_mode=args.execution_mode,
        dry_run=args.dry_run,
        timeout_budget_seconds=args.timeout_budget,
        max_workers=args.max_workers,
        analysis_mode=args.analysis_mode,
        report_path=Path(args.report_path),
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
