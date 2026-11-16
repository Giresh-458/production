from __future__ import annotations

import threading
import urllib.error

import pytest
import requests

import core.http_client as http_client
import core.shared_crawl_manager as shared_crawler
import core.web_collection as web_collection
import core.recursive_collection as recursive_collection
from core.crawl_context import CrawlContext, current_crawl_context


def _http_error(status: int, *, retry_after: str | None = None) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    response.url = "https://example.org/research"
    if retry_after:
        response.headers["Retry-After"] = retry_after
    return requests.HTTPError(f"HTTP {status}", response=response)


def test_robots_missing_file_allows_crawl(monkeypatch):
    def missing(url, timeout=None):
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", missing)
    assert web_collection._robots_allowed(
        "https://example.org/", "https://example.org/research", cache={}
    ) is True


def test_robots_forbidden_file_denies_crawl(monkeypatch):
    def forbidden(url, timeout=None):
        raise urllib.error.HTTPError(url, 403, "forbidden", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    assert web_collection._robots_allowed(
        "https://example.org/", "https://example.org/research", cache={}
    ) is False


def test_robots_transport_failure_fails_closed(monkeypatch):
    def unavailable(url, timeout=None):
        raise urllib.error.URLError("connection failed")

    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    assert web_collection._robots_allowed(
        "https://example.org/", "https://example.org/research", cache={}
    ) is False


def test_expired_deadline_prevents_new_http_request():
    ctx = CrawlContext(deadline=0.001)
    token = current_crawl_context.set(ctx)
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            http_client._apply_context_timeout((8.0, 20.0))
    finally:
        current_crawl_context.reset(token)


def test_shared_crawler_checks_robots_before_cache():
    manager = shared_crawler.SharedCrawlManager.__new__(
        shared_crawler.SharedCrawlManager
    )
    manager.robots_cache = {}
    manager.lock = threading.Lock()
    manager.stats_unique_urls = 0
    manager._robots_allowed = lambda _url: False
    result = manager.fetch("https://example.org/research")
    assert result.error == "robots_disallow"
    assert result.fetch_method == "robots"


def test_shared_crawler_preserves_http_429_and_does_not_classify_it_as_js(monkeypatch):
    manager = shared_crawler.SharedCrawlManager.__new__(
        shared_crawler.SharedCrawlManager
    )
    monkeypatch.setattr(
        shared_crawler,
        "get_response",
        lambda *args, **kwargs: (_ for _ in ()).throw(_http_error(429, retry_after="7")),
    )
    result = manager._do_http_fetch("https://example.org/research")
    assert result.status_code == 429
    assert "Retry-After: 7" in result.error
    assert manager._needs_browser("", 429) is False


def test_web_collection_does_not_browser_fallback_after_http_429(monkeypatch):
    monkeypatch.setattr(web_collection, "_robots_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        web_collection,
        "get_response",
        lambda *args, **kwargs: (_ for _ in ()).throw(_http_error(429, retry_after="3")),
    )
    monkeypatch.setattr(
        web_collection,
        "_render_with_browser",
        lambda *args, **kwargs: pytest.fail("Browser fallback must not follow HTTP 429"),
    )

    result = web_collection.collect_source(
        "https://example.org/research", keywords=(), max_pages=1
    )
    assert result.pages == []
    assert any("http_429_retry_after:3" in item["reason"] for item in result.failed_urls)


def test_recursive_collection_skips_robots_disallowed_child(monkeypatch):
    monkeypatch.setattr(recursive_collection, "_sitemap_links", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        web_collection, "_robots_allowed", lambda *args, **kwargs: False
    )
    fetched: list[str] = []

    def fetch_html(url: str) -> str:
        fetched.append(url)
        return "<html><body>child content</body></html>"

    pages, review = recursive_collection.collect_recursive_pages_with_review(
        root_url="https://example.org/",
        root_name="example",
        initial_html='<html><body><a href="/funding">Funding</a></body></html>',
        fetch_html=fetch_html,
        extract_page_text=lambda html, **kwargs: ("Root", "Root content " * 50),
        config=recursive_collection.RecursiveCollectionConfig(
            max_depth=1,
            max_pages=3,
            allowed_domains=("example.org",),
            link_keywords=("funding",),
            relevance_threshold=1,
        ),
    )

    assert fetched == []
    assert len(pages) == 1
    assert any(
        item["reason"] == "robots_disallow"
        for item in review["skipped_links"]
    )


@pytest.mark.parametrize(
    ("module_name", "error_class_name"),
    [
        ("lab", "LabAgentError"),
        ("company", "CompanyAgentError"),
        ("regulation", "RegulationAgentError"),
        ("opensource", "OpenSourceAgentError"),
        ("practitioner", "PractitionerAgentError"),
        ("investment", "InvestmentAgentError"),
        ("failure", "FailureAgentError"),
        ("data_availability", "DataAvailabilityAgentError"),
        ("hackathon", "HackathonAgentError"),
        ("expert", "ExpertAgentError"),
    ],
)
def test_configured_agent_fetch_html_propagates_shared_crawler_error(
    monkeypatch, module_name, error_class_name
):
    import importlib
    from types import SimpleNamespace

    module = importlib.import_module(f"agents.{module_name}_agent")
    from core.shared_crawl_manager import SharedCrawlManager

    class FakeManager:
        def fetch(self, _url):
            return SimpleNamespace(
                error="robots_disallow",
                status_code=0,
                fetch_method="robots",
                html="",
            )

    monkeypatch.setattr(SharedCrawlManager, "get", staticmethod(lambda: FakeManager()))
    error_class = getattr(module, error_class_name)

    with pytest.raises(error_class, match="robots_disallow"):
        module.fetch_html("https://example.org/research")


def test_http_429_retries_using_retry_after(monkeypatch):
    from unittest.mock import Mock

    first = Mock()
    first.status_code = 429
    first.headers = {"Retry-After": "0"}
    first.content = b""
    first.url = "https://example.org/research"
    second = Mock()
    second.status_code = 200
    second.headers = {"Content-Type": "text/plain"}
    second.content = b"ok"
    second.text = "ok"
    second.url = "https://example.org/research"

    session = Mock()
    session.get.side_effect = [first, second]
    result = http_client.get_response(
        "https://example.org/research", session=session, retries=1
    )
    assert result.status_code == 200
    assert result.text == "ok"
    assert session.get.call_count == 2


def test_shared_crawler_rejects_redirect_to_robots_disallowed_path():
    from concurrent.futures import ThreadPoolExecutor

    manager = shared_crawler.SharedCrawlManager.__new__(
        shared_crawler.SharedCrawlManager
    )
    manager._get_domain_semaphore = lambda _url: threading.Semaphore(1)
    manager._throttle_domain = lambda _domain, deadline=None: True
    manager._robots_allowed = lambda target: not target.endswith("/private")
    manager._needs_browser = lambda _html, _status: False
    manager.http_pool = ThreadPoolExecutor(max_workers=1)
    manager._do_http_fetch = lambda url: shared_crawler.CrawlResult(
        canonical_url=url,
        final_url="https://example.org/private",
        status_code=200,
        content_type="text/html",
        title="Private",
        html="<p>private content</p>",
        clean_text="private content",
        content_hash="hash",
        fetch_method="http",
        from_cache=False,
    )
    try:
        result = manager._execute_fetch("https://example.org/public")
    finally:
        manager.http_pool.shutdown(wait=True)

    assert result.error == "robots_disallow_redirect"
    assert result.html == ""


def test_web_collection_rejects_redirect_to_robots_disallowed_path(monkeypatch):
    class Response:
        url = "https://example.org/private"
        text = "<html><body>Private content.</body></html>"
        content = text.encode()
        headers = {"Content-Type": "text/html"}

    monkeypatch.setattr(web_collection, "get_response", lambda *args, **kwargs: Response())
    monkeypatch.setattr(
        web_collection,
        "_robots_allowed",
        lambda _root, target, **kwargs: not target.endswith("/private"),
    )

    result = web_collection.collect_source(
        "https://example.org/public", keywords=(), max_pages=1
    )
    assert result.pages == []
    assert any(
        item["reason"] == "robots_disallow_redirect"
        for item in result.failed_urls
    )


def test_funding_agent_never_uses_browser_fallback_for_http_429(monkeypatch):
    import importlib

    funding_agent = importlib.import_module("agents.funding_agent")
    monkeypatch.setattr(
        web_collection, "_robots_allowed", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(
        http_client,
        "fetch_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(_http_error(429, retry_after="11")),
    )
    monkeypatch.setattr(
        web_collection,
        "render_page_with_browser",
        lambda *args, **kwargs: pytest.fail("HTTP 429 must not trigger browser fallback"),
    )

    with pytest.raises(funding_agent.FundingAgentError, match="HTTP 429"):
        funding_agent.fetch_html("https://example.org/research")


def test_robots_disallow_rule_is_respected(monkeypatch):
    class RobotsResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _limit):
            return b"User-agent: *\nDisallow: /private\n"

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: RobotsResponse())
    assert web_collection._robots_allowed(
        "https://example.org/", "https://example.org/private/call", cache={}
    ) is False


