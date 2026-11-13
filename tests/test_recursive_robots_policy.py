import pytest

from core import recursive_collection


def test_recursive_children_are_checked_before_fetch(monkeypatch):
    requested = []
    monkeypatch.setattr(recursive_collection, "_sitemap_links", lambda *a, **k: [])
    monkeypatch.setattr(recursive_collection, "wait_for_host_slot", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        recursive_collection,
        "check_robots",
        lambda url, **kwargs: (
            (False, "robots disallow", 0.0)
            if url.endswith("/blocked")
            else (True, None, 0.0)
        ),
    )
    monkeypatch.setattr(
        recursive_collection,
        "extract_ranked_links",
        lambda **kwargs: [
            ("https://example.test/blocked", 10, "blocked"),
            ("https://example.test/allowed", 9, "allowed"),
        ],
    )

    def fake_fetch_recursive_page(url, fetch_html, extract_page_text):
        requested.append(url)
        return (
            "Child",
            "This is enough source-backed content for a deterministic crawler test.",
            "<html><body>Allowed child content.</body></html>",
        )

    monkeypatch.setattr(
        recursive_collection,
        "_fetch_recursive_page",
        fake_fetch_recursive_page,
    )

    def extract_text(html, **kwargs):
        if "root" in html:
            return "Root", "Unique root-page text for deterministic content deduplication."
        return "Child", "This is enough source-backed content for a deterministic crawler test."

    from core.crawl_context import current_crawl_context
    context_token = current_crawl_context.set(None)
    try:
        pages, review = recursive_collection.collect_recursive_pages_with_review(
            root_url="https://example.test/",
        root_name="test",
        initial_html="<html><body>root</body></html>",
        fetch_html=lambda url: "",
        extract_page_text=extract_text,
        config=recursive_collection.RecursiveCollectionConfig(
            max_depth=2,
            max_pages=5,
            allowed_domains={"example.test"},
            relevance_threshold=0,
            ),
        )
    finally:
        current_crawl_context.reset(context_token)

    assert requested == ["https://example.test/allowed"]
    assert [page.url for page in pages] == [
        "https://example.test/",
        "https://example.test/allowed",
    ]
    assert any(
        item["url"] == "https://example.test/blocked"
        and item["reason"].startswith("robots_disallow")
        for item in review["skipped_links"]
    )


def test_recursive_redirect_checks_robots_before_requesting_target(monkeypatch):
    calls = []

    class RedirectResponse:
        status_code = 302
        headers = {"Location": "/private"}
        def close(self):
            pass

    def fake_get_response(url, **kwargs):
        calls.append((url, kwargs.get("allow_redirects")))
        return RedirectResponse()

    monkeypatch.setattr("core.http_client.get_response", fake_get_response)
    monkeypatch.setattr(recursive_collection, "wait_for_host_slot", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        recursive_collection,
        "check_robots",
        lambda url, **kwargs: (
            (False, "robots disallow", 0.0)
            if url.endswith("/private")
            else (True, None, 0.0)
        ),
    )

    try:
        recursive_collection._get_response_with_robots(
            "https://example.test/start",
            allowed_domains={"example.test"},
        )
    except PermissionError as exc:
        assert "robots disallow" in str(exc)
    else:
        raise AssertionError("Redirect to a disallowed path must be blocked")

    assert calls == [("https://example.test/start", False)]


def test_sitemap_discovery_stops_when_root_is_disallowed(monkeypatch):
    monkeypatch.setattr(recursive_collection, "wait_for_host_slot", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        recursive_collection,
        "check_robots",
        lambda *args, **kwargs: (False, "robots disallow", 0.0),
    )
    calls = []
    monkeypatch.setattr(
        "core.http_client.get_response",
        lambda *args, **kwargs: calls.append(args[0]),
    )

    assert recursive_collection._sitemap_links("https://example.test/") == []
    assert calls == []

def test_recursive_crawler_rejects_private_and_local_urls():
    assert recursive_collection._is_crawlable_url("https://example.test/public") is True
    assert recursive_collection._is_crawlable_url("http://127.0.0.1/private") is False
    assert recursive_collection._is_crawlable_url("http://169.254.169.254/latest/meta-data/") is False
    assert recursive_collection._is_crawlable_url("http://localhost/private") is False

def test_recursive_pdf_extraction_failure_is_reported_and_response_closed(monkeypatch):
    class FakeResponse:
        content = b"not a valid PDF"
        closed = False

        def close(self):
            self.closed = True

    response = FakeResponse()
    monkeypatch.setattr(
        recursive_collection,
        "_get_response_with_robots",
        lambda *args, **kwargs: response,
    )

    with pytest.raises(ValueError, match="pdf_extraction_failed"):
        recursive_collection._fetch_recursive_page(
            "https://example.test/report.pdf",
            fetch_html=lambda url: "",
            extract_page_text=lambda html: ("", ""),
        )

    assert response.closed is True

