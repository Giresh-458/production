from __future__ import annotations

import json
from pathlib import Path

from core.normalization import save_normalized_collection_records


def main() -> int:
    outputs = save_normalized_collection_records(Path("outputs"))
    print(json.dumps({key: str(path.resolve()) for key, path in outputs.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