def test_funding_agent_does_not_browser_bypass_plain_http_403(monkeypatch):
    import importlib

    funding_agent = importlib.import_module("agents.funding_agent")
    denied = _http_error(403)
    denied.response._content = b"Access denied"
    monkeypatch.setattr(
        web_collection, "_robots_allowed", lambda *args, **kwargs: True
    )
    monkeypatch.setattr(
        http_client,
        "fetch_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(denied),
    )
    monkeypatch.setattr(
        web_collection,
        "render_page_with_browser",
        lambda *args, **kwargs: pytest.fail("Plain HTTP 403 must not trigger browser fallback"),
    )

    with pytest.raises(funding_agent.FundingAgentError, match="not a recognized JavaScript challenge"):
        funding_agent.fetch_html("https://example.org/research")


def test_shared_crawler_deadline_bounds_domain_semaphore_wait():
    import time

    manager = shared_crawler.SharedCrawlManager.__new__(
        shared_crawler.SharedCrawlManager
    )

    class BusySemaphore:
        def acquire(self, timeout=None):
            assert timeout is not None and timeout > 0
            return False

        def release(self):
            pytest.fail("A semaphore that was not acquired must not be released")

    manager._get_domain_semaphore = lambda _url: BusySemaphore()
    token = current_crawl_context.set(CrawlContext(deadline=time.time() + 1.0))
    try:
        result = manager._execute_fetch("https://example.org/research")
    finally:
        current_crawl_context.reset(token)

    assert result.error == "deadline_exceeded"


