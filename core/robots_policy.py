from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit

from core.http_client import get_response


# RFC 9309 recommends not retaining a successful robots.txt response for more
# than 24 hours. Transient failures are cached briefly to avoid retry storms.
_SUCCESS_TTL_SECONDS = 24 * 60 * 60
_FAILURE_TTL_SECONDS = 60
_UNAVAILABLE_TTL_SECONDS = 60 * 60
_MAX_ROBOTS_BYTES = 500 * 1024
_MAX_RATE_LIMIT_COOLDOWN_SECONDS = 365 * 24 * 60 * 60
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)

# Cache entries: (expires_at, parsed_policy, unavailable_allows, failure_reason).
_CACHE: dict[str, tuple[float, "RobotsPolicy | None", bool, str | None]] = {}
_CACHE_LOCK = threading.RLock()
_HOST_NEXT_ALLOWED: dict[str, float] = {}
_HOST_RATE_LOCK = threading.Lock()


@dataclass(frozen=True)
class _Rule:
    pattern: str
    allowed: bool
    regex: re.Pattern[str]
    specificity: int


@dataclass(frozen=True)
class _Group:
    user_agents: tuple[str, ...]
    rules: tuple[_Rule, ...]
    crawl_delay: float


@dataclass(frozen=True)
class RobotsPolicy:
    groups: tuple[_Group, ...]
    sitemaps: tuple[str, ...] = ()

    def decision(self, user_agent: str, url: str) -> tuple[bool, float]:
        agent = str(user_agent or "*").lower()
        matching = [
            group
            for group in self.groups
            if any(
                token != "*" and token and token.lower() in agent
                for token in group.user_agents
            )
        ]
        # RFC 9309 requires all groups matching the crawler product token to
        # be combined. The wildcard group is used only when none match.
        selected = matching or [
            group for group in self.groups if "*" in group.user_agents
        ]

        if not selected:
            return True, 0.0

        target = _normalize_target(url)
        matching_rules = [
            rule
            for group in selected
            for rule in group.rules
            if rule.regex.match(target)
        ]
        crawl_delay = max((group.crawl_delay for group in selected), default=0.0)
        if not matching_rules:
            return True, crawl_delay

        best_specificity = max(rule.specificity for rule in matching_rules)
        best_rules = [
            rule for rule in matching_rules if rule.specificity == best_specificity
        ]
        # RFC 9309: the most specific matching rule wins; when equally specific
        # Allow and Disallow rules conflict, prefer Allow.
        return any(rule.allowed for rule in best_rules), crawl_delay


def _normalize_percent_encoding(value: str) -> str:
    # RFC 9309 requires non-ASCII octets to be UTF-8 percent-encoded before
    # comparison. Keep reserved octets encoded, but decode unreserved octets.
    encoded = quote(value, safe="/%:@!$&'()*+,;=-._~*")

    def replace(match: re.Match[str]) -> str:
        byte = int(match.group(1), 16)
        char = chr(byte)
        return char if char in _UNRESERVED else f"%{byte:02X}"

    return re.sub(r"%([0-9A-Fa-f]{2})", replace, encoded)


def _normalize_target(url: str) -> str:
    parsed = urlsplit(url)
    # Matching applies to the URI path component, not its query or fragment.
    return _normalize_percent_encoding(parsed.path or "/")


def _compile_rule(pattern: str, allowed: bool) -> _Rule | None:
    value = _normalize_percent_encoding(pattern.strip())
    if not value:
        # Empty Disallow has no effect; empty Allow allows everything.
        return None
    anchored = value.endswith("$")
    if anchored:
        value = value[:-1]
    pieces = ["^"]
    for char in value:
        pieces.append(".*" if char == "*" else re.escape(char))
    if anchored:
        pieces.append("$")
    try:
        compiled = re.compile("".join(pieces))
    except re.error:
        return None
    return _Rule(
        pattern=value,
        allowed=allowed,
        regex=compiled,
        specificity=len(value.encode("utf-8")),
    )


def _parse_robots(text: str) -> RobotsPolicy:
    groups: list[_Group] = []
    agents: list[str] = []
    rules: list[_Rule] = []
    crawl_delay = 0.0
    sitemaps: list[str] = []

    def finish_group() -> None:
        nonlocal agents, rules, crawl_delay
        if agents:
            groups.append(
                _Group(
                    user_agents=tuple(agents),
                    rules=tuple(rules),
                    crawl_delay=crawl_delay,
                )
            )
        agents = []
        rules = []
        crawl_delay = 0.0

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            if agents:
                finish_group()
            continue
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        directive = name.strip().lower()
        value = value.strip()
        if directive == "user-agent":
            if rules:
                finish_group()
            if value:
                agents.append(value)
        elif directive in {"allow", "disallow"}:
            if agents:
                rule = _compile_rule(value, allowed=(directive == "allow"))
                if rule is not None:
                    rules.append(rule)
        elif directive == "sitemap" and value:
            sitemap_url = value.strip()
            parsed_sitemap = urlsplit(sitemap_url)
            if (
                parsed_sitemap.scheme in {"http", "https"}
                and parsed_sitemap.netloc
                and not parsed_sitemap.username
                and not parsed_sitemap.password
            ):
                sitemaps.append(sitemap_url)
        elif directive == "crawl-delay" and agents:
            try:
                parsed_delay = float(value)
                if parsed_delay >= 0:
                    crawl_delay = max(crawl_delay, parsed_delay)
            except (TypeError, ValueError, OverflowError):
                pass

    finish_group()
    return RobotsPolicy(tuple(groups), tuple(dict.fromkeys(sitemaps)))


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


