"""Policy and pacing tests for the public-web funding crawler."""
from unittest.mock import MagicMock

import pytest

from core import crawl4ai_web


def _clear_caches():
    with crawl4ai_web._ROBOTS_LOCK:
        crawl4ai_web._ROBOTS_CACHE.clear()
    with crawl4ai_web._HOST_LOCK:
        crawl4ai_web._HOST_LAST_REQUEST.clear()


def test_robots_disallow_is_respected_and_crawl_delay_is_read(monkeypatch):
    _clear_caches()
    response = MagicMock()
    response.status_code = 200
    response.text = (
        "User-agent: *\n"
        "Disallow: /private/\n"
        "Crawl-delay: 2\n"
    )
    monkeypatch.setattr("core.crawl4ai_web.get_response", lambda *a, **k: response)

    allowed, delay, reason = crawl4ai_web._get_robots_policy(
        "https://example.test/private/call"
    )

    assert allowed is False
    assert delay == 2.0
    assert "disallows" in reason


def test_missing_robots_file_allows_public_page_with_default_delay(monkeypatch):
    _clear_caches()
    response = MagicMock()
    response.status_code = 404
    response.text = ""
    monkeypatch.setattr("core.crawl4ai_web.get_response", lambda *a, **k: response)

    allowed, delay, reason = crawl4ai_web._get_robots_policy(
        "https://example.test/public/call"
    )

    assert allowed is True
    assert delay >= 1.0
    assert reason is None


def test_robots_network_failure_fails_closed(monkeypatch):
    _clear_caches()

    def fail(*args, **kwargs):
        raise TimeoutError("robots unavailable")

    monkeypatch.setattr("core.crawl4ai_web.get_response", fail)
    allowed, delay, reason = crawl4ai_web._get_robots_policy(
        "https://example.test/public/call"
    )

    assert allowed is False
    assert delay >= 1.0
    assert "could not verify robots.txt" in reason


def test_same_host_request_waits_for_minimum_delay(monkeypatch):
    _clear_caches()
    times = iter([100.0, 100.8, 100.8])
    monkeypatch.setattr("core.crawl4ai_web.time.monotonic", lambda: next(times))
    sleeps = []
    monkeypatch.setattr("core.crawl4ai_web.time.sleep", lambda seconds: sleeps.append(seconds))

    with crawl4ai_web._HOST_LOCK:
        crawl4ai_web._HOST_LAST_REQUEST["example.test"] = 99.8

    allowed = crawl4ai_web._wait_for_host_slot("https://example.test/page", 1.0)

    assert allowed is True
    assert sleeps == [pytest.approx(0.8)]
    assert crawl4ai_web._HOST_LAST_REQUEST["example.test"] == 100.8


def test_unverified_robots_policy_is_recorded_as_partial_coverage_not_budget_truncation(monkeypatch):
    _clear_caches()
    monkeypatch.setattr(
        "core.crawl4ai_web._get_robots_policy",
        lambda url: (False, 1.0, "could not verify robots.txt policy (403)"),
    )
    monkeypatch.setattr("core.crawl4ai_web._wait_for_host_slot", lambda *args, **kwargs: True)

    result = crawl4ai_web.crawl_funding_source(
        source_id="Policy-limited source",
        url="https://example.test/call",
        discovery_config={
            "start_urls": ["https://example.test/call"],
            "max_depth": 1,
            "max_pages": 5,
            "follow_detail_pages": True,
            "follow_pdfs": True,
            "pagination": False,
        },
        allowed_domains=[],
    )

    assert result.truncated is False
    assert result.final_status == "FAIL"  # No page was fetched; never claim verified coverage.
    assert result.relevant_urls_skipped == 1
    assert result.coverage_gaps == [{
        "url": "https://example.test/call",
        "parent_url": None,
        "reason": "robots_policy_unverified",
        "detail": "could not verify robots.txt policy (403)",
    }]
    assert result.errors[0]["operation"] == "robots_txt_policy"



@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 410])
def test_robots_client_error_other_than_rate_limit_means_policy_unavailable(monkeypatch, status_code):
    """RFC 9309 treats a 4xx robots response as unavailable, not as a disallow rule."""
    _clear_caches()
    response = MagicMock()
    response.status_code = status_code
    response.text = ""
    monkeypatch.setattr("core.crawl4ai_web.get_response", lambda *a, **k: response)

    allowed, delay, reason = crawl4ai_web._get_robots_policy(
        "https://example.test/public/call"
    )

    assert allowed is True
    assert delay >= 1.0
    assert reason is None


def test_robots_rate_limit_fails_closed(monkeypatch):
    _clear_caches()
    response = MagicMock()
    response.status_code = 429
    response.text = "rate limited"
    response.headers = {}
    monkeypatch.setattr("core.crawl4ai_web.get_response", lambda *a, **k: response)

    allowed, delay, reason = crawl4ai_web._get_robots_policy(
        "https://example.test/public/call"
    )

    assert allowed is False
    assert delay >= 1.0
    assert "could not verify robots.txt policy" in reason
    assert "429" in reason


def test_robots_server_error_fails_closed(monkeypatch):
    _clear_caches()
    response = MagicMock()
    response.status_code = 503
    response.text = "temporarily unavailable"
    monkeypatch.setattr("core.crawl4ai_web.get_response", lambda *a, **k: response)

    allowed, delay, reason = crawl4ai_web._get_robots_policy(
        "https://example.test/public/call"
    )

    assert allowed is False
    assert delay >= 1.0
    assert "could not verify robots.txt policy" in reason

def test_browser_download_error_falls_back_to_direct_pdf_fetch(monkeypatch):
    _clear_caches()
    url = "https://www.climate-kic.org/download/72240/?tmstv=1761558226"
    monkeypatch.setattr(
        "core.crawl4ai_web._get_robots_policy",
        lambda candidate: (True, 0.0, None),
    )
    monkeypatch.setattr(
        "core.crawl4ai_web._wait_for_host_slot",
        lambda *args, **kwargs: True,
    )

    def raise_download_start(candidate):
        raise RuntimeError("Page.goto: Download is starting")

    monkeypatch.setattr("core.crawl4ai_web._fetch_sync", raise_download_start)
    monkeypatch.setattr(
        "core.crawl4ai_web._fetch_pdf",
        lambda candidate, max_pdf_pages=None: {
            "success": True,
            "content": "Verified PDF text",
            "total_pages": 1,
            "pages_processed": 1,
            "truncated": False,
        },
    )

    result = crawl4ai_web.crawl_funding_source(
        source_id="Climate KIC Open Calls",
        url=url,
        discovery_config={
            "start_urls": [url],
            "max_depth": 1,
            "max_pages": 5,
            "follow_detail_pages": True,
            "follow_pdfs": True,
            "pagination": False,
        },
        allowed_domains=[],
    )

    assert result.pages_discovered == 0
    assert result.pages_failed == 0
    assert result.documents_discovered == 1
    assert result.documents_fetched == 1
    assert result.documents_failed == 0
    assert result.failed_urls == []
    assert result.opportunities[0]["type"] == "pdf"
    assert result.opportunities[0]["content"] == "Verified PDF text"
