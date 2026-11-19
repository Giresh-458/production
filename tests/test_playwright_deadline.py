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

def test_playwright_navigation_timeout_retries_once_with_commit_wait():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_result = SimpleNamespace(
        success=True,
        html="<html><head><title>Recovered page</title></head><body>Verified content</body></html>",
        markdown_v2=SimpleNamespace(raw_markdown="Verified content"),
        url="https://example.com/slow",
        redirected_url=None,
        error_message=None,
        status_code=200,
        redirected_status_code=200,
    )
    crawler_instance = MagicMock()
    crawler_instance.arun = AsyncMock(side_effect=[
        RuntimeError(
            "Failed on navigating ACS-GOTO: Page.goto: Timeout 30000ms exceeded"
        ),
        mock_result,
    ])

    with patch("core.crawl4ai_web.AsyncWebCrawler") as mock_crawler:
        mock_crawler.return_value.__aenter__.return_value = crawler_instance
        with patch("core.crawl4ai_web.CrawlerRunConfig") as mock_config:
            from core.crawl4ai_web import _fetch_sync

            result = _fetch_sync("https://example.com/slow")

    assert result["success"] is True
    assert result["title"] == "Recovered page"
    assert crawler_instance.arun.await_count == 2
    assert mock_config.call_count == 2
    assert mock_config.call_args_list[1].kwargs["wait_until"] == "commit"
    assert mock_config.call_args_list[1].kwargs["page_timeout"] == 20000


def test_playwright_non_navigation_errors_are_not_retried():
    from unittest.mock import AsyncMock, MagicMock, patch

    crawler_instance = MagicMock()
    crawler_instance.arun = AsyncMock(
        side_effect=RuntimeError("robots policy denied this URL")
    )

    with patch("core.crawl4ai_web.AsyncWebCrawler") as mock_crawler:
        mock_crawler.return_value.__aenter__.return_value = crawler_instance
        with patch("core.crawl4ai_web.CrawlerRunConfig") as mock_config:
            from core.crawl4ai_web import _fetch_sync
            import pytest

            with pytest.raises(RuntimeError, match="robots policy denied"):
                _fetch_sync("https://example.com/blocked")

    assert crawler_instance.arun.await_count == 1
    assert mock_config.call_count == 1

