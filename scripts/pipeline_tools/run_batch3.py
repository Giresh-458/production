"""Run Batch-3 (tagging -> clustering -> trends) on a completed Batch-2 run.

Usage:
    python scripts/pipeline_tools/run_batch3.py --batch2-run-dir outputs/batch2_runs/<run_id>

Only calls that Batch-2 marked ``ready_for_batch3`` are processed. Each call's
outputs are validated (core.batch3.validate_batch3_outputs). Exit code 0 means
every processed call is ``ready_for_batch4``; 2 means at least one is blocked or
no call was ready.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.batch3 import run_batch3_for_call  # noqa: E402
from core.file_lock import atomic_write_json  # noqa: E402
from run_pipeline import run_stage  # noqa: E402
from core.batch2 import validate_batch1_context  # noqa: E402
from core.funding_selection import FundingCallContext  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run and validate Batch-3 on a Batch-2 run directory.")
    parser.add_argument("--batch2-run-dir", required=True, help="Batch-2 run directory containing workflow/batch2_report.json")
    args = parser.parse_args(argv)

    run_dir = Path(args.batch2_run_dir).resolve()
    report_path = run_dir / "workflow" / "batch2_report.json"
    contexts_path = run_dir / "workflow" / "funding_contexts.json"
    try:
        batch2 = json.loads(report_path.read_text(encoding="utf-8"))
        # Batch-2 writes its own copy of the validated contexts (Batch-1 and
        # Batch-2 run IDs + contexts, no Batch-1 acceptance envelope). Require
        # matching run identity and re-validate every context before use.
        handoff = json.loads(contexts_path.read_text(encoding="utf-8"))
        if handoff.get("batch2_run_id") != batch2.get("run_id") or handoff.get("batch1_run_id") != batch2.get("batch1_run_id"):
            raise ValueError("Batch-2 funding_contexts.json run identity does not match batch2_report.json")
        contexts = {}
        for raw in handoff.get("funding_contexts") or []:
            ctx = FundingCallContext.from_dict(raw)
            problems = validate_batch1_context(ctx)
            if problems:
                raise ValueError(f"context {ctx.funding_call_id} failed revalidation: {problems}")
            contexts[ctx.funding_call_id] = ctx
    except Exception as exc:
        print(json.dumps({"status": "blocked_missing_batch2", "error": str(exc)}, indent=2))
        return 2

    report = {
        "status": "incomplete_no_ready_calls",
        "batch2_run_id": batch2.get("run_id"),
        "batch1_run_id": batch2.get("batch1_run_id"),
        "generated_at": datetime.now(UTC).isoformat(),
        "funding_calls": [],
    }
    for call in batch2.get("funding_calls") or []:
        call_id = call.get("funding_call_id")
        if call.get("status") != "ready_for_batch3":
            report["funding_calls"].append({"funding_call_id": call_id, "status": "skipped_not_ready", "batch2_status": call.get("status")})
            continue
        context = contexts.get(call_id)
        if context is None:
            report["funding_calls"].append({"funding_call_id": call_id, "status": "blocked_missing_context"})
            continue
        # Resolve the call inside the run directory that was passed in. The
        # recorded absolute output_root points at wherever Batch-2 originally ran;
        # for a copied/moved run it would process (or fail on) the old location
        # (observed while re-processing a copied run).
        call_root = run_dir / "calls" / str(call_id)
        if not call_root.is_dir():
            call_root = Path(call["output_root"])
        report["funding_calls"].append(run_batch3_for_call(call_root, context, run_stage))

    processed = [c for c in report["funding_calls"] if c.get("status") != "skipped_not_ready"]
    if processed and all(c.get("status") == "ready_for_batch4" for c in processed):
        report["status"] = "ready_for_batch4"
    elif processed:
        report["status"] = "blocked"
    atomic_write_json(run_dir / "workflow" / "batch3_report.json", report)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0 if report["status"] == "ready_for_batch4" else 2


if __name__ == "__main__":
    raise SystemExit(main())
