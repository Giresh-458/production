from __future__ import annotations

import argparse
import json

from core.source_registry import sync_source_registry


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync configured YAML sources into the shared source registry.")
    parser.add_argument("--agent", help="Optional agent name filter (e.g. funding, company, regulation).")
    args = parser.parse_args()

    result = sync_source_registry(agent_filter=args.agent)
    print(
        json.dumps(
            {
                "total_entries": result.total_entries,
                "active_entries": result.active_entries,
                "deprecated_entries": result.deprecated_entries,
                "registry_path": result.registry_path,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
