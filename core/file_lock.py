import json
import os
import tempfile
import threading
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import time

_JSON_FILE_LOCK = threading.RLock()
_GLOBAL_LOCK_DIR = Path(tempfile.gettempdir()) / "rif_global_registry.lock"

def with_registry_lock(func):
    """Decorator to wrap a function with a global process-safe lock."""
    @wraps(func)
    def wrapper(*args, **kwargs):
        # 1. Thread lock (re-entrant for the same thread)
        with _JSON_FILE_LOCK:
            # 2. Process lock (using atomic mkdir)
            acquired = False
            for _ in range(600):  # Wait up to 60s
                try:
                    _GLOBAL_LOCK_DIR.mkdir(parents=True, exist_ok=False)
                    acquired = True
                    break
                except FileExistsError:
                    time.sleep(0.1)
            
            if not acquired:
                raise TimeoutError("Could not acquire global registry lock")
            
            try:
                return func(*args, **kwargs)
            finally:
                try:
                    _GLOBAL_LOCK_DIR.rmdir()
                except Exception:
                    pass
    return wrapper

def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Writes a JSON file atomically using a temporary file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
