from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.normalization import build_normalized_collection_records
from core.source_registry import (
    SOURCE_DISCOVERY_PATH,
    register_discovered_candidates,
    save_source_discovery_candidates,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover candidate sources from normalized intermediate artifacts.")
    parser.add_argument("--outputs-root", default="outputs", help="Outputs root containing intermediate/ artifacts.")
    parser.add_argument("--agent", default=None, help="Optional agent filter.")
    parser.add_argument("--min-support", type=int, default=1, help="Minimum supporting mentions before suggesting a candidate.")
    parser.add_argument(
        "--register-candidates",
        action="store_true",
        help="Register newly discovered candidates into the source registry as candidate sources.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Optional limit when registering discovered candidates.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    records = build_normalized_collection_records(Path(args.outputs_root))
    result = save_source_discovery_candidates(
        records,
        agent_filter=args.agent,
        min_support=args.min_support,
    )
    payload: dict[str, object] = {
        "discovery_path": result.discovery_path,
        "total_candidates": result.total_candidates,
        "new_candidates": result.new_candidates,
        "existing_candidates": result.existing_candidates,
    }

    if args.register_candidates:
        registered = register_discovered_candidates(
            discovery_path=SOURCE_DISCOVERY_PATH,
            agent_filter=args.agent,
            limit=args.limit,
        )
        payload["registered_candidates"] = [
            {
                "source_id": item.source_id,
                "old_status": item.old_status,
                "new_status": item.new_status,
            }
            for item in registered
        ]

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
