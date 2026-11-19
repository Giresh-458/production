from __future__ import annotations

import requests

from core import recursive_collection, web_collection


def test_web_sitemap_fetch_failure_is_reported(monkeypatch):
    monkeypatch.setattr(
        web_collection,
        "check_robots",
        lambda *args, **kwargs: (True, None, 0.0),
    )
    monkeypatch.setattr(
        web_collection,
        "wait_for_host_slot",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        web_collection,
        "get_response",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            requests.Timeout("sitemap request timed out")
        ),
    )
    errors: list[dict[str, str]] = []

    result = web_collection._sitemap_urls(
        "https://example.test/sitemap.xml",
        allowed_origin="https://example.test",
        errors=errors,
    )

    assert result == []
    assert errors
    assert errors[0]["url"] == "https://example.test/sitemap.xml"
    assert errors[0]["reason"].startswith("sitemap_fetch_failed:Timeout:")


def test_recursive_sitemap_fetch_failure_is_reported(monkeypatch):
    monkeypatch.setattr(
        recursive_collection,
        "check_robots",
        lambda *args, **kwargs: (True, None, 0.0),
    )
    monkeypatch.setattr(
        recursive_collection,
        "get_robots_sitemaps",
        lambda *args, **kwargs: ["https://example.test/sitemap.xml"],
    )
    monkeypatch.setattr(
        recursive_collection,
        "_get_response_with_robots",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            requests.Timeout("recursive sitemap request timed out")
        ),
    )
    errors: list[dict[str, str]] = []

    result = recursive_collection._sitemap_links(
        "https://example.test/",
        errors=errors,
    )

    assert result == []
    assert errors
    assert errors[0]["url"] == "https://example.test/sitemap.xml"
    assert errors[0]["reason"].startswith("sitemap_fetch_failed:Timeout:")
