from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass

from core.source_registry import (
    add_candidate_source,
    build_source_review_queue,
    deprecate_source,
    export_source_health_summary,
    load_source_registry,
    mark_stale_sources,
    pause_source,
    promote_candidate_source,
    prune_sources,
    rescore_source,
    rescore_sources_from_recent_evidence,
    review_source_candidate,
    set_source_status,
)


def _print(value: object) -> None:
    payload = asdict(value) if is_dataclass(value) else value
    print(json.dumps(payload, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage source lifecycle in the shared source registry.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add-candidate", help="Add a new candidate source to the registry.")
    add_parser.add_argument("--agent", required=True)
    add_parser.add_argument("--name", required=True)
    add_parser.add_argument("--url", required=True)
    add_parser.add_argument("--source-type", default="candidate_source")
    add_parser.add_argument("--area", action="append", default=[])
    add_parser.add_argument("--notes", default="")
    add_parser.add_argument("--discovery-method", default="manual_candidate")
    add_parser.add_argument("--fetch-frequency")

    promote_parser = subparsers.add_parser("promote", help="Promote a candidate source to approved or active.")
    promote_parser.add_argument("--source-id", required=True)
    promote_parser.add_argument("--activate", action="store_true")
    promote_parser.add_argument("--notes")

    pause_parser = subparsers.add_parser("pause", help="Pause a noisy or temporarily unsuitable source.")
    pause_parser.add_argument("--source-id", required=True)
    pause_parser.add_argument("--notes")

    deprecate_parser = subparsers.add_parser("deprecate", help="Deprecate a dead or obsolete source.")
    deprecate_parser.add_argument("--source-id", required=True)
    deprecate_parser.add_argument("--notes")

    set_parser = subparsers.add_parser("set-status", help="Set a source status using lifecycle transition rules.")
    set_parser.add_argument("--source-id", required=True)
    set_parser.add_argument("--status", required=True)
    set_parser.add_argument("--notes")

    stale_parser = subparsers.add_parser("mark-stale", help="Mark overdue approved/active sources as stale.")
    stale_parser.add_argument("--agent")
    stale_parser.add_argument("--multiplier", type=float, default=2.0)

    queue_parser = subparsers.add_parser("review-queue", help="Build or inspect the source review queue.")
    queue_parser.add_argument("--agent")

    review_parser = subparsers.add_parser("review", help="Review a queued candidate source.")
    review_parser.add_argument("--review-id", required=True)
    review_parser.add_argument("--action", required=True, choices=["approve", "reject", "pause", "rescore"])
    review_parser.add_argument("--rationale", required=True)
    review_parser.add_argument("--activate", action="store_true")
    review_parser.add_argument("--trust-score", type=float)
    review_parser.add_argument("--relevance-score", type=float)

    rescore_parser = subparsers.add_parser("rescore", help="Update trust and relevance scores for a registry source.")
    rescore_parser.add_argument("--source-id", required=True)
    rescore_parser.add_argument("--trust-score", type=float)
    rescore_parser.add_argument("--relevance-score", type=float)
    rescore_parser.add_argument("--notes", default="")

    prune_parser = subparsers.add_parser("prune", help="Prune deprecated or stale sources from the registry.")
    prune_parser.add_argument("--agent")
    prune_parser.add_argument("--status", action="append", default=["deprecated"])
    prune_parser.add_argument("--older-than-days", type=int, default=30)
    prune_parser.add_argument("--apply", action="store_true")

    rescore_recent_parser = subparsers.add_parser("rescore-recent", help="Rescore sources using recent normalized evidence.")
    rescore_recent_parser.add_argument("--agent")
    rescore_recent_parser.add_argument("--lookback-days", type=int, default=120)

    health_parser = subparsers.add_parser("health-summary", help="Export a source health summary.")
    health_parser.add_argument("--agent")

    list_parser = subparsers.add_parser("list", help="List registry entries with optional filters.")
    list_parser.add_argument("--agent")
    list_parser.add_argument("--status")

    args = parser.parse_args()

    if args.command == "add-candidate":
        result = add_candidate_source(
            agent_name=args.agent,
            name=args.name,
            url=args.url,
            source_type=args.source_type,
            area_hints=args.area,
            notes=args.notes,
            discovery_method=args.discovery_method,
            fetch_frequency=args.fetch_frequency,
        )
        _print(result)
        return 0

    if args.command == "promote":
        result = promote_candidate_source(source_id=args.source_id, activate=args.activate, notes=args.notes)
        _print(result)
        return 0

    if args.command == "pause":
        result = pause_source(source_id=args.source_id, notes=args.notes)
        _print(result)
        return 0

    if args.command == "deprecate":
        result = deprecate_source(source_id=args.source_id, notes=args.notes)
        _print(result)
        return 0

    if args.command == "set-status":
        result = set_source_status(source_id=args.source_id, new_status=args.status, notes=args.notes)
        _print(result)
        return 0

    if args.command == "mark-stale":
        updated = mark_stale_sources(agent_filter=args.agent, staleness_multiplier=args.multiplier)
        _print({"updated_count": updated})
        return 0

    if args.command == "review-queue":
        result = build_source_review_queue(agent_filter=args.agent)
        _print(result)
        return 0

    if args.command == "review":
        result = review_source_candidate(
            review_id=args.review_id,
            action=args.action,
            rationale=args.rationale,
            activate=args.activate,
            trust_score=args.trust_score,
            relevance_score=args.relevance_score,
        )
        _print(result)
        return 0

    if args.command == "rescore":
        result = rescore_source(
            source_id=args.source_id,
            trust_score=args.trust_score,
            relevance_score=args.relevance_score,
            notes=args.notes,
        )
        _print(result)
        return 0

    if args.command == "prune":
        result = prune_sources(
            agent_filter=args.agent,
            statuses=args.status,
            older_than_days=args.older_than_days,
            dry_run=not args.apply,
        )
        _print(result)
        return 0

    if args.command == "rescore-recent":
        result = rescore_sources_from_recent_evidence(
            agent_filter=args.agent,
            lookback_days=args.lookback_days,
        )
        _print(result)
        return 0

    if args.command == "health-summary":
        result = export_source_health_summary(agent_filter=args.agent)
        _print(result)
        return 0

    if args.command == "list":
        entries = load_source_registry()
        if args.agent:
            entries = [entry for entry in entries if entry.get("agent_name") == args.agent]
        if args.status:
            entries = [entry for entry in entries if str(entry.get("status", "")).strip().lower() == args.status.lower()]
        _print({"count": len(entries), "entries": entries})
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
