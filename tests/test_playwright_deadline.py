import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.crawl4ai_web import _fetch_sync
from core.crawl_context import CrawlContext, current_crawl_context


def test_playwright_deadline_enforcement():
    # Setup context with a strict deadline in the past.
    ctx = CrawlContext()
    ctx.deadline = time.time() - 10.0
    token = current_crawl_context.set(ctx)

    try:
        # Mock the crawler with a concrete result so its attributes do not
        # become additional AsyncMock instances.
        with patch("core.crawl4ai_web.AsyncWebCrawler") as mock_crawler:
            mock_result = SimpleNamespace(
                success=True,
                html="",
                markdown_v2=SimpleNamespace(raw_markdown=""),
                url="http://example.com/slow",
                redirected_url=None,
                error_message=None,
                status_code=200,
                redirected_status_code=200,
            )
            mock_crawler_instance = MagicMock()
            mock_crawler_instance.arun = AsyncMock(return_value=mock_result)
            mock_crawler.return_value.__aenter__.return_value = mock_crawler_instance

            with patch("core.crawl4ai_web.CrawlerRunConfig") as mock_config:
                results = _fetch_sync("http://example.com/slow")

                # A past deadline is clamped to the 5-second minimum.
                mock_config.assert_called_once()
                assert mock_config.call_args.kwargs["page_timeout"] == 5000
                mock_crawler_instance.arun.assert_awaited_once()
                mock_crawler.return_value.__aenter__.assert_awaited_once()
                mock_crawler.return_value.__aexit__.assert_awaited_once()
                assert results["success"] is True
    finally:
        current_crawl_context.reset(token)
