from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
from typing import Any

from core.http_client import get_response


# RFC 9309 recommends not retaining a successful robots.txt response for more
# than 24 hours. Transient failures are cached briefly to avoid retry storms.
_SUCCESS_TTL_SECONDS = 24 * 60 * 60
_FAILURE_TTL_SECONDS = 60
_UNAVAILABLE_TTL_SECONDS = 60 * 60
_MAX_ROBOTS_BYTES = 500 * 1024

# Cache entries: (expires_at, parser, unavailable_allows, failure_reason).
_CACHE: dict[str, tuple[float, RobotFileParser | None, bool, str | None]] = {}
_CACHE_LOCK = threading.RLock()


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError("robots policy requires a credential-free absolute HTTP(S) URL")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _retry_after_ttl(headers: Any) -> int:
    """Keep a robots 429 cached until its Retry-After time, within 24 hours."""
    raw = str((headers or {}).get("Retry-After", "")).strip()
    delay = 0.0
    if raw.isdigit():
        delay = float(raw)
    elif raw:
        try:
            retry_at = parsedate_to_datetime(raw)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            delay = (retry_at - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            delay = 0.0
    return int(min(_SUCCESS_TTL_SECONDS, max(_FAILURE_TTL_SECONDS, delay)))


def _cached_policy(
    origin: str,
    cache: dict[str, tuple[float, RobotFileParser | None, bool, str | None]] | None,
) -> tuple[float, RobotFileParser | None, bool, str | None] | None:
    store = cache if cache is not None else _CACHE
    lock = _CACHE_LOCK if cache is None else None
    if lock:
        with lock:
            entry = store.get(origin)
            if entry and entry[0] > time.monotonic():
                return entry
            store.pop(origin, None)
            return None
    entry = store.get(origin)
    if entry and entry[0] > time.monotonic():
        return entry
    store.pop(origin, None)
    return None


def _store_policy(
    origin: str,
    entry: tuple[float, RobotFileParser | None, bool, str | None],
    cache: dict[str, tuple[float, RobotFileParser | None, bool, str | None]] | None,
) -> None:
    store = cache if cache is not None else _CACHE
    if cache is None:
        with _CACHE_LOCK:
            store[origin] = entry
    else:
        store[origin] = entry


def check_robots(
    url: str,
    *,
    user_agent: str = "RIF/3.1",
    timeout: float = 5.0,
    cache: dict[str, tuple[float, RobotFileParser | None, bool, str | None]] | None = None,
) -> tuple[bool, str | None, float]:
    """Return (allowed, reason, crawl_delay) using RFC 9309 failure semantics.

    A successful robots.txt response is parsed and enforced. HTTP 4xx means
    robots.txt is unavailable and may be treated as allowing access, except
    429, which is a rate-limit signal and is deliberately fail-closed. HTTP
    5xx, network errors, malformed responses and parsing failures fail closed.
    """
    try:
        origin = _origin(url)
    except ValueError as exc:
        return False, str(exc), 0.0

    entry = _cached_policy(origin, cache)
    if entry is None:
        robots_url = f"{origin}/robots.txt"
        try:
            response = get_response(
                robots_url,
                headers={"User-Agent": user_agent},
                timeout=(max(0.1, timeout), max(0.1, timeout)),
                retries=0,
            )
            status = int(response.status_code)
            if 200 <= status < 300:
                raw = response.content[:_MAX_ROBOTS_BYTES]
                text = raw.decode("utf-8-sig", errors="replace")
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(text.splitlines())
                entry = (
                    time.monotonic() + _SUCCESS_TTL_SECONDS,
                    parser,
                    False,
                    None,
                )
            elif 400 <= status < 500 and status != 429:
                # RFC 9309 §2.3.1.3: an unavailable robots.txt permits access.
                entry = (
                    time.monotonic() + _UNAVAILABLE_TTL_SECONDS,
                    None,
                    True,
                    None,
                )
            else:
                headers = getattr(response, "headers", {}) or {}
                retry_after = headers.get("Retry-After") if status == 429 else None
                reason = f"robots.txt returned HTTP {status}"
                if retry_after:
                    reason += f"; Retry-After={retry_after}"
                ttl = _retry_after_ttl(headers) if status == 429 else _FAILURE_TTL_SECONDS
                entry = (
                    time.monotonic() + ttl,
                    None,
                    False,
                    reason,
                )
        except Exception as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if isinstance(status, int) and 400 <= status < 500 and status != 429:
                entry = (
                    time.monotonic() + _UNAVAILABLE_TTL_SECONDS,
                    None,
                    True,
                    None,
                )
            else:
                headers = getattr(response, "headers", {}) or {}
                retry_after = headers.get("Retry-After") if status == 429 else None
                reason = (
                    f"robots.txt returned HTTP {status}"
                    if status is not None
                    else f"robots.txt could not be fetched: {type(exc).__name__}"
                )
                if retry_after:
                    reason += f"; Retry-After={retry_after}"
                ttl = _retry_after_ttl(headers) if status == 429 else _FAILURE_TTL_SECONDS
                entry = (
                    time.monotonic() + ttl,
                    None,
                    False,
                    reason,
                )
        _store_policy(origin, entry, cache)

    _, parser, unavailable_allows, failure_reason = entry
    if parser is None:
        if unavailable_allows:
            return True, None, 0.0
        return False, failure_reason or "robots.txt policy unavailable", 0.0

    try:
        allowed = parser.can_fetch(user_agent, url)
        delay = parser.crawl_delay(user_agent) or 0
        return allowed, None if allowed else "robots.txt disallows this URL", float(delay)
    except Exception as exc:
        return False, f"robots.txt policy could not be evaluated: {type(exc).__name__}", 0.0


def clear_robots_cache() -> None:
    """Clear the process-wide robots cache; intended for deterministic tests."""
    with _CACHE_LOCK:
        _CACHE.clear()