def test_sitemap_fetch_is_blocked_by_robots_and_reported(monkeypatch):
    errors: list[dict[str, str]] = []
    monkeypatch.setattr(
        web_collection,
        "_robots_allowed",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        web_collection,
        "get_response",
        lambda *args, **kwargs: pytest.fail("robots-disallowed sitemap must not be fetched"),
    )

    result = web_collection._sitemap_urls(
        "https://example.org/sitemap.xml",
        allowed_origin="https://example.org",
        robots_cache={},
        errors=errors,
    )

    assert result == []
    assert errors == [{
        "url": "https://example.org/sitemap.xml",
        "reason": "robots_disallow_sitemap",
    }]


def test_sitemap_fetch_failure_is_reported(monkeypatch):
    errors: list[dict[str, str]] = []
    monkeypatch.setattr(
        web_collection,
        "_robots_allowed",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        web_collection,
        "get_response",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            requests.Timeout("sitemap request timed out")
        ),
    )

    result = web_collection._sitemap_urls(
        "https://example.org/sitemap.xml",
        allowed_origin="https://example.org",
        robots_cache={},
        errors=errors,
    )

    assert result == []
    assert errors
    assert errors[0]["reason"].startswith("sitemap_fetch_failed:Timeout:")


def test_recursive_sitemap_is_not_fetched_when_robots_disallows_it(monkeypatch):
    import urllib.request

    class RobotsResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, _limit):
            return b"Sitemap: https://example.org/sitemap.xml\\n"

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: RobotsResponse(),
    )
    monkeypatch.setattr(
        web_collection,
        "_robots_allowed",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        http_client,
        "get_response",
        lambda *args, **kwargs: pytest.fail("robots-disallowed sitemap must not be fetched"),
    )
    errors: list[dict[str, str]] = []

    result = recursive_collection._sitemap_links(
        "https://example.org/",
        robots_cache={},
        errors=errors,
    )

    assert result == []
    assert errors == [{
        "url": "https://example.org/sitemap.xml",
        "reason": "robots_disallow_sitemap",
    }]
