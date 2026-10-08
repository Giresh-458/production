import pytest
from core.crawl_context import CrawlContext, current_crawl_context
from core.web_collection import collect_source
from core.shared_crawl_manager import SharedCrawlManager
import concurrent.futures

class MockResponse:
    def __init__(self, content, status_code=200, url="http://example.com"):
        self.content = content
        self.text = content.decode('utf-8')
        self.status_code = status_code
        self.url = url
        self.headers = {"Content-Type": "text/html"}
    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"{self.status_code} Error")

def mock_get_response(url, *args, **kwargs):
    if "js-required" in url:
        return MockResponse(b"<html><body>Please enable JavaScript to view this page.</body></html>", 200, url)
    elif "bot-challenge" in url:
        raise Exception("403 Forbidden - Cloudflare")
    return MockResponse(b"<html><body>Static content</body></html>", 200, url)

def mock_render_with_browser(url):
    if "fail-browser" in url:
        return None
    return ("Rendered Title", "<html><body>JS rendered content</body></html>", [], url)

@pytest.fixture(autouse=True)
def patch_network(monkeypatch):
    monkeypatch.setattr("core.web_collection.get_response", mock_get_response)
    monkeypatch.setattr("core.web_collection._render_with_browser", mock_render_with_browser)
    # Also patch for shared_crawl_manager
    monkeypatch.setattr("core.shared_crawl_manager.get_response", mock_get_response)
    
    # Mock Playwright installation check
    monkeypatch.setattr("core.shared_crawl_manager.HAS_PLAYWRIGHT", True)
    
    # Mock _do_browser_fetch to avoid launching real browsers in unit tests
    def mock_do_browser_fetch(self, url):
        from core.shared_crawl_manager import CrawlResult
        if "fail-browser" in url:
            return CrawlResult(
                canonical_url=url, final_url=url, status_code=0,
                content_type="", title="", html="", clean_text="",
                content_hash="", fetch_method="browser", from_cache=False, error="Browser crashed"
            )
        return CrawlResult(
            canonical_url=url, final_url=url, status_code=200,
            content_type="text/html", title="Rendered Title", html="<html><body>JS rendered content</body></html>", 
            clean_text="JS rendered content", content_hash="abc", fetch_method="browser", from_cache=False
        )
    monkeypatch.setattr("core.shared_crawl_manager.SharedCrawlManager._do_browser_fetch", mock_do_browser_fetch)

def test_web_collection_playwright_fallback():
    ctx = CrawlContext()
    token = current_crawl_context.set(ctx)
    
    try:
        # 1. Success case: JS required, browser works
        res = collect_source("http://example.com/js-required", keywords=())
        assert ctx.playwright_used == 1
        assert ctx.playwright_fallback_failed == 0
        assert "JS rendered content" in res.pages[0].text
        assert res.pages[0].is_playwright is True
        
        # 2. Failure case: challenge, browser fails
        res = collect_source("http://example.com/bot-challenge/fail-browser", keywords=())
        assert ctx.playwright_used == 2
        assert ctx.playwright_fallback_failed == 1
        assert len(res.pages) == 0
        assert len(res.failed_urls) > 0
        
    finally:
        current_crawl_context.reset(token)

def test_shared_crawl_manager_playwright_fallback():
    ctx = CrawlContext()
    token = current_crawl_context.set(ctx)
    manager = SharedCrawlManager.get()
    manager.run_cache.clear()
    
    try:
        # Success fallback
        res = manager.fetch("http://example.com/js-required", bypass_cache=True)
        print("RES METHOD:", res.fetch_method)
        print("RES ERROR:", res.error)
        print("PLAYWRIGHT USED:", ctx.playwright_used)
        assert res.fetch_method == "browser"
        assert ctx.playwright_used == 1
        assert ctx.playwright_fallback_failed == 0
        
        # Failed fallback
        res2 = manager.fetch("http://example.com/bot-challenge/fail-browser", bypass_cache=True)
        assert res2.error == "Browser crashed"
        assert ctx.playwright_used == 2
        assert ctx.playwright_fallback_failed == 1
    finally:
        current_crawl_context.reset(token)

def test_playwright_multiple_worker_threads():
    # Prove that multiple worker threads can execute independently
    manager = SharedCrawlManager.get()
    manager.run_cache.clear()
    
    urls = [f"http://example.com/js-required/{i}" for i in range(10)]
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(manager.fetch, urls))
        
    for res in results:
        assert res.fetch_method == "browser"
        assert "JS rendered" in res.html
