from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.synthesis import build_synthesis_input_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preview a rich synthesis input bundle from intermediate artifacts.")
    parser.add_argument("input_files", nargs="+", help="Intermediate markdown files to include in the bundle.")
    parser.add_argument("--notes", default="", help="Optional notes to attach to the bundle.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    bundle = build_synthesis_input_bundle(
        input_files=[Path(item) for item in args.input_files],
        notes=args.notes or None,
    )
    print(json.dumps(bundle, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
