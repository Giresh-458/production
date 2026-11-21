"""Disk persistence for the "Add source" page.

Streamlit keeps session state in memory only, so a browser reload lost an
in-progress scan, the user's ticks and the review. The page saves its state here
after every interaction and restores it on reload until the user finishes or
cancels. A separate list remembers discarded URLs (pages that fit no agent and
pages the user discarded) so later scans neither fetch nor show them again.

Files live under outputs/workflow/ (git-ignored); RIF_ADD_SOURCE_STATE_DIR overrides.
"""
from __future__ import annotations

import dataclasses
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from core.agent_fit import AgentFit, PageCandidate, PageFinding
from core.user_sources import UrlInspection

_TYPES = {cls.__name__: cls for cls in (AgentFit, PageCandidate, PageFinding, UrlInspection)}


def _dir() -> Path:
    return Path(os.environ.get("RIF_ADD_SOURCE_STATE_DIR", "outputs/workflow"))


def _state_file() -> Path:
    return _dir() / "add_source_state.json"


def _discard_file() -> Path:
    return _dir() / "discarded_source_urls.json"


def encode(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {"__type__": type(value).__name__,
                **{f.name: encode(getattr(value, f.name)) for f in dataclasses.fields(value)}}
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [encode(v) for v in value]
    return value


def decode(value: Any) -> Any:
    if isinstance(value, dict):
        kind = value.get("__type__")
        fields = {k: decode(v) for k, v in value.items() if k != "__type__"}
        if kind in _TYPES:
            cls = _TYPES[kind]
            known = {f.name for f in dataclasses.fields(cls)}
            item = cls(**{k: v for k, v in fields.items() if k in known})
            if isinstance(item, UrlInspection):
                item.domain_evidence = [tuple(x) for x in item.domain_evidence]
            return item
        return fields
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def save_state(values: dict[str, Any]) -> None:
    _write(_state_file(), encode(values))


def load_state() -> dict[str, Any]:
    try:
        return decode(json.loads(_state_file().read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return {}


def clear_state() -> None:
    try:
        _state_file().unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- discarded URLs
# Query parameters that change on every visit (observed: colosseum.com "...&seed=<random>")
# would otherwise make the same discarded page look new each time.
_VOLATILE_PARAMS = re.compile(r"^(?:seed|session|sessionid|sid|token|ts|timestamp|cb|cache|_|utm_\w+|gclid|fbclid)$", re.I)


def _key(url: str) -> str:
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower().removeprefix("www.")
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _VOLATILE_PARAMS.match(k)]
    query = f"?{urlencode(sorted(params))}" if params else ""
    return f"{host}{(parts.path or '/').rstrip('/')}{query}".lower()


def load_discarded() -> dict[str, dict[str, str]]:
    try:
        data = json.loads(_discard_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_discarded(url: str, discarded: dict[str, dict[str, str]] | None = None) -> bool:
    return _key(url) in (discarded if discarded is not None else load_discarded())


def discard(urls: list[str], reason: str) -> None:
    data = load_discarded()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    for url in urls:
        data[_key(url)] = {"url": url, "reason": reason, "at": now}
    _write(_discard_file(), data)


def discarded_for_site(url: str) -> list[dict[str, str]]:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    return [item for key, item in load_discarded().items() if key.split("/", 1)[0] == host]


def restore_site(url: str) -> int:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    data = load_discarded()
    keep = {k: v for k, v in data.items() if k.split("/", 1)[0] != host}
    _write(_discard_file(), keep)
    return len(data) - len(keep)
