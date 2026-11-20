from __future__ import annotations

import json
import ipaddress
import socket
import logging
import random
import time
import threading
from typing import Any
from urllib.parse import urlsplit, urlunsplit, urljoin, parse_qsl, urlencode

LOGGER = logging.getLogger("rif.http")

DEFAULT_HEADERS = {
    "User-Agent": "RIF/3.1 (+research-intelligence-pipeline)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.6",
    "Accept-Language": "en-US,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid"}
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def canonicalize_url(url: str) -> str:
    parts = urlsplit(str(url).strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query, doseq=True), ""))


from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import requests
from requests.adapters import HTTPAdapter

_SESSION_LOCAL = threading.local()

def _get_session() -> requests.Session:
    client = getattr(_SESSION_LOCAL, "session", None)
    if client is None:
        client = requests.Session()
        client.headers.update(DEFAULT_HEADERS)
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
        client.mount("http://", adapter)
        client.mount("https://", adapter)
        _SESSION_LOCAL.session = client
    return client


def _retry_after_seconds(response: Any) -> float | None:
    raw = str(response.headers.get("Retry-After", "")).strip()
    if not raw:
        return None
    try:
        if raw.isdigit():
            return min(float(int(raw)), 365.0 * 24 * 60 * 60)
        retry_at = parsedate_to_datetime(raw)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        delay = (retry_at - datetime.now(UTC)).total_seconds()
        return min(max(0.0, delay), 365.0 * 24 * 60 * 60)
    except (TypeError, ValueError, OverflowError):
        return None


def _retry_delay(response: Any, attempt: int) -> float:
    delay = _retry_after_seconds(response)
    if delay is not None and delay > 0:
        if delay > 300.0:
            raise RuntimeError(f"Retry-After {delay}s is too long to wait. Aborting.")
        return delay
    return min(2.0 ** attempt, 8.0) + random.uniform(0.0, 0.25)


def _defer_rate_limited_host(
    url: str,
    response: Any,
    attempt: int,
    delay_seconds: float | None = None,
) -> None:
    """Prevent subsequent requests to an origin before its 429 Retry-After."""
    server_delay = _retry_after_seconds(response)
    if server_delay is not None and server_delay > 0:
        delay = server_delay
    elif delay_seconds is not None:
        delay = delay_seconds
    else:
        delay = min(2.0 ** attempt, 8.0) + random.uniform(0.0, 0.25)
    from core.robots_policy import defer_host
    defer_host(url, delay)


def _apply_context_timeout(configured_timeout: float | tuple[float, float]) -> float | tuple[float, float]:
    from core.crawl_context import current_crawl_context
    ctx = current_crawl_context.get(None)
    if not ctx or not ctx.deadline:
        return configured_timeout
    
    now = time.time()
    remaining = ctx.deadline - now
    if remaining <= 0:
        ctx.stop_reasons.add("deadline")
        raise TimeoutError("Deadline exceeded before HTTP request")

    if isinstance(configured_timeout, tuple):
        conn_to, read_to = configured_timeout
        return (min(conn_to, remaining), min(read_to, remaining))
    return min(configured_timeout, remaining)

def _sleep_or_abort(delay: float) -> None:
    from core.crawl_context import current_crawl_context
    ctx = current_crawl_context.get(None)
    if ctx and ctx.deadline:
        now = time.time()
        if now + delay >= ctx.deadline:
            ctx.stop_reasons.add("deadline")
            raise TimeoutError("Deadline exceeded during retry sleep")
    time.sleep(delay)

