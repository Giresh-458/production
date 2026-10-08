import pytest
import time
from unittest.mock import patch, MagicMock
from core.crawl4ai_web import _fetch_sync
from core.crawl_context import CrawlContext, current_crawl_context

def test_playwright_deadline_enforcement():
    # Setup context with a strict deadline in the past
    ctx = CrawlContext()
    ctx.deadline = time.time() - 10.0 # 10 seconds ago!
    token = current_crawl_context.set(ctx)
    
    try:
        # We mock AsyncWebCrawler so we don't really spin up browsers
        with patch('core.crawl4ai_web.AsyncWebCrawler') as MockCrawler:
            from unittest.mock import AsyncMock
            mock_crawler_instance = MagicMock()
            mock_crawler_instance.arun = AsyncMock()
            MockCrawler.return_value.__aenter__.return_value = mock_crawler_instance
            
            # Record the arguments passed to CrawlerRunConfig
            with patch('core.crawl4ai_web.CrawlerRunConfig') as MockConfig:
                results = _fetch_sync("http://example.com/slow")
                
                # Verify that max(5.0, ctx.deadline - time.time()) is used
                # Since deadline is in the past, it should clamp to 5.0 seconds
                
                MockConfig.assert_called_once()
                kwargs = MockConfig.call_args.kwargs
                assert kwargs['page_timeout'] == 5000, f"Expected page_timeout=5000ms, got {kwargs['page_timeout']}"
    finally:
        current_crawl_context.reset(token)
