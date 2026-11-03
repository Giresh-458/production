from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from core.batch2 import execute_batch2
from core.funding_selection import FundingCallContext


def load_funding_context_handoff(path: Path) -> tuple[list[FundingCallContext], str | None]:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read funding-context JSON from {path}: {exc}") from exc

    batch1_run_id = None
    context_validation = None
    if isinstance(payload, dict):
        batch1_run_id = payload.get("batch1_run_id") or payload.get("run_id")
        acceptance = payload.get("batch1_acceptance")
        nested_acceptance = acceptance.get("acceptance") if isinstance(acceptance, dict) else None
        global_acceptance_valid = (
            isinstance(acceptance, dict)
            and acceptance.get("accepted") is True
            and str(acceptance.get("status") or "").upper() == "ACCEPTED"
            and acceptance.get("source_coverage_verified") is True
            and str(acceptance.get("batch1_run_id") or "") == str(batch1_run_id or "")
            and isinstance(nested_acceptance, dict)
            and nested_acceptance.get("accepted") is True
            and str(nested_acceptance.get("status") or "").upper() == "ACCEPTED"
            and nested_acceptance.get("blockers") == []
            and nested_acceptance.get("blocker_count") == 0
        )
        candidate_validation = payload.get("context_validation")
        if not global_acceptance_valid and isinstance(candidate_validation, dict):
            if (
                str(candidate_validation.get("status") or "").upper() == "VALIDATED"
                and candidate_validation.get("scope") == "selected_contexts_only"
                and str(candidate_validation.get("batch1_run_id") or "") == str(batch1_run_id or "")
                and isinstance(candidate_validation.get("validated_funding_call_ids"), list)
            ):
                context_validation = candidate_validation
        if not global_acceptance_valid and context_validation is None:
            raise ValueError(
                "Batch-2 requires either an ACCEPTED global Batch-1 source-coverage "
                "attestation or a selected-context validation for the same batch1_run_id."
            )
    else:
        raise ValueError(
            "Batch-2 requires an auditable Batch-1 handoff envelope; a bare context list is not accepted."
        )

    if isinstance(payload, list):
        raw_contexts = payload
    elif isinstance(payload, dict) and isinstance(payload.get("funding_contexts"), list):
        raw_contexts = payload["funding_contexts"]
    elif isinstance(payload, dict) and isinstance(payload.get("contexts"), list):
        raw_contexts = payload["contexts"]
    elif isinstance(payload, dict) and isinstance(payload.get("funding_context"), dict):
        raw_contexts = [payload["funding_context"]]
    elif isinstance(payload, dict) and payload.get("funding_call_id"):
        raw_contexts = [payload]
    else:
        raise ValueError(
            "Expected a FundingCallContext object, a list of contexts, or an object "
            "containing 'funding_contexts'. Use the Batch-1 workflow/funding_contexts.json output."
        )

    if not raw_contexts or not all(isinstance(item, dict) for item in raw_contexts):
        raise ValueError("Funding-context input must contain one or more JSON objects.")

    try:
        contexts = [FundingCallContext.from_dict(item) for item in raw_contexts]
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(
            "Funding-context JSON does not match the validated FundingCallContext schema. "
            f"Details: {exc}"
        ) from exc
    if context_validation is not None:
        actual_ids = sorted(context.funding_call_id for context in contexts)
        validated_ids = sorted(str(value).strip() for value in context_validation["validated_funding_call_ids"])
        if actual_ids != validated_ids or len(set(actual_ids)) != len(actual_ids):
            raise ValueError(
                "Selected-context validation IDs do not exactly match the FundingCallContext objects."
            )
    return contexts, str(batch1_run_id) if batch1_run_id else None


def load_funding_contexts(path: Path) -> list[FundingCallContext]:
    """Compatibility helper returning only the parsed funding contexts."""
    return load_funding_context_handoff(path)[0]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run RIF Batch-2 collection and normalization, stopping before Batch-3."
    )
    parser.add_argument(
        "--funding-context-file",
        required=True,
        help="JSON exported by Batch-1 at workflow/funding_contexts.json.",
    )
    parser.add_argument("--output-root", default="outputs/batch2_runs")
    parser.add_argument("--run-id", default=None, help="Optional unique run ID.")
    parser.add_argument(
        "--profile",
        choices=["smoke", "regression", "refresh"],
        default="refresh",
    )
    parser.add_argument(
        "--execution-mode",
        choices=["parallel", "sequential"],
        default="parallel",
    )
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--dry-run", action="store_true", help="Plan collection tasks without fetching or normalizing evidence.")
    parser.add_argument(
        "--scheduled-refresh",
        action="store_true",
        help="Use source refresh schedules instead of forcing a live refresh.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO)

    profiles = {
        "smoke": {"default_limit": 1, "timeout_budget_seconds": 600},
        "regression": {"default_limit": 2, "timeout_budget_seconds": 1800},
        "refresh": {"default_limit": None, "timeout_budget_seconds": 3600},
    }
    profile = profiles[args.profile]

    try:
        handoff_path = Path(args.funding_context_file)
        contexts, batch1_run_id = load_funding_context_handoff(handoff_path)
        if not batch1_run_id:
            raise ValueError("Accepted Batch-1 handoff is missing batch1_run_id.")
        handoff_payload = json.loads(handoff_path.read_text(encoding="utf-8"))
        batch1_acceptance = handoff_payload.get("batch1_acceptance")
        context_validation = handoff_payload.get("context_validation")
        report = execute_batch2(
            funding_contexts=contexts,
            output_root=Path(args.output_root),
            run_id=args.run_id,
            batch1_run_id=batch1_run_id,
            batch1_acceptance=batch1_acceptance,
            context_validation=context_validation,
            execution_mode=args.execution_mode,
            max_workers=args.max_workers,
            timeout_budget_seconds=profile["timeout_budget_seconds"],
            default_limit=profile["default_limit"],
            force_refresh=not args.scheduled_refresh,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        logging.exception("Batch-2 execution failed")
        print(f"Batch-2 failed: {exc}")
        return 1

    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report.get("status") == "dry_run":
        return 0
    return 0 if report.get("status") == "ready_for_batch3" else 2


if __name__ == "__main__":
    raise SystemExit(main())
