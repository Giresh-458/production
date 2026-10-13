"""Policy and pacing tests for the public-web funding crawler."""
from unittest.mock import MagicMock

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
    assert sleeps == [0.2]
    assert crawl4ai_web._HOST_LAST_REQUEST["example.test"] == 100.8