def defer_host(url: str, delay_seconds: float) -> None:
    """Apply an origin-wide cooldown after an HTTP 429 response."""
    try:
        origin = _origin(url)
    except ValueError:
        return
    delay = max(0.0, min(float(delay_seconds), float(_MAX_RATE_LIMIT_COOLDOWN_SECONDS)))
    with _HOST_RATE_LOCK:
        _HOST_NEXT_ALLOWED[origin] = max(
            _HOST_NEXT_ALLOWED.get(origin, 0.0),
            time.monotonic() + delay,
        )


def wait_for_host_slot(
    url: str,
    *,
    crawl_delay: float = 0.0,
    default_delay: float = 1.0,
    deadline: float | None = None,
) -> bool:
    """Serialize requests per origin and honor robots Crawl-delay and 429 cooldowns.

    A worker never sleeps more than five minutes for a single host slot. Longer
    server cooldowns are deferred and reported as rate-limited, not bypassed.
    Deadline values use the CrawlContext wall-clock epoch convention.
    """
    try:
        origin = _origin(url)
    except ValueError:
        return False

    from core.crawl_context import current_crawl_context
    crawl_ctx = current_crawl_context.get(None)
    if deadline is None and crawl_ctx:
        deadline = crawl_ctx.deadline

    interval = max(float(default_delay), max(0.0, float(crawl_delay or 0.0)))
    with _HOST_RATE_LOCK:
        now_mono = time.monotonic()
        next_allowed = max(now_mono, _HOST_NEXT_ALLOWED.get(origin, now_mono))
        wait_seconds = next_allowed - now_mono
        if deadline is not None and time.time() + wait_seconds >= deadline:
            if crawl_ctx:
                with crawl_ctx.lock:
                    crawl_ctx.stop_reasons.add("deadline")
            return False
        if wait_seconds > 300.0:
            if crawl_ctx:
                with crawl_ctx.lock:
                    crawl_ctx.stop_reasons.add("rate_limited")
            return False
        _HOST_NEXT_ALLOWED[origin] = next_allowed + interval

    if wait_seconds > 0:
        time.sleep(wait_seconds)
    return deadline is None or time.time() < deadline


def _retry_after_ttl(headers) -> int:
    """Keep a robots 429 cached until Retry-After, capped at one year."""
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
    return int(min(_MAX_RATE_LIMIT_COOLDOWN_SECONDS, max(_FAILURE_TTL_SECONDS, delay)))


def _cached_policy(origin: str, cache=None):
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


def _store_policy(origin: str, entry, cache=None) -> None:
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
    cache: dict[str, tuple[float, RobotsPolicy | None, bool, str | None]] | None = None,
) -> tuple[bool, str | None, float]:
    """Return (allowed, reason, crawl_delay) under RFC 9309 failure semantics.

    Successful rules are enforced with longest-match specificity and Allow
    winning equal-specificity conflicts. HTTP 4xx makes robots.txt unavailable
    and permits access except for 429, which is fail-closed. HTTP 5xx, network
    errors and malformed policy responses fail closed.
    """
    try:
        origin = _origin(url)
    except ValueError as exc:
        return False, str(exc), 0.0

    entry = _cached_policy(origin, cache)
    if entry is None:
        robots_url = f"{origin}/robots.txt"
        try:
            from core.crawl_context import current_crawl_context
            crawl_ctx = current_crawl_context.get(None)
            crawl_deadline = crawl_ctx.deadline if crawl_ctx else None
            if not wait_for_host_slot(robots_url, deadline=crawl_deadline):
                deadline_exhausted = (
                    crawl_deadline is not None
                    and time.time() >= crawl_deadline
                )
                reason = (
                    "deadline_exceeded_before_robots_request"
                    if deadline_exhausted
                    else "rate_limit_cooldown_exceeds_safe_wait"
                )
                raise TimeoutError(reason)
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
                policy = _parse_robots(text)
                entry = (
                    time.monotonic() + _SUCCESS_TTL_SECONDS,
                    policy,
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
            if isinstance(exc, TimeoutError) and (
                "deadline" in str(exc).lower() or "rate_limit" in str(exc).lower()
            ):
                # A run deadline is not a robots.txt server failure. Do not
                # poison the process-wide policy cache for a later run.
                return False, str(exc), 0.0
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

    _, policy, unavailable_allows, failure_reason = entry
    if policy is None:
        if unavailable_allows:
            return True, None, 0.0
        return False, failure_reason or "robots.txt policy unavailable", 0.0

    try:
        allowed, delay = policy.decision(user_agent, url)
        return allowed, None if allowed else "robots.txt disallows this URL", delay
    except Exception as exc:
        return False, f"robots.txt policy could not be evaluated: {type(exc).__name__}", 0.0


def get_robots_sitemaps(
    url: str,
    *,
    user_agent: str = "RIF/3.1",
    timeout: float = 5.0,
    cache: dict[str, tuple[float, RobotsPolicy | None, bool, str | None]] | None = None,
) -> list[str]:
    """Return Sitemap directives from the cached robots policy without refetching it."""
    try:
        origin = _origin(url)
    except ValueError:
        return []
    entry = _cached_policy(origin, cache)
    if entry is None:
        check_robots(url, user_agent=user_agent, timeout=timeout, cache=cache)
        entry = _cached_policy(origin, cache)
    if entry is None or entry[1] is None:
        return []
    return list(entry[1].sitemaps)


def clear_robots_cache() -> None:
    """Clear process-wide robots and host-rate state; intended for deterministic tests."""
    with _CACHE_LOCK:
        _CACHE.clear()
    with _HOST_RATE_LOCK:
        _HOST_NEXT_ALLOWED.clear()
