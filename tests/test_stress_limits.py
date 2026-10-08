import pytest
import time
from core.crawl_context import CrawlContext, current_crawl_context
from core.recursive_collection import collect_recursive_pages_with_review, RecursiveCollectionConfig
from datetime import datetime, UTC

def test_recursive_stress_timeout(monkeypatch):
    """
    Simulates an extremely large URL frontier, slow fetches, and deadline expiration 
    while workers (the fetch function) are active.
    """
    def mock_get_response(url, *args, **kwargs):
        class MockResponse:
            text = ""
            status_code = 404
            def raise_for_status(self): pass
        return MockResponse()

    import urllib.request
    def mock_urlopen(url, *args, **kwargs):
        raise Exception("Mocked out")
    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    monkeypatch.setattr("core.http_client.get_response", mock_get_response)

    ctx = CrawlContext(deadline=time.time() + 2.0)  # 2 second deadline
    token = current_crawl_context.set(ctx)
    
    def slow_infinite_fetch_html(url: str) -> str:
        # Simulate slow network response
        time.sleep(0.5)
        import uuid
        unique = str(uuid.uuid4())
        links = "\n".join(f'<a href="http://example.com/page{i}">Link {i}</a>' for i in range(100))
        return f"<html><body><p>Unique content {unique}</p>{links}</body></html>"
    
    def dummy_extract(html: str):
        import uuid
        uid = str(uuid.uuid4()) * 100
        return uid, uid
    
    config = RecursiveCollectionConfig(
        max_depth=5,
        max_pages=1000,
        allowed_domains={"example.com"},
        relevance_threshold=0
    )
    
    start = time.time()
    try:
        pages, review = collect_recursive_pages_with_review(
            root_url="http://example.com/root",
            root_name="Stress Root",
            initial_html="<html><body><a href='http://example.com/page0'>Link 0</a></body></html>",
            fetch_html=slow_infinite_fetch_html,
            extract_page_text=dummy_extract,
            config=config
        )
    finally:
        current_crawl_context.reset(token)
        
    duration = time.time() - start
    
    # Assertions
    assert duration < 5.0, f"Crawl loop failed to respect deadline! Took {duration}s"
    # It should have crawled at least a few pages before stopping
    assert 2 <= len(pages) < 10
    # It should record deadline as stop reason
    assert "deadline" in ctx.stop_reasons
