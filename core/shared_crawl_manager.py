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
from core.http_client import get_response, canonicalize_url

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
        self.robots_cache: Dict[str, Any] = {}
        
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

    def _throttle_domain(self, domain: str, deadline: float | None = None) -> bool:
        sleep_time = 0.0
        with self.lock:
            last = self.domain_last_fetch.get(domain, 0.0)
            now = time.time()
            if now - last < 1.0:
                sleep_time = 1.0 - (now - last)
                self.domain_last_fetch[domain] = now + sleep_time
            else:
                self.domain_last_fetch[domain] = now

        if sleep_time > 0:
            if deadline is not None and time.time() + sleep_time >= deadline:
                return False
            time.sleep(sleep_time)
        return deadline is None or time.time() < deadline

    def fetch(self, url: str, bypass_cache: bool = False) -> CrawlResult:
        from core.source_registry import current_force_source_refresh
        canonical = canonicalize_url(url)

        # Robots policy applies to cached and live content alike.
        if not self._robots_allowed(canonical):
            return CrawlResult(
                canonical_url=canonical, final_url=canonical, status_code=0,
                content_type="", title="", html="", clean_text="", content_hash="",
                fetch_method="robots", from_cache=False, error="robots_disallow",
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
            result = self._execute_fetch(canonical)
            
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

    def _robots_allowed(self, url: str) -> bool:
        from core.web_collection import _robots_allowed

        return _robots_allowed(
            url, url, cache=self.robots_cache, timeout=5.0, fail_open=False
        )

    def _deadline_error(self, url: str) -> CrawlResult:
        return CrawlResult(
            canonical_url=url, final_url=url, status_code=0, content_type="",
            title="", html="", clean_text="", content_hash="",
            fetch_method="error", from_cache=False, error="deadline_exceeded",
        )

    def _execute_fetch(self, url: str) -> CrawlResult:
        import contextvars
        import time
        from concurrent.futures import TimeoutError as FuturesTimeoutError
        from core.crawl_context import current_crawl_context

        ctx_obj = current_crawl_context.get(None)
        deadline = ctx_obj.deadline if ctx_obj and ctx_obj.deadline else None
        sem = self._get_domain_semaphore(url)

        # Waiting for a per-domain slot is part of the same end-to-end deadline.
        if deadline is None:
            sem.acquire()
        else:
            remaining = deadline - time.time()
            if remaining <= 0 or not sem.acquire(timeout=remaining):
                return self._deadline_error(url)

        try:
            domain = urlparse(url).netloc.lower()
            if not self._throttle_domain(domain, deadline=deadline):
                return self._deadline_error(url)

            timeout = None
            if deadline is not None:
                timeout = deadline - time.time()
                if timeout <= 0:
                    return self._deadline_error(url)

            if ctx_obj:
                with ctx_obj.lock:
                    ctx_obj.http_requests += 1

            http_context = contextvars.copy_context()
            http_future = self.http_pool.submit(
                http_context.run, self._do_http_fetch, url
            )
            try:
                result = http_future.result(timeout=timeout)
            except FuturesTimeoutError:
                http_future.cancel()
                return self._deadline_error(url)

            # Render only successful HTTP 200 JavaScript shells. Never use a
            # browser to work around authorization, rate limits, server errors,
            # or transport failures.
            if not result.error and self._needs_browser(result.html, result.status_code):
                if not HAS_PLAYWRIGHT:
                    result.error = (
                        "javascript_shell_requires_browser_but_playwright_not_installed"
                    )
                    return result

                if deadline is not None:
                    timeout = deadline - time.time()
                    if timeout <= 0:
                        return self._deadline_error(url)

                browser_timeout_ms = (
                    max(1, int(timeout * 1000)) if timeout is not None else 30000
                )
                if ctx_obj:
                    with ctx_obj.lock:
                        ctx_obj.browser_attempts += 1
                        ctx_obj.playwright_used += 1
                        ctx_obj.js_shells_detected += 1
                        ctx_obj.browser_escalations += 1

                browser_context = contextvars.copy_context()
                browser_future = self.browser_pool.submit(
                    browser_context.run,
                    self._do_browser_fetch,
                    url,
                    browser_timeout_ms,
                )
                try:
                    result = browser_future.result(timeout=timeout)
                    if ctx_obj:
                        with ctx_obj.lock:
                            if result.error:
                                ctx_obj.browser_failures += 1
                                ctx_obj.playwright_fallback_failed += 1
                            else:
                                ctx_obj.browser_successes += 1
                except FuturesTimeoutError:
                    browser_future.cancel()
                    if ctx_obj:
                        with ctx_obj.lock:
                            ctx_obj.browser_failures += 1
                            ctx_obj.playwright_fallback_failed += 1
                    return self._deadline_error(url)

            return result
        finally:
            sem.release()

    def _needs_browser(self, html: str, status_code: int) -> bool:
        if status_code != 200:
            return False
        html_lower = html.lower()
        if "enable javascript" in html_lower or "enable js" in html_lower or "javascript is required" in html_lower:
            return True
        if "<noscript>" in html_lower and len(html) < 2000:
            return True
        if "cloudflare" in html_lower and "challenge" in html_lower:
            return True
        return False

    def _do_http_fetch(self, url: str) -> CrawlResult:
        try:
            resp = get_response(url, timeout=(8.0, 20.0), retries=2)
            html = resp.text
            final_url = resp.url
            status = resp.status_code
            ctype = resp.headers.get("Content-Type", "")
            
            soup = BeautifulSoup(html, "html.parser")
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else ""
            clean_text = soup.get_text(separator=" ", strip=True)
            content_hash = hashlib.sha256(clean_text.encode('utf-8')).hexdigest()
            
            return CrawlResult(
                canonical_url=url, final_url=final_url, status_code=status,
                content_type=ctype, title=title, html=html, clean_text=clean_text,
                content_hash=content_hash, fetch_method="http", from_cache=False
            )
        except requests.HTTPError as e:
            response = e.response
            status = response.status_code if response is not None else 0
            retry_after = response.headers.get("Retry-After") if response is not None else None
            error = f"http_{status}: {e}"
            if status == 429 and retry_after:
                error += f" (Retry-After: {retry_after})"
            return CrawlResult(
                canonical_url=url, final_url=str(getattr(response, "url", url)),
                status_code=status, content_type=str(response.headers.get("Content-Type", "")) if response is not None else "",
                title="", html="", clean_text="", content_hash="",
                fetch_method="http", from_cache=False, error=error,
            )
        except Exception as e:
            return CrawlResult(
                canonical_url=url, final_url=url, status_code=0,
                content_type="", title="", html="", clean_text="",
                content_hash="", fetch_method="http", from_cache=False, error=str(e)
            )

    def _do_browser_fetch(self, url: str, timeout_ms: int = 30000) -> CrawlResult:
        try:
            if not hasattr(self.pw_local, "playwright"):
                import asyncio
                # Playwright needs an asyncio loop per thread
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    
                self.pw_local.playwright = sync_playwright().start()
                self.pw_local.browser = self.pw_local.playwright.chromium.launch(headless=True)
            
            page = self.pw_local.browser.new_page()
            try:
                started = time.monotonic()
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                status = response.status if response else 200
                if status >= 400:
                    return CrawlResult(
                        canonical_url=url, final_url=page.url, status_code=status,
                        content_type="", title="", html="", clean_text="", content_hash="",
                        fetch_method="browser", from_cache=False,
                        error=f"http_{status}_during_browser_fallback",
                    )
                remaining_ms = timeout_ms - int((time.monotonic() - started) * 1000)
                if remaining_ms <= 0:
                    return self._deadline_error(url)
                page.wait_for_timeout(min(2000, remaining_ms))
                html = page.content()
                title = page.title()
                final_url = page.url
                status = response.status if response else 200
                
                soup = BeautifulSoup(html, "html.parser")
                clean_text = soup.get_text(separator=" ", strip=True)
                content_hash = hashlib.sha256(clean_text.encode('utf-8')).hexdigest()
                
                return CrawlResult(
                    canonical_url=url, final_url=final_url, status_code=status,
                    content_type="text/html", title=title, html=html, clean_text=clean_text,
                    content_hash=content_hash, fetch_method="browser", from_cache=False
                )
            finally:
                page.close()
        except Exception as e:
            return CrawlResult(
                canonical_url=url, final_url=url, status_code=0,
                content_type="", title="", html="", clean_text="",
                content_hash="", fetch_method="browser", from_cache=False, error=str(e)
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
