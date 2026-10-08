from __future__ import annotations

import json
import logging
import random
import time
import threading
from typing import Any
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

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


def _retry_delay(response: Any, attempt: int) -> float:
    retry_after = str(response.headers.get("Retry-After", "")).strip()
    if retry_after.isdigit():
        delay = float(retry_after)
        if delay > 300.0:
            raise RuntimeError(f"Retry-After {delay}s is too long to wait. Aborting.")
        return delay
    if retry_after:
        try:
            dt = parsedate_to_datetime(retry_after)
            delay = (dt - datetime.now(UTC)).total_seconds()
            if delay > 300.0:
                raise RuntimeError(f"Retry-After {delay}s is too long to wait. Aborting.")
            if delay > 0:
                return delay
        except Exception:
            pass
    return min(2.0 ** attempt, 8.0) + random.uniform(0.0, 0.25)


def _apply_context_timeout(configured_timeout: float | tuple[float, float]) -> float | tuple[float, float]:
    from core.crawl_context import current_crawl_context
    ctx = current_crawl_context.get(None)
    if not ctx or not ctx.deadline:
        return configured_timeout
    
    now = time.time()
    remaining = max(0.1, ctx.deadline - now)
    
    if isinstance(configured_timeout, tuple):
        conn_to, read_to = configured_timeout
        return (min(conn_to, remaining), min(read_to, remaining))
    return min(configured_timeout, remaining)

def _sleep_or_abort(delay: float) -> None:
    from core.crawl_context import current_crawl_context
    ctx = current_crawl_context.get(None)
    if ctx and ctx.deadline:
        now = time.time()
        if now + delay > ctx.deadline:
            ctx.stop_reasons.add("deadline")
            raise TimeoutError("Deadline exceeded during retry sleep")
    time.sleep(delay)

def get_response(
    url: str,
    *,
    session: Any | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    timeout: tuple[float, float] = (8.0, 20.0),
    retries: int = 1,
    expected_status: tuple[int, ...] = (200,),
) -> Any:
    """Fetch a live URL predictably."""
    client = session or _get_session()
    merged_headers = dict(DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)

    last_exc: Exception | None = None
    target = canonicalize_url(url)
    MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB

    from core.crawl_context import current_crawl_context
    ctx = current_crawl_context.get(None)

    for attempt in range(retries + 1):
        if ctx: ctx.http_requests += 1
        try:
            effective_timeout = _apply_context_timeout(timeout)
            response = client.get(
                target,
                params=params,
                headers=merged_headers,
                timeout=effective_timeout,
                allow_redirects=True,
                stream=True
            )
            content_length = int(response.headers.get("Content-Length", 0) or 0)
            if content_length > MAX_RESPONSE_BYTES:
                response.close()
                raise ValueError(f"Response too large: {content_length} bytes")
            chunks: list[bytes] = []
            total = 0
            try:
                iterator = response.iter_content(chunk_size=64 * 1024)
                for chunk in iterator:
                    from core.crawl_context import current_crawl_context
                    ctx = current_crawl_context.get(None)
                    if ctx and ctx.check_limits():
                        response.close()
                        raise TimeoutError("Deadline exceeded during download stream")
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        response.close()
                        raise ValueError(f"Response exceeded {MAX_RESPONSE_BYTES} byte limit")
                    chunks.append(chunk)
                response._content = b"".join(chunks)
                response._content_consumed = True
            except (AttributeError, TypeError):
                raw_body = getattr(response, "content", b"")
                if not isinstance(raw_body, (bytes, bytearray)):
                    raw_body = b""
                body = bytes(raw_body)
                if len(body) > MAX_RESPONSE_BYTES:
                    response.close()
                    raise ValueError(f"Response exceeded {MAX_RESPONSE_BYTES} byte limit")
                response._content = body
                response._content_consumed = True

            if response.status_code in RETRYABLE_STATUS and attempt < retries:
                delay = _retry_delay(response, attempt)
                LOGGER.warning("Transient HTTP %s for %s; retrying in %.1fs", response.status_code, target, delay)
                _sleep_or_abort(delay)
                continue
            if response.status_code not in expected_status:
                response.raise_for_status()
            return response
        except requests.HTTPError as exc:
            last_exc = exc
            raise
        except (requests.RequestException, TimeoutError) as exc:
            last_exc = exc
            if isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema, requests.exceptions.MissingSchema)):
                raise
            if attempt < retries:
                delay = min(2.0 ** attempt, 4.0) + random.uniform(0.0, 0.25)
                LOGGER.warning("Network fetch failed for %s (%s); retrying in %.1fs", target, exc, delay)
                _sleep_or_abort(delay)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Unable to fetch {target}")


def fetch_text(url: str, *, session: Any | None = None, headers: dict[str, str] | None = None,
               timeout: tuple[float, float] = (8.0, 20.0), retries: int = 1) -> tuple[str, str]:
    response = get_response(url, session=session, headers=headers, timeout=timeout, retries=retries)
    content_type = str(response.headers.get("Content-Type", "")).lower()
    if content_type and not any(t in content_type for t in ("text/html", "application/xhtml+xml", "text/plain", "application/xml", "text/xml", "application/json")):
        raise ValueError(f"Unexpected content type for {url}: {content_type}")
    return response.text, str(response.url)


def fetch_json(url: str, *, session: Any | None = None, headers: dict[str, str] | None = None,
               params: dict[str, Any] | None = None, timeout: tuple[float, float] = (8.0, 20.0), retries: int = 1) -> tuple[dict[str, Any], str]:
    response = get_response(url, session=session, headers=headers, params=params, timeout=timeout, retries=retries)
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON returned by {url}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object from {url}, got {type(data).__name__}")
    return data, str(response.url)


def post_json(url: str, *, json_body: dict[str, Any], headers: dict[str, str] | None = None, timeout: tuple[float,float]=(8.0,20.0), retries: int=1) -> tuple[dict[str,Any], str]:
    client=_get_session(); merged=dict(DEFAULT_HEADERS); merged.update(headers or {})
    merged["Content-Type"]="application/json"
    target=canonicalize_url(url)
    import requests as _requests
    for attempt in range(retries+1):
        try:
            effective_timeout = _apply_context_timeout(timeout)
            response=client.post(target,json=json_body,headers=merged,timeout=effective_timeout,allow_redirects=True)
            if response.status_code in RETRYABLE_STATUS and attempt < retries:
                _sleep_or_abort(_retry_delay(response,attempt)); continue
            response.raise_for_status()
            data=response.json()
            if not isinstance(data,dict): raise ValueError(f"Expected JSON object from {url}")
            return data,str(response.url)
        except (_requests.RequestException, TimeoutError):
            if attempt >= retries: raise
            _sleep_or_abort(min(2.0**attempt,4.0)+random.uniform(0,.25))
    raise RuntimeError(f"Unable to POST {target}")
