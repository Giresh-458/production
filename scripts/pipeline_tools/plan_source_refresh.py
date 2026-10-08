from __future__ import annotations

import argparse
import json

from core.source_registry import build_source_refresh_plan


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a source refresh plan from the shared source registry.")
    parser.add_argument("--agent", help="Optional agent name filter (e.g. funding, company, regulation).")
    parser.add_argument("--due-only", action="store_true", help="Only include sources that are due for refresh.")
    parser.add_argument("--force", action="store_true", help="Mark all selected sources as due in the plan.")
    args = parser.parse_args()

    result = build_source_refresh_plan(agent_filter=args.agent, due_only=args.due_only, force=args.force)
    print(
        json.dumps(
            {
                "total_entries": result.total_entries,
                "due_entries": result.due_entries,
                "skipped_entries": result.skipped_entries,
                "refresh_plan_path": result.refresh_plan_path,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
