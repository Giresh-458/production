from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable

_JSON_FILE_LOCK = threading.RLock()
# Keep this file permanently: deleting a lock file while another process has it
# open can split contenders across different inodes/file objects.
_GLOBAL_LOCK_FILE = Path(tempfile.gettempdir()) / "rif_global_registry.lockfile"
_LOCK_STATE = threading.local()
_LOCK_TIMEOUT_SECONDS = 60.0
_LOCK_RETRY_SECONDS = 0.1


def _acquire_process_lock(timeout_seconds: float = _LOCK_TIMEOUT_SECONDS) -> int:
    """Acquire an OS-managed cross-process lock, released automatically on crash."""
    _GLOBAL_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR
    fd = os.open(str(_GLOBAL_LOCK_FILE), flags, 0o600)
    deadline = time.monotonic() + timeout_seconds

    try:
        if os.name == "nt":
            import msvcrt

            # msvcrt.locking locks a byte range; ensure byte zero exists.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            while True:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return fd
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Could not acquire global registry lock within 60 seconds") from exc
                    time.sleep(_LOCK_RETRY_SECONDS)

        import fcntl

        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Could not acquire global registry lock within 60 seconds") from exc
                time.sleep(_LOCK_RETRY_SECONDS)
    except Exception:
        os.close(fd)
        raise


def _release_process_lock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def with_registry_lock(func: Callable[..., Any]) -> Callable[..., Any]:
    """Serialize registry operations across threads and processes.

    The thread-local depth makes nested decorated registry operations re-entrant.
    The OS lock is released by the kernel when a process exits or crashes, so a
    stale directory can never permanently strand future registry updates.
    """
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _JSON_FILE_LOCK:
            depth = int(getattr(_LOCK_STATE, "depth", 0) or 0)
            if depth:
                _LOCK_STATE.depth = depth + 1
                try:
                    return func(*args, **kwargs)
                finally:
                    _LOCK_STATE.depth = depth

            fd = _acquire_process_lock()
            _LOCK_STATE.depth = 1
            try:
                return func(*args, **kwargs)
            finally:
                _LOCK_STATE.depth = 0
                _release_process_lock(fd)

    return wrapper


def atomic_write_text(path: Path, content: str) -> None:
    """Write UTF-8 text atomically so interrupted writes cannot truncate an artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically using a temporary file and os.replace."""
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))
