import pytest
from core.crawl4ai_web import crawl_funding_source
from core.crawl_context import CrawlContext, current_crawl_context

def test_funding_boundaries_respects_limits(monkeypatch):
    # Mock queue logic is not needed, we'll just return many links from fake_fetch_sync
    
    # Mock fetching
    def fake_fetch_sync(url):
        print("FETCHING:", url)
        return {
            "success": True,
            "markdown": "Here is a funding opportunity. Grant! Apply now! Deadline!",
            "html": "<html><body><a href='/child'>Child</a></body></html>",
            "title": "Funding Call",
            "links": [{"url": f"https://test.org/child{i}", "text": f"Child {i}"} for i in range(10)]
        }
        
    monkeypatch.setattr("core.crawl4ai_web._fetch_sync", fake_fetch_sync)
    monkeypatch.setattr("core.crawl4ai_web._is_allowed_domain", lambda *a: True)
    monkeypatch.setattr("core.crawl4ai_web._classify_link_stage_a", lambda *a, **k: 1.0)
    
    ctx = CrawlContext()
    token = current_crawl_context.set(ctx)
    try:
        # Pass test_limits to ensure it respects them
        res = crawl_funding_source("Test", "https://test.org", discovery_config={}, allowed_domains=["test.org"], test_limits={"max_pages": 4})
        
        # Should break out of the infinite queue after 4 pages
        assert res.truncated == True
        assert res.pages_fetched <= 4
    finally:
        current_crawl_context.reset(token)