def _safe_redirect_target(
    current_url: str,
    location: str | None,
    *,
    allow_loopback: bool = False,
) -> str:
    """Resolve and validate an HTTP redirect before issuing the next request.

    Redirects to non-HTTP schemes, credential-bearing URLs, local hostnames,
    and non-global IP literals are rejected. A narrowly scoped loopback opt-in
    exists for controlled local tests; production callers must leave it disabled.
    DNS names are not resolved here; robots policy is checked separately per hop.
    """
    if not location or not str(location).strip():
        raise ValueError("redirect_missing_location")
    target = canonicalize_url(urljoin(current_url, str(location).strip()))
    parsed = urlsplit(target)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("redirect_to_invalid_or_unsafe_url")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("redirect_to_invalid_or_unsafe_url") from exc

    host = parsed.hostname.rstrip(".").casefold()
    if (
        (host == "localhost" or host.endswith(".localhost")) and not allow_loopback
    ) or host.endswith(".local") or host in {"metadata", "metadata.google.internal"}:
        raise ValueError("redirect_to_local_or_metadata_host")
    address = None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # inet_aton also recognizes legacy numeric IPv4 forms such as 127.1,
        # 0177.0.0.1 and 0x7f000001 that some resolvers treat as loopback.
        try:
            address = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            pass
    if address is not None and not address.is_global:
        if not (allow_loopback and address.is_loopback):
            raise ValueError("redirect_to_non_public_ip")
    return target


def _url_origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, host, port


# ---------------------------------------------------------------------------
# OpenAlex budget handling (https://help.openalex.org/how-to-use-the-api/
# rate-limits-and-authentication): keyless use has a small daily USD budget
# (observed live: X-RateLimit-Limit-USD 0.1, search cost $0.001 -> ~100 searches
# per day, then 429 with Retry-After of several hours). A free API key raises the
# budget 10x and is sent as the ``api_key`` parameter. Single-record lookups
# (/works/<id>, /works/doi:...) cost $0 and keep working.
# ---------------------------------------------------------------------------
_OPENALEX_HOST = "api.openalex.org"
_OPENALEX_EXHAUSTED_UNTIL = 0.0
_OPENALEX_LOCK = threading.Lock()


def _is_openalex(url: str) -> bool:
    return (urlsplit(str(url or "")).hostname or "").casefold() == _OPENALEX_HOST


def _is_openalex_singleton(url: str, params: dict[str, Any] | None) -> bool:
    parts = urlsplit(str(url or ""))
    path_segments = [seg for seg in parts.path.split("/") if seg]
    query_keys = {k for k, _ in parse_qsl(parts.query)} | set((params or {}).keys())
    return len(path_segments) >= 2 and not (query_keys - {"api_key", "select", "mailto"})


def _openalex_params(url: str, params: dict[str, Any] | None) -> dict[str, Any] | None:
    import os

    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if not key or not _is_openalex(url):
        return params
    merged = dict(params or {})
    if "api_key=" not in str(url):
        merged.setdefault("api_key", key)
    return merged


def _openalex_budget_check(url: str, params: dict[str, Any] | None) -> None:
    if not _is_openalex(url) or _is_openalex_singleton(url, params):
        return
    with _OPENALEX_LOCK:
        remaining = _OPENALEX_EXHAUSTED_UNTIL - time.time()
    if remaining > 0:
        raise RuntimeError(
            f"OpenAlex daily budget exhausted for this client; resets in {remaining / 3600:.1f} h "
            "(set OPENALEX_API_KEY for a 10x budget)"
        )


def _openalex_note_rate_limit(url: str, response: Any) -> bool:
    """Record an exhausted daily budget; True when the request must not be retried."""
    global _OPENALEX_EXHAUSTED_UNTIL
    if not _is_openalex(url) or int(getattr(response, "status_code", 0) or 0) != 429:
        return False
    headers = getattr(response, "headers", {}) or {}
    if str(headers.get("X-RateLimit-Remaining", "")).strip() != "0":
        return False
    try:
        wait = float(headers.get("Retry-After") or headers.get("X-RateLimit-Reset") or 3600)
    except (TypeError, ValueError):
        wait = 3600.0
    with _OPENALEX_LOCK:
        _OPENALEX_EXHAUSTED_UNTIL = max(_OPENALEX_EXHAUSTED_UNTIL, time.time() + wait)
    return True


