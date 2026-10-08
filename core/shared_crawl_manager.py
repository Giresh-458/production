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

    def _throttle_domain(self, domain: str):
        sleep_time = 0
        with self.lock:
            last = self.domain_last_fetch.get(domain, 0.0)
            now = time.time()
            if now - last < 1.0: # 1 second delay between requests to same domain
                sleep_time = 1.0 - (now - last)
                self.domain_last_fetch[domain] = now + sleep_time
            else:
                self.domain_last_fetch[domain] = now
                
        if sleep_time > 0:
            time.sleep(sleep_time)

    def fetch(self, url: str, bypass_cache: bool = False) -> CrawlResult:
        from core.source_registry import current_force_source_refresh
        canonical = canonicalize_url(url)
        
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

    def _execute_fetch(self, url: str) -> CrawlResult:
        import contextvars
        import time
        from concurrent.futures import TimeoutError as FuturesTimeoutError
        from core.crawl_context import current_crawl_context
        
        domain = urlparse(url).netloc.lower()
        sem = self._get_domain_semaphore(url)
        
        with sem:
            self._throttle_domain(domain)
            
            ctx_obj = current_crawl_context.get(None)
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
            
            # Check if JS fallback is needed
            if result.error or self._needs_browser(result.html, result.status_code):
                if HAS_PLAYWRIGHT:
                    LOGGER.info(f"Falling back to browser fetch for {url}")
                    if ctx_obj:
                        with ctx_obj.lock: ctx_obj.browser_attempts += 1
                        with ctx_obj.lock: ctx_obj.playwright_used += 1
                        with ctx_obj.lock: ctx_obj.js_shells_detected += 1 if not result.error else 0
                        with ctx_obj.lock: ctx_obj.browser_escalations += 1

                    if ctx_obj and ctx_obj.deadline:
                        timeout = max(0.1, ctx_obj.deadline - time.time())

                    ctx2 = contextvars.copy_context()
                    browser_future = self.browser_pool.submit(ctx2.run, self._do_browser_fetch, url)
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
            
            return result

    def _needs_browser(self, html: str, status_code: int) -> bool:
        if status_code in (403, 429, 503):
            return True
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
        except Exception as e:
            return CrawlResult(
                canonical_url=url, final_url=url, status_code=0,
                content_type="", title="", html="", clean_text="",
                content_hash="", fetch_method="http", from_cache=False, error=str(e)
            )

    def _do_browser_fetch(self, url: str) -> CrawlResult:
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
                response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # wait a moment for JS to render
                page.wait_for_timeout(2000)
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
