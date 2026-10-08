from __future__ import annotations

import json
from pathlib import Path

from core.quality_metrics import generate_and_save_quality_metrics


def main() -> int:
    path = generate_and_save_quality_metrics(Path("outputs"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["path"] = str(path.resolve())
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
