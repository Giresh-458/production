import pytest
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



def _failed_crawl_result(message):
    return SimpleNamespace(
        success=False, html="", markdown_v2=SimpleNamespace(raw_markdown=""),
        url="https://www.sbir.gov/topics?page=19", redirected_url=None,
        error_message=message, status_code=None, redirected_status_code=None,
    )


# Verbatim shape returned (not raised) by Crawl4AI 0.9.5 arun() in the live
# Batch-1 scan for sbir.gov listing pages 19-20.
CRAWL4AI_RETURNED_TIMEOUT = (
    "Unexpected error in _crawl_web at line 779 in _crawl_web "
    "(.venv/Lib/site-packages/crawl4ai/async_crawler_strategy.py):\n"
    "Error: Failed on navigating ACS-GOTO:\nPage.goto: Timeout 30000ms exceeded.\n"
    "Call log:\n  - navigating to \"https://www.sbir.gov/topics?page=19\", waiting until \"domcontentloaded\"\n"
)


def test_returned_navigation_timeout_result_triggers_bounded_commit_retry():
    recovered = SimpleNamespace(
        success=True,
        html="<html><head><title>Topics</title></head><body>Open topics</body></html>",
        markdown_v2=SimpleNamespace(raw_markdown="Open topics"),
        url="https://www.sbir.gov/topics?page=19", redirected_url=None,
        error_message=None, status_code=200, redirected_status_code=200,
    )
    crawler_instance = MagicMock()
    crawler_instance.arun = AsyncMock(side_effect=[
        _failed_crawl_result(CRAWL4AI_RETURNED_TIMEOUT), recovered,
    ])
    with patch("core.crawl4ai_web.AsyncWebCrawler") as mock_crawler:
        mock_crawler.return_value.__aenter__.return_value = crawler_instance
        with patch("core.crawl4ai_web.CrawlerRunConfig") as mock_config:
            result = _fetch_sync("https://www.sbir.gov/topics?page=19")

    assert result["success"] is True
    assert crawler_instance.arun.await_count == 2
    assert mock_config.call_args_list[1].kwargs["wait_until"] == "commit"


def test_returned_non_navigation_failure_is_not_retried():
    crawler_instance = MagicMock()
    crawler_instance.arun = AsyncMock(return_value=_failed_crawl_result(
        "Unexpected error in _crawl_web: Error: Failed on navigating ACS-GOTO:\n"
        "Page.goto: net::ERR_CERT_AUTHORITY_INVALID at https://www.sbir.gov/topics"
    ))
    with patch("core.crawl4ai_web.AsyncWebCrawler") as mock_crawler:
        mock_crawler.return_value.__aenter__.return_value = crawler_instance
        with patch("core.crawl4ai_web.CrawlerRunConfig"):
            result = _fetch_sync("https://www.sbir.gov/topics?page=19")

    assert result["success"] is False
    assert crawler_instance.arun.await_count == 1


def test_returned_timeout_that_persists_after_retry_stays_failed():
    crawler_instance = MagicMock()
    crawler_instance.arun = AsyncMock(side_effect=[
        _failed_crawl_result(CRAWL4AI_RETURNED_TIMEOUT),
        _failed_crawl_result(CRAWL4AI_RETURNED_TIMEOUT),
    ])
    with patch("core.crawl4ai_web.AsyncWebCrawler") as mock_crawler:
        mock_crawler.return_value.__aenter__.return_value = crawler_instance
        with patch("core.crawl4ai_web.CrawlerRunConfig"):
            result = _fetch_sync("https://www.sbir.gov/topics?page=19")

    assert result["success"] is False
    assert crawler_instance.arun.await_count == 2  # bounded: exactly one retry


@pytest.mark.parametrize("net_error", [
    # Verbatim error codes observed in live Batch-1 scans.
    "net::ERR_NAME_NOT_RESOLVED", "net::ERR_ADDRESS_UNREACHABLE",
    "net::ERR_NETWORK_CHANGED", "net::ERR_CONNECTION_ABORTED",
])
def test_transient_network_navigation_error_gets_one_paused_retry(net_error):
    message = (
        "Unexpected error in _crawl_web at line 779 in _crawl_web:\n"
        f"Error: Failed on navigating ACS-GOTO:\nPage.goto: {net_error} at https://www.sbir.gov/topics/13162\n"
    )
    recovered = SimpleNamespace(
        success=True, html="<html><head><title>Topic</title></head><body>ok</body></html>",
        markdown_v2=SimpleNamespace(raw_markdown="ok"), url="https://www.sbir.gov/topics/13162",
        redirected_url=None, error_message=None, status_code=200, redirected_status_code=200,
    )
    crawler_instance = MagicMock()
    crawler_instance.arun = AsyncMock(side_effect=[_failed_crawl_result(message), recovered])
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    with patch("core.crawl4ai_web.AsyncWebCrawler") as mock_crawler, \
            patch("core.crawl4ai_web.asyncio.sleep", fake_sleep):
        mock_crawler.return_value.__aenter__.return_value = crawler_instance
        with patch("core.crawl4ai_web.CrawlerRunConfig"):
            result = _fetch_sync("https://www.sbir.gov/topics/13162")

    assert result["success"] is True
    assert crawler_instance.arun.await_count == 2
    assert sleeps == [2.0]


def test_certificate_errors_are_not_classified_as_transient():
    from core.crawl4ai_web import _is_retryable_navigation_error

    assert not _is_retryable_navigation_error("Page.goto: net::ERR_CERT_AUTHORITY_INVALID at https://x")
    assert not _is_retryable_navigation_error("HTTP 403 Forbidden")
    assert _is_retryable_navigation_error("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://x")
