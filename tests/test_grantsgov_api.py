"""Tests for Grants.gov API integration."""
import pytest
from core.structured_sources import grantsgov_paginated_search, collect_structured_source


def test_grantsgov_search_success(monkeypatch):
    monkeypatch.setattr(
        "core.structured_sources.post_json",
        lambda *a, **k: (
            {"data": {"hitCount": 1, "oppHits": [
                {"title": "Fake Grant", "number": "123", "agencyName": "Test",
                 "openDate": "2026-01-01", "closeDate": "2026-12-31",
                 "oppStatus": "posted", "id": "999"}
            ]}},
            "https://api.grants.gov/v1/api/search2",
        ),
    )

    result = grantsgov_paginated_search("research")
    assert result["pagination_complete"] is True
    assert len(result["hits"]) == 1
    assert result["hits"][0]["title"] == "Fake Grant"
    assert result["total_expected"] == 1


def test_grantsgov_search_zero_results(monkeypatch):
    monkeypatch.setattr(
        "core.structured_sources.post_json",
        lambda *a, **k: ({"data": {"hitCount": 0, "oppHits": []}}, "url"),
    )

    result = grantsgov_paginated_search("weird_keyword_nobody_searches")
    assert len(result["hits"]) == 0
    assert result["pagination_complete"] is True


def test_grantsgov_search_timeout(monkeypatch):
    def fake_post(*a, **k):
        raise TimeoutError("timeout")
    monkeypatch.setattr("core.structured_sources.post_json", fake_post)

    result = grantsgov_paginated_search("research")
    assert len(result["hits"]) == 0
    assert result["pagination_complete"] is False
    assert len(result["errors"]) == 1


def test_collect_structured_source_integration(monkeypatch):
    """collect_structured_source returns None for Grants.gov (handled natively)."""
    result = collect_structured_source("https://api.grants.gov/v1/api/search2")
    assert result is None
