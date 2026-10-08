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
def test_collect_auto_sources_separates_production_source_limits(monkeypatch):
    from types import SimpleNamespace
    import agents.funding_collector as fc

    captured = {}
    sentinel = object()

    def fake_collect_api_source(
        source,
        area,
        warnings,
        test_limits=None,
        source_limits=None,
    ):
        captured["test_limits"] = test_limits
        captured["source_limits"] = source_limits
        return sentinel

    monkeypatch.setattr(fc, "_collect_api_source", fake_collect_api_source)
    source = SimpleNamespace(
        name="Grants.gov Public Opportunities API",
        url="https://api.grants.gov/v1/api/search2",
        collection={
            "mode": "api",
            "provider": "grants_gov",
            "limits": {"max_pages": 2, "max_documents": 100},
        },
    )

    result = next(fc.collect_auto_sources([source], is_test_mode=False))
    assert result is sentinel
    assert captured["test_limits"] is None
    assert captured["source_limits"] == {"max_pages": 2, "max_documents": 100}
