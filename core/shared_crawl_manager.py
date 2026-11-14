import threading
import sqlite3
import hashlib
from datetime import datetime, UTC
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, asdict

import requests
from bs4 import BeautifulSoup
from core.http_client import get_response, canonicalize_url, _safe_redirect_target
from core.robots_policy import check_robots, wait_for_host_slot

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

LOGGER = logging.getLogger("rif.shared_crawl")

@dataclass
class CrawlResult:
    canonical_url: str
    final_url: str
    status_code: int
    content_type: str
    title: str
    html: str
    clean_text: str
    content_hash: str
    fetch_method: str
    from_cache: bool
    error: Optional[str] = None
    retry_after: Optional[str] = None

class SharedCrawlManager:
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> 'SharedCrawlManager':
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.db_path = "outputs/crawl_cache.db"
        self._init_db()
        
        self.lock = threading.Lock()
        
        # In-flight deduplication
        self.in_flight: Dict[str, Future] = {}
        
        # Run-level cache (memory)
        self.run_cache: Dict[str, CrawlResult] = {}
        
        # Concurrency pools
        self.http_pool = ThreadPoolExecutor(max_workers=15, thread_name_prefix="crawl_http")
        self.browser_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="crawl_browser")
        
        # Per-domain throttling
        self.domain_locks: Dict[str, threading.Semaphore] = {}
        self.domain_last_fetch: Dict[str, float] = {}
        
        # Initialize Playwright thread-local
        self.pw_local = threading.local()
        self.stats_unique_urls = 0
        self.stats_actual_fetches = 0
        self.stats_cache_hits = 0
        self.stats_in_flight_dedupes = 0

    def get_stats(self) -> dict:
        with self.lock:
            return {
                'unique_urls_requested': self.stats_unique_urls, 
                'actual_network_fetches': self.stats_actual_fetches, 
                'cache_hits': self.stats_cache_hits,
                'in_flight_deduplications': self.stats_in_flight_dedupes
            }

    def _init_db(self):
        import os
        os.makedirs("outputs", exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            # enable WAL mode for concurrency
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            # simple persistent cache
            conn.execute("""
                CREATE TABLE IF NOT EXISTS crawl_cache (
                    url TEXT PRIMARY KEY,
                    final_url TEXT,
                    status_code INTEGER,
                    content_type TEXT,
                    title TEXT,
                    html TEXT,
                    clean_text TEXT,
                    content_hash TEXT,
                    fetch_method TEXT,
                    fetched_at TEXT
                )
            """)
            conn.commit()

    def _get_domain_semaphore(self, url: str) -> threading.Semaphore:
        domain = urlparse(url).netloc.lower()
        with self.lock:
            if domain not in self.domain_locks:
                # Default 2 concurrent requests per domain
                self.domain_locks[domain] = threading.Semaphore(2)
            return self.domain_locks[domain]

    def _throttle_domain(
        self,
        url: str,
        deadline: float | None = None,
        minimum_delay: float = 0.0,
    ) -> bool:
        return wait_for_host_slot(
            url,
            crawl_delay=minimum_delay,
            deadline=deadline,
        )

    def fetch(self, url: str, bypass_cache: bool = False) -> CrawlResult:
        from core.source_registry import current_force_source_refresh
        canonical = canonicalize_url(url)
        try:
            # The shared manager is a public-web crawler, not a local-service
            # client. Reject loopback, private, link-local, and metadata targets
            # before even requesting robots.txt for them.
            _safe_redirect_target(canonical, canonical)
        except ValueError as exc:
            return CrawlResult(
                canonical_url=canonical, final_url=canonical, status_code=0,
                content_type="", title="", html="", clean_text="", content_hash="",
                fetch_method="security", from_cache=False, error=str(exc),
            )

        # Check policy before cache lookup: a cached page must not bypass the
        # origin's current robots.txt rules.
        allowed, robots_reason, crawl_delay = check_robots(canonical)
        if not allowed:
            return CrawlResult(
                canonical_url=canonical, final_url=canonical, status_code=0,
                content_type="", title="", html="", clean_text="", content_hash="",
                fetch_method="robots", from_cache=False,
                error=robots_reason or "robots_disallow",
            )

        # Merge explicitly passed bypass_cache with global refresh flag
        bypass_cache = bypass_cache or current_force_source_refresh.get()
        
        fut_to_await = None
        with self.lock:
            self.stats_unique_urls += 1
            # 1. Check in-flight
            if canonical in self.in_flight:
                self.stats_in_flight_dedupes += 1
                fut_to_await = self.in_flight[canonical]
            
            if not fut_to_await:
                # 2. Check run-level cache
                if not bypass_cache and canonical in self.run_cache:
                    self.stats_cache_hits += 1
                    res = self.run_cache[canonical]
                    res.from_cache = True
                    return res
                
                # 3. Check persistent cache (sqlite)
                if not bypass_cache:
                    cached = self._read_cache(canonical)
                    if cached:
                        self.stats_cache_hits += 1
                        self.run_cache[canonical] = cached
                        return cached
                
                # Not found anywhere, we must fetch. Create a future.
                self.stats_actual_fetches += 1
                fut = Future()
                self.in_flight[canonical] = fut
                
        if fut_to_await:
            return fut_to_await.result()

        # Execute fetch outside the global lock, but manage domain concurrency
        try:
            result = self._execute_fetch(canonical, crawl_delay=crawl_delay)
            
            # Save to caches
            if not result.error:
                with self.lock:
                    self.run_cache[canonical] = result
                self._write_cache(result)
                
            fut.set_result(result)
            return result
        except Exception as e:
            err_res = CrawlResult(
                canonical_url=canonical, final_url=canonical, status_code=0,
                content_type="", title="", html="", clean_text="", content_hash="",
                fetch_method="error", from_cache=False, error=str(e)
            )
            fut.set_result(err_res)
            return err_res
        finally:
            with self.lock:
                if canonical in self.in_flight:
                    del self.in_flight[canonical]

    def _execute_fetch(self, url: str, crawl_delay: float = 0.0) -> CrawlResult:
        import contextvars
        import time
        from concurrent.futures import TimeoutError as FuturesTimeoutError
        from core.crawl_context import current_crawl_context
        
        sem = self._get_domain_semaphore(url)
        
        with sem:
            ctx_obj = current_crawl_context.get(None)
            deadline = ctx_obj.deadline if ctx_obj else None
            if not self._throttle_domain(url, deadline=deadline, minimum_delay=crawl_delay):
                wait_error = (
                    "deadline_exceeded_before_request"
                    if ctx_obj and "deadline" in ctx_obj.stop_reasons
                    else "rate_limit_cooldown_exceeds_safe_wait"
                )
                return CrawlResult(
                    canonical_url=url, final_url=url, status_code=0,
                    content_type="", title="", html="", clean_text="",
                    content_hash="", fetch_method="error", from_cache=False,
                    error=wait_error,
                )

            timeout = None
            if ctx_obj and ctx_obj.deadline:
                timeout = max(0.1, ctx_obj.deadline - time.time())
                if timeout <= 0.1:
                    return CrawlResult(canonical_url=url, final_url=url, status_code=0, content_type="", title="", html="", clean_text="", content_hash="", fetch_method="error", from_cache=False, error="deadline_exceeded")

            # Try HTTP first
            if ctx_obj:
                with ctx_obj.lock:
                    ctx_obj.http_requests += 1

            ctx = contextvars.copy_context()
            http_future = self.http_pool.submit(ctx.run, self._do_http_fetch, url)
            try:
                result = http_future.result(timeout=timeout)
            except FuturesTimeoutError:
                return CrawlResult(canonical_url=url, final_url=url, status_code=0, content_type="", title="", html="", clean_text="", content_hash="", fetch_method="error", from_cache=False, error="deadline_exceeded")
            
            # Browser rendering is only a rendering fallback. It must never
            # bypass HTTP access controls or rate limits. A transport failure
            # may use one bounded browser attempt; HTTP errors never may.
            transport_failure = (
                result.status_code == 0
                and bool(result.error)
                and result.error.startswith(("ConnectionError:", "Timeout:", "ConnectTimeout:", "ReadTimeout:"))
            )
            if transport_failure or (
                not result.error and self._needs_browser(result.html, result.status_code)
            ):
                if HAS_PLAYWRIGHT:
                    LOGGER.info(f"Falling back to browser fetch for {url}")
                    if ctx_obj:
                        with ctx_obj.lock: ctx_obj.browser_attempts += 1
                        with ctx_obj.lock: ctx_obj.playwright_used += 1
                        with ctx_obj.lock: ctx_obj.js_shells_detected += 1 if not result.error else 0
                        with ctx_obj.lock: ctx_obj.browser_escalations += 1

                    if ctx_obj and ctx_obj.deadline:
                        timeout = ctx_obj.deadline - time.time()
                        if timeout <= 0.1:
                            return CrawlResult(
                                canonical_url=url, final_url=url, status_code=0,
                                content_type="", title="", html="", clean_text="",
                                content_hash="", fetch_method="error", from_cache=False,
                                error="deadline_exceeded_before_browser_fallback",
                            )

                    ctx2 = contextvars.copy_context()
                    browser_future = self.browser_pool.submit(
                        ctx2.run, self._do_browser_fetch, url, timeout
                    )
                    try:
                        result = browser_future.result(timeout=timeout)
                        if result.error:
                            if ctx_obj:
                                with ctx_obj.lock: ctx_obj.browser_failures += 1
                                with ctx_obj.lock: ctx_obj.playwright_fallback_failed += 1
                        else:
                            if ctx_obj:
                                with ctx_obj.lock: ctx_obj.browser_successes += 1
                    except FuturesTimeoutError:
                        if ctx_obj:
                            with ctx_obj.lock: ctx_obj.browser_failures += 1
                            with ctx_obj.lock: ctx_obj.playwright_fallback_failed += 1
                        return CrawlResult(canonical_url=url, final_url=url, status_code=0, content_type="", title="", html="", clean_text="", content_hash="", fetch_method="error", from_cache=False, error="deadline_exceeded")
                else:
                    LOGGER.warning(f"Browser fallback needed for {url}, but Playwright not installed.")
                    if not result.error and self._needs_browser(result.html, result.status_code):
                        return CrawlResult(
                            canonical_url=result.canonical_url,
                            final_url=result.final_url,
                            status_code=result.status_code,
                            content_type=result.content_type,
                            title=result.title,
                            html="",
                            clean_text="",
                            content_hash="",
                            fetch_method="error",
                            from_cache=False,
                            error="browser_fallback_unavailable: Playwright is not installed",
                        )

            return result

    @staticmethod
    def _is_anti_bot_challenge(html: str, title: str, text: str) -> bool:
        content = f"{html} {title} {text}".lower()
        return bool(
            ("cloudflare" in content and ("ray id" in content or "challenge" in content))
            or ("verify you are" in content and "human" in content)
            or ("just a moment" in content and "enable javascript" in content)
            or "pardon our interruption" in content
            or "vercel security checkpoint" in content
            or "checking if the site connection is secure" in content
            or "we're verifying your browser" in content
        )

    def _needs_browser(self, html: str, status_code: int) -> bool:
        if not 200 <= status_code < 300:
            return False
        html_lower = html.lower()
        if self._is_anti_bot_challenge(html, "", ""):
            return False
        if "enable javascript" in html_lower or "enable js" in html_lower or "javascript is required" in html_lower:
            return True
        if "<noscript>" in html_lower and len(html) < 2000:
            return True
        return False

    def _do_http_fetch(self, url: str) -> CrawlResult:
        current_url = url
        redirect_statuses = {301, 302, 303, 307, 308}
        try:
            for _ in range(11):
                response = get_response(
                    current_url,
                    timeout=(8.0, 20.0),
                    retries=2,
                    allow_redirects=False,
                )
                try:
                    status = int(response.status_code)
                    if status in redirect_statuses:
                        location = response.headers.get("Location")
                        if not location:
                            raise RuntimeError(f"HTTP {status} redirect missing Location header")
                        next_url = _safe_redirect_target(current_url, location)
                        allowed, reason, redirect_crawl_delay = check_robots(next_url)
                        if not allowed:
                            return CrawlResult(
                                canonical_url=url, final_url=next_url, status_code=0,
                                content_type="", title="", html="", clean_text="",
                                content_hash="", fetch_method="http", from_cache=False,
                                error=f"robots_disallow_redirect: {reason or 'robots policy denied redirect'}",
                            )
                        from core.crawl_context import current_crawl_context
                        crawl_ctx = current_crawl_context.get(None)
                        if not self._throttle_domain(
                            next_url,
                            deadline=crawl_ctx.deadline if crawl_ctx else None,
                            minimum_delay=redirect_crawl_delay,
                        ):
                            raise TimeoutError("host_wait_unavailable_before_redirect_request")
                        current_url = next_url
                        continue

                    if status < 200 or status >= 300:
                        response.raise_for_status()
                        raise RuntimeError(f"Unexpected HTTP status {status}")
                    html = response.text
                    final_url = str(response.url or current_url)
                    ctype = response.headers.get("Content-Type", "")
                    soup = BeautifulSoup(html, "html.parser")
                    title_tag = soup.find("title")
                    title = title_tag.get_text(strip=True) if title_tag else ""
                    clean_text = soup.get_text(separator=" ", strip=True)
                    content_hash = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()
                    if self._is_anti_bot_challenge(html, title, clean_text):
                        return CrawlResult(
                            canonical_url=url, final_url=final_url, status_code=status,
                            content_type=ctype, title=title, html="", clean_text="",
                            content_hash="", fetch_method="http", from_cache=False,
                            error="anti_bot_challenge_detected",
                        )
                    return CrawlResult(
                        canonical_url=url, final_url=final_url, status_code=status,
                        content_type=ctype, title=title, html=html, clean_text=clean_text,
                        content_hash=content_hash, fetch_method="http", from_cache=False,
                    )
                finally:
                    close_response = getattr(response, "close", None)
                    if callable(close_response):
                        close_response()

            raise RuntimeError("too_many_redirects")
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            status = int(response.status_code) if response is not None else 0
            headers = getattr(response, "headers", {}) or {}
            retry_after = headers.get("Retry-After")
            return CrawlResult(
                canonical_url=url, final_url=current_url, status_code=status,
                content_type=str(headers.get("Content-Type", "")), title="", html="",
                clean_text="", content_hash="", fetch_method="http", from_cache=False,
                error=f"http_status_{status}: {exc}",
                retry_after=str(retry_after) if retry_after else None,
            )
        except Exception as exc:
            return CrawlResult(
                canonical_url=url, final_url=current_url, status_code=0,
                content_type="", title="", html="", clean_text="", content_hash="",
                fetch_method="http", from_cache=False,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _do_browser_fetch(self, url: str, timeout_seconds: float | None = None) -> CrawlResult:
        started = time.monotonic()
        try:
            if timeout_seconds is not None and timeout_seconds <= 0.5:
                raise TimeoutError("deadline_exceeded_before_browser_start")
            if not hasattr(self.pw_local, "playwright"):
                import asyncio
                try:
                    asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                self.pw_local.playwright = sync_playwright().start()
                launch_timeout_ms = (
                    max(1, int(min(30.0, timeout_seconds) * 1000))
                    if timeout_seconds is not None else 30_000
                )
                self.pw_local.browser = self.pw_local.playwright.chromium.launch(
                    headless=True,
                    timeout=launch_timeout_ms,
                )

            remaining = (
                timeout_seconds - (time.monotonic() - started)
                if timeout_seconds is not None else 30.0
            )
            if remaining <= 0.5:
                raise TimeoutError("deadline_exceeded_before_browser_navigation")
            browser_context = self.pw_local.browser.new_context(
                user_agent="RIF/3.1 (+research-intelligence-pipeline)"
            )
            page = None
            try:
                page = browser_context.new_page()
                remaining = (
                    timeout_seconds - (time.monotonic() - started)
                    if timeout_seconds is not None else 30.0
                )
                if remaining <= 0.5:
                    raise TimeoutError("deadline_exceeded_before_browser_navigation")
                navigation_timeout_ms = max(1, int(min(30.0, remaining) * 1000))
                page.set_default_navigation_timeout(navigation_timeout_ms)

                # Enforce robots policy for every top-level document navigation,
                # including redirects, before the browser requests that document.
                def guard_document_navigation(route):
                    request = route.request
                    if request.url.lower().startswith(("http://", "https://")):
                        try:
                            _safe_redirect_target(url, request.url)
                        except ValueError:
                            route.abort("blockedbyclient")
                            return
                        allowed, _, browser_crawl_delay = check_robots(request.url)
                        if not allowed:
                            route.abort("blockedbyclient")
                            return
                        from core.crawl_context import current_crawl_context
                        from core.robots_policy import wait_for_host_slot
                        crawl_ctx = current_crawl_context.get(None)
                        deadline = crawl_ctx.deadline if crawl_ctx else None
                        if request.resource_type == "document":
                            throttled = self._throttle_domain(
                                request.url,
                                deadline=deadline,
                                minimum_delay=browser_crawl_delay,
                            )
                        elif browser_crawl_delay > 0:
                            throttled = wait_for_host_slot(
                                request.url,
                                crawl_delay=browser_crawl_delay,
                                default_delay=0.0,
                                deadline=deadline,
                            )
                        else:
                            throttled = True
                        if not throttled:
                            route.abort("blockedbyclient")
                            return
                    elif request.resource_type == "document":
                        route.abort("blockedbyclient")
                        return
                    route.continue_()

                page.route("**/*", guard_document_navigation)
                response = page.goto(
                    url, wait_until="domcontentloaded", timeout=navigation_timeout_ms
                )
                final_url = page.url
                _safe_redirect_target(url, final_url)
                allowed, reason, _ = check_robots(final_url)
                if not allowed:
                    raise PermissionError(reason or "robots policy denied browser navigation target")
                status = response.status if response else 200
                if status < 200 or status >= 300:
                    retry_after = response.headers.get("retry-after") if response else None
                    return CrawlResult(
                        canonical_url=url, final_url=final_url, status_code=status,
                        content_type="text/html", title="", html="", clean_text="",
                        content_hash="", fetch_method="browser", from_cache=False,
                        error=f"http_status_{status}",
                        retry_after=retry_after,
                    )

                remaining = (
                    timeout_seconds - (time.monotonic() - started)
                    if timeout_seconds is not None else 2.0
                )
                if remaining <= 0:
                    raise TimeoutError("deadline_exceeded_during_browser_render")
                page.wait_for_timeout(max(0, int(min(2.0, remaining) * 1000)))
                html = page.content()
                title = page.title()
                soup = BeautifulSoup(html, "html.parser")
                clean_text = soup.get_text(separator=" ", strip=True)
                content_hash = hashlib.sha256(clean_text.encode("utf-8")).hexdigest()
                if self._is_anti_bot_challenge(html, title, clean_text):
                    return CrawlResult(
                        canonical_url=url, final_url=final_url, status_code=status,
                        content_type="text/html", title=title, html="", clean_text="",
                        content_hash="", fetch_method="browser", from_cache=False,
                        error="anti_bot_challenge_detected",
                    )
                return CrawlResult(
                    canonical_url=url, final_url=final_url, status_code=status,
                    content_type="text/html", title=title, html=html, clean_text=clean_text,
                    content_hash=content_hash, fetch_method="browser", from_cache=False,
                )
            finally:
                if page is not None:
                    page.close()
                browser_context.close()
        except Exception as exc:
            return CrawlResult(
                canonical_url=url, final_url=url, status_code=0,
                content_type="", title="", html="", clean_text="", content_hash="",
                fetch_method="browser", from_cache=False,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _read_cache(self, url: str) -> Optional[CrawlResult]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    "SELECT final_url, status_code, content_type, title, html, clean_text, content_hash, fetch_method FROM crawl_cache WHERE url = ?",
                    (url,)
                )
                row = cur.fetchone()
                if row:
                    return CrawlResult(
                        canonical_url=url,
                        final_url=row[0],
                        status_code=row[1],
                        content_type=row[2],
                        title=row[3],
                        html=row[4],
                        clean_text=row[5],
                        content_hash=row[6],
                        fetch_method=row[7],
                        from_cache=True
                    )
        except Exception as e:
            LOGGER.warning(f"Cache read failed for {url}: {e}")
        return None

    def _write_cache(self, result: CrawlResult):
        if result.error:
            return
        try:
            with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                # Keep transactions short!
                conn.execute(
                    """
                    INSERT OR REPLACE INTO crawl_cache 
                    (url, final_url, status_code, content_type, title, html, clean_text, content_hash, fetch_method, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.canonical_url, result.final_url, result.status_code,
                        result.content_type, result.title, result.html, result.clean_text,
                        result.content_hash, result.fetch_method, datetime.now(UTC).isoformat()
                    )
                )
                conn.commit()
        except Exception as e:
            LOGGER.warning(f"Cache write failed for {result.canonical_url}: {e}")
