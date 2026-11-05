from __future__ import annotations

import json
import threading
from pathlib import Path

from core.file_lock import atomic_write_json, atomic_write_text, with_registry_lock


def test_registry_lock_is_reentrant_for_nested_operations():
    calls: list[str] = []

    @with_registry_lock
    def inner() -> str:
        calls.append("inner")
        return "ok"

    @with_registry_lock
    def outer() -> str:
        calls.append("outer-before")
        result = inner()
        calls.append("outer-after")
        return result

    assert outer() == "ok"
    assert calls == ["outer-before", "inner", "outer-after"]


def test_registry_lock_serializes_threads():
    value = {"count": 0}
    failures: list[BaseException] = []

    @with_registry_lock
    def increment() -> None:
        current = value["count"]
        value["count"] = current + 1

    def worker() -> None:
        try:
            for _ in range(50):
                increment()
        except BaseException as exc:  # surface worker exceptions in the test thread
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert value["count"] == 300


def test_atomic_writers_replace_complete_utf8_files(tmp_path: Path):
    text_path = tmp_path / "nested" / "artifact.md"
    json_path = tmp_path / "nested" / "report.json"

    atomic_write_text(text_path, "source-grounded evidence — complete\n")
    atomic_write_json(json_path, {"records": [{"id": "r1"}]})

    assert text_path.read_text(encoding="utf-8") == "source-grounded evidence — complete\n"
    assert json.loads(json_path.read_text(encoding="utf-8")) == {"records": [{"id": "r1"}]}
    assert not list(text_path.parent.glob("*.tmp"))