def _openalex_cache_path(url: str, params: dict[str, Any] | None):
    import hashlib
    import os
    from pathlib import Path

    root = os.environ.get("RIF_OPENALEX_CACHE_DIR", "outputs/cache/openalex").strip()
    if not root or not _is_openalex(url) or _is_openalex_singleton(url, params):
        return None
    items = sorted((k, str(v)) for k, v in (params or {}).items() if k != "api_key")
    key = hashlib.sha256(json.dumps([canonicalize_url(url), items]).encode("utf-8")).hexdigest()
    return Path(root) / f"{key}.json"


def _openalex_cache_read(path) -> dict[str, Any] | None:
    import os

    if path is None or not path.is_file():
        return None
    ttl = float(os.environ.get("RIF_OPENALEX_CACHE_TTL_SECONDS", str(3 * 24 * 3600)))
    if time.time() - path.stat().st_mtime > ttl:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _openalex_cache_write(path, data: dict[str, Any]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        LOGGER.debug("OpenAlex cache write failed for %s: %s", path, exc)


def get_response(
    url: str,
    *,
    session: Any | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: tuple[float, float] = (8.0, 20.0),
    retries: int = 1,
    expected_status: tuple[int, ...] = (200,),
    allow_redirects: bool = True,
    check_redirect_robots: bool = True,
) -> Any:
    """Fetch a live URL with bounded retries and policy-checked redirects."""
    params = _openalex_params(url, params)
    _openalex_budget_check(url, params)
    client = session or _get_session()
    merged_headers = dict(DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)

    last_exc: Exception | None = None
    target = canonicalize_url(url)
    max_response_bytes = 10 * 1024 * 1024
    max_redirects = 10
    redirect_statuses = {301, 302, 303, 307, 308}

    from core.crawl_context import current_crawl_context
    ctx = current_crawl_context.get(None)

    for attempt in range(retries + 1):
        request_url = target
        request_params = params
        request_headers = dict(merged_headers)
        redirects_followed = 0
        response = None
        try:
            while True:
                if ctx:
                    ctx.http_requests += 1
                effective_timeout = _apply_context_timeout(timeout)
                response = client.get(
                    request_url,
                    params=request_params,
                    headers=request_headers,
                    timeout=effective_timeout,
                    allow_redirects=False,
                    stream=True,
                )
                request_params = None

                status = int(response.status_code)
                if allow_redirects and status in redirect_statuses:
                    if redirects_followed >= max_redirects:
                        response.close()
                        raise requests.TooManyRedirects(
                            f"Exceeded {max_redirects} redirects while fetching {target}"
                        )
                    location = response.headers.get("Location")
                    response.close()
                    next_url = _safe_redirect_target(request_url, location)

                    if _url_origin(request_url) != _url_origin(next_url):
                        # A configured session-level auth object cannot be safely
                        # forwarded or temporarily mutated on a shared session.
                        if getattr(client, "auth", None) is not None:
                            raise PermissionError(
                                "cross_origin_redirect_blocked_for_authenticated_session"
                            )

                        def is_sensitive_header(name: str) -> bool:
                            lowered = name.casefold().replace("_", "-")
                            return any(
                                marker in lowered
                                for marker in (
                                    "authorization",
                                    "proxy-authorization",
                                    "cookie",
                                    "api-key",
                                    "access-token",
                                    "auth-token",
                                    "client-secret",
                                    "credential",
                                )
                            )

                        request_headers = {
                            key: value
                            for key, value in request_headers.items()
                            if not is_sensitive_header(key)
                        }
                        # requests merges per-request headers with session headers;
                        # explicitly suppress any sensitive session defaults too.
                        for key in (getattr(client, "headers", {}) or {}):
                            if is_sensitive_header(str(key)):
                                request_headers[str(key)] = None

                    crawl_delay = 0.0
                    if check_redirect_robots:
                        from core.robots_policy import check_robots
                        allowed, reason, crawl_delay = check_robots(next_url)
                        if not allowed:
                            raise PermissionError(
                                f"robots_disallow_redirect: {reason or 'robots policy denied redirect'}"
                            )

                    from core.robots_policy import wait_for_host_slot
                    if not wait_for_host_slot(
                        next_url,
                        crawl_delay=crawl_delay,
                        deadline=ctx.deadline if ctx else None,
                    ):
                        raise TimeoutError(
                            "rate_limit_cooldown_or_deadline_before_redirect_request"
                        )

                    request_url = next_url
                    redirects_followed += 1
                    continue
                break

            content_length = int(response.headers.get("Content-Length", 0) or 0)
            if content_length > max_response_bytes:
                response.close()
                raise ValueError(f"Response too large: {content_length} bytes")
            chunks: list[bytes] = []
            total = 0
            try:
                iterator = response.iter_content(chunk_size=64 * 1024)
                for chunk in iterator:
                    ctx = current_crawl_context.get(None)
                    if ctx and ctx.check_limits():
                        response.close()
                        raise TimeoutError("Deadline exceeded during download stream")
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_response_bytes:
                        response.close()
                        raise ValueError(
                            f"Response exceeded {max_response_bytes} byte limit"
                        )
                    chunks.append(chunk)
                response._content = b"".join(chunks)
                response._content_consumed = True
            except (AttributeError, TypeError):
                raw_body = getattr(response, "content", b"")
                if not isinstance(raw_body, (bytes, bytearray)):
                    raw_body = b""
                body = bytes(raw_body)
                if len(body) > max_response_bytes:
                    response.close()
                    raise ValueError(
                        f"Response exceeded {max_response_bytes} byte limit"
                    )
                response._content = body
                response._content_consumed = True

            if _openalex_note_rate_limit(request_url, response):
                response.close()
                raise RuntimeError(
                    "OpenAlex daily budget exhausted (HTTP 429, X-RateLimit-Remaining 0); "
                    "set OPENALEX_API_KEY for a 10x budget"
                )
            if response.status_code in RETRYABLE_STATUS and attempt < retries:
                if response.status_code == 429:
                    try:
                        delay = _retry_delay(response, attempt)
                    except RuntimeError:
                        _defer_rate_limited_host(request_url, response, attempt)
                        response.close()
                        raise
                    _defer_rate_limited_host(
                        request_url, response, attempt, delay_seconds=delay
                    )
                else:
                    delay = _retry_delay(response, attempt)
                LOGGER.warning(
                    "Transient HTTP %s for %s; retrying in %.1fs",
                    response.status_code,
                    request_url,
                    delay,
                )
                response.close()
                try:
                    _sleep_or_abort(delay)
                except TimeoutError:
                    raise
                if response.status_code == 429:
                    from core.robots_policy import wait_for_host_slot
                    if not wait_for_host_slot(
                        request_url,
                        deadline=ctx.deadline if ctx else None,
                    ):
                        raise TimeoutError(
                            "rate_limit_cooldown_or_deadline_before_retry"
                        )
                continue

            if response.status_code == 429:
                _defer_rate_limited_host(request_url, response, attempt)
            if response.status_code not in expected_status:
                response.raise_for_status()
            return response
        except requests.HTTPError as exc:
            last_exc = exc
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            raise
        except TimeoutError as exc:
            last_exc = exc
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            message = str(exc).casefold()
            if (
                "deadline exceeded" in message
                or "rate_limit" in message
                or "cooldown" in message
            ):
                raise
            if attempt < retries:
                delay = min(2.0 ** attempt, 4.0) + random.uniform(0.0, 0.25)
                LOGGER.warning(
                    "Request timed out for %s (%s); retrying in %.1fs",
                    target,
                    exc,
                    delay,
                )
                _sleep_or_abort(delay)
                continue
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            if isinstance(
                exc,
                (
                    requests.exceptions.InvalidURL,
                    requests.exceptions.InvalidSchema,
                    requests.exceptions.MissingSchema,
                ),
            ):
                raise
            if attempt < retries:
                delay = min(2.0 ** attempt, 4.0) + random.uniform(0.0, 0.25)
                LOGGER.warning(
                    "Network fetch failed for %s (%s); retrying in %.1fs",
                    target,
                    exc,
                    delay,
                )
                _sleep_or_abort(delay)
                continue
            raise
        except Exception:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Unable to fetch {target}")


def fetch_text(url: str, *, session: Any | None = None, headers: dict[str, str] | None = None,
               timeout: tuple[float, float] = (8.0, 20.0), retries: int = 1) -> tuple[str, str]:
    response = get_response(url, session=session, headers=headers, timeout=timeout, retries=retries)
    try:
        content_type = str(response.headers.get("Content-Type", "")).lower()
        if content_type and not any(t in content_type for t in ("text/html", "application/xhtml+xml", "text/plain", "application/xml", "text/xml", "application/json")):
            raise ValueError(f"Unexpected content type for {url}: {content_type}")
        return response.text, str(response.url)
    finally:
        close_response = getattr(response, "close", None)
        if callable(close_response):
            close_response()


def fetch_json(url: str, *, session: Any | None = None, headers: dict[str, str] | None = None,
               params: dict[str, Any] | None = None, timeout: tuple[float, float] = (8.0, 20.0), retries: int = 1) -> tuple[dict[str, Any], str]:
    cache_path = _openalex_cache_path(url, params)
    cached = _openalex_cache_read(cache_path)
    if cached is not None:
        return cached, canonicalize_url(url)
    for attempt in range(max(0, int(retries)) + 1):
        response = get_response(url, session=session, headers=headers, params=params, timeout=timeout, retries=retries)
        try:
            try:
                data = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                # Undecodable 200 bodies are usually truncated in transit; retry
                # within the same bounded budget before reporting the failure.
                if attempt < retries:
                    continue
                raise ValueError(f"Invalid JSON returned by {url}: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"Expected JSON object from {url}, got {type(data).__name__}")
            _openalex_cache_write(cache_path, data)
            return data, str(response.url)
        finally:
            close_response = getattr(response, "close", None)
            if callable(close_response):
                close_response()
    raise RuntimeError(f"Unable to fetch JSON from {url}")


def post_multipart_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    fields: dict[str, tuple[str | None, str, str]],
    headers: dict[str, str] | None = None,
    timeout: tuple[float, float] = (8.0, 20.0),
    retries: int = 1,
) -> tuple[dict[str, Any], str]:
    """POST multipart form fields and require a JSON-object response.

    Each form part maps to (filename, value, content_type). Do not set Content-Type
    manually: requests must generate the multipart boundary. Redirects are rejected
    so API credentials and form data are never forwarded to another origin.
    """
    client = _get_session()
    merged = dict(DEFAULT_HEADERS)
    merged.update(headers or {})
    target = canonicalize_url(url)
    import requests as _requests
    from core.crawl_context import current_crawl_context
    from core.robots_policy import wait_for_host_slot

    for attempt in range(retries + 1):
        ctx = current_crawl_context.get(None)
        response = None
        try:
            if ctx:
                ctx.http_requests += 1
            response = client.post(
                target,
                params=params,
                files=fields,
                headers=merged,
                timeout=_apply_context_timeout(timeout),
                allow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                raise _requests.HTTPError(
                    f"Unexpected redirect from API POST {target}: HTTP {response.status_code}",
                    response=response,
                )

            if response.status_code in RETRYABLE_STATUS and attempt < retries:
                try:
                    delay = _retry_delay(response, attempt)
                except RuntimeError:
                    if response.status_code == 429:
                        _defer_rate_limited_host(target, response, attempt)
                    raise
                if response.status_code == 429:
                    _defer_rate_limited_host(
                        target, response, attempt, delay_seconds=delay
                    )
                response.close()
                _sleep_or_abort(delay)
                if response.status_code == 429 and not wait_for_host_slot(
                    target, deadline=ctx.deadline if ctx else None
                ):
                    raise TimeoutError(
                        "rate_limit_cooldown_or_deadline_before_retry"
                    )
                continue

            if response.status_code == 429:
                _defer_rate_limited_host(target, response, attempt)
            response.raise_for_status()
            content = response.content
            if len(content) > 10 * 1024 * 1024:
                raise ValueError("Response exceeded 10485760 byte limit")
            try:
                data = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                # A 200 whose body cannot be decoded is almost always a body
                # truncated in transit (observed live: "Unterminated string" at
                # ~380 KB from the EU Search API). Treat it like other transport
                # failures and use the bounded retry budget before failing.
                if attempt < retries:
                    response.close()
                    _sleep_or_abort(min(2.0 ** attempt, 4.0) + random.uniform(0.0, 0.25))
                    continue
                raise ValueError(f"Invalid JSON returned by {url}: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError(
                    f"Expected JSON object from {url}, got {type(data).__name__}"
                )
            response_url = str(response.url)
            response.close()
            return data, response_url
        except _requests.HTTPError:
            if response is not None:
                response.close()
            raise
        except (_requests.RequestException, TimeoutError) as exc:
            if response is not None:
                response.close()
            if (
                attempt >= retries
                or "deadline exceeded" in str(exc).casefold()
                or "rate_limit" in str(exc).casefold()
            ):
                raise
            _sleep_or_abort(
                min(2.0 ** attempt, 4.0) + random.uniform(0.0, 0.25)
            )
        except Exception:
            if response is not None:
                response.close()
            raise
    raise RuntimeError(f"Unable to POST {target}")


def post_json(
    url: str,
    *,
    json_body: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout: tuple[float, float] = (8.0, 20.0),
    retries: int = 1,
) -> tuple[dict[str, Any], str]:
    """POST JSON to a read-only API endpoint with bounded retries.

    Redirects are rejected so API credentials and request bodies are never
    silently forwarded to a different endpoint.
    """
    client = _get_session()
    merged = dict(DEFAULT_HEADERS)
    merged.update(headers or {})
    merged["Content-Type"] = "application/json"
    target = canonicalize_url(url)
    import requests as _requests
    from core.crawl_context import current_crawl_context
    from core.robots_policy import wait_for_host_slot

    for attempt in range(retries + 1):
        ctx = current_crawl_context.get(None)
        response = None
        try:
            if ctx:
                ctx.http_requests += 1
            response = client.post(
                target,
                json=json_body,
                headers=merged,
                timeout=_apply_context_timeout(timeout),
                allow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                raise _requests.HTTPError(
                    f"Unexpected redirect from API POST {target}: HTTP {response.status_code}",
                    response=response,
                )

            if response.status_code in RETRYABLE_STATUS and attempt < retries:
                try:
                    delay = _retry_delay(response, attempt)
                except RuntimeError:
                    if response.status_code == 429:
                        _defer_rate_limited_host(target, response, attempt)
                    raise
                if response.status_code == 429:
                    _defer_rate_limited_host(
                        target, response, attempt, delay_seconds=delay
                    )
                response.close()
                _sleep_or_abort(delay)
                if response.status_code == 429 and not wait_for_host_slot(
                    target, deadline=ctx.deadline if ctx else None
                ):
                    raise TimeoutError(
                        "rate_limit_cooldown_or_deadline_before_retry"
                    )
                continue

            if response.status_code == 429:
                _defer_rate_limited_host(target, response, attempt)
            response.raise_for_status()
            content = response.content
            if len(content) > 10 * 1024 * 1024:
                raise ValueError("Response exceeded 10485760 byte limit")
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError(f"Expected JSON object from {url}")
            response_url = str(response.url)
            response.close()
            return data, response_url
        except _requests.HTTPError:
            if response is not None:
                response.close()
            raise
        except (_requests.RequestException, TimeoutError) as exc:
            if response is not None:
                response.close()
            if (
                attempt >= retries
                or "deadline exceeded" in str(exc).casefold()
                or "rate_limit" in str(exc).casefold()
            ):
                raise
            _sleep_or_abort(
                min(2.0 ** attempt, 4.0) + random.uniform(0.0, 0.25)
            )
        except Exception:
            if response is not None:
                response.close()
            raise
    raise RuntimeError(f"Unable to POST {target}")
