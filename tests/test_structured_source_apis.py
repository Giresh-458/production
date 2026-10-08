from __future__ import annotations
from core import structured_sources as ss
from core.crawl_context import current_crawl_context, CrawlContext
import pytest

@pytest.fixture(autouse=True)
def clean_crawl_context():
    token = current_crawl_context.set(CrawlContext(deadline=None))
    yield
    current_crawl_context.reset(token)

def test_sec_structured_source_uses_public_json(monkeypatch):
    def fake_fetch(url, **kwargs):
        assert url.endswith("/files/company_tickers.json")
        return ({"0":{"title":"Example Corp","ticker":"EX","cik_str":123}}, url)
    monkeypatch.setattr(ss, "fetch_json", fake_fetch)
    result = ss._sec("https://www.sec.gov/")
    assert result and "Example Corp" in result.text and result.metadata["authenticated"] is False

def test_zenodo_structured_source_uses_record_api(monkeypatch):
    def fake_fetch(url, **kwargs):
        assert url.endswith("/api/records/19965384")
        return ({"metadata":{"title":"Dataset","publication_date":"2026-01-01","description":"desc"},"links":{"self":"https://zenodo.org/api/records/19965384"}}, url)
    monkeypatch.setattr(ss, "fetch_json", fake_fetch)
    result = ss._zenodo("https://zenodo.org/records/19965384")
    assert result and "Dataset" in result.text

def test_grantsgov_api_string_error_handling(monkeypatch):
    from core.structured_sources import grantsgov_paginated_search
    # Test A: Exact observed failure
    def mock_post_json(url, json_body, **kwargs):
        return {"errorcode": 0, "msg": "Webservice Succeeds", "token": "mock_token", "data": "read ECONNRESET"}, url
    monkeypatch.setattr(ss, "post_json", mock_post_json)
    result = grantsgov_paginated_search({"keyword": "test"}, test_limits={"max_pages": 1})
    assert len(result["hits"]) == 0
    assert len(result["errors"]) == 1
    assert "non-dict data block" in result["errors"][0]
    assert "read ECONNRESET" in result["errors"][0]
    assert result["pagination_complete"] is False

def test_grantsgov_api_valid_empty_response(monkeypatch):
    from core.structured_sources import grantsgov_paginated_search
    # Test B: Valid empty response
    def mock_post_json(url, json_body, **kwargs):
        return {"errorcode": 0, "data": {"hitCount": 0, "oppHits": []}}, url
    monkeypatch.setattr(ss, "post_json", mock_post_json)
    result = grantsgov_paginated_search({"keyword": "test"})
    assert len(result["errors"]) == 0
    assert len(result["hits"]) == 0
    assert result["pagination_complete"] is True

def test_grantsgov_api_valid_response_with_records(monkeypatch):
    from core.structured_sources import grantsgov_paginated_search
    # Test C: Valid response with records
    def mock_post_json(url, json_body, **kwargs):
        return {"errorcode": 0, "data": {"hitCount": 2, "oppHits": [{"id": "1", "title": "Opp 1"}, {"id": "2", "title": "Opp 2"}]}}, url
    monkeypatch.setattr(ss, "post_json", mock_post_json)
    result = grantsgov_paginated_search({"keyword": "test"})
    assert len(result["errors"]) == 0
    assert len(result["hits"]) == 2
    assert result["pagination_complete"] is True

def test_grantsgov_api_multiple_pages(monkeypatch):
    from core.structured_sources import grantsgov_paginated_search
    # Test D: Multiple pages
    def mock_post_json(url, json_body, **kwargs):
        start = json_body.get("startRecordNum", 0)
        # return 100 items for first page to trigger pagination
        if start == 0:
            hits = [{"id": str(i), "title": f"Opp {i}"} for i in range(100)]
        else:
            hits = [{"id": str(i), "title": f"Opp {i}"} for i in range(100, 108)]
        return {"errorcode": 0, "data": {"hitCount": 108, "oppHits": hits}}, url
    monkeypatch.setattr(ss, "post_json", mock_post_json)
    result = grantsgov_paginated_search({"keyword": "test"})
    assert len(result["hits"]) == 108
    assert result["pagination_complete"] is True
    assert result["pages_fetched"] == 2

def test_grantsgov_api_repeated_page_protection(monkeypatch):
    from core.structured_sources import grantsgov_paginated_search
    # Test E: Repeated page protection
    def mock_post_json(url, json_body, **kwargs):
        # Always return the same 100 items
        hits = [{"id": str(i), "title": f"Opp {i}"} for i in range(100)]
        return {"errorcode": 0, "data": {"hitCount": 200, "oppHits": hits}}, url
    monkeypatch.setattr(ss, "post_json", mock_post_json)
    result = grantsgov_paginated_search({"keyword": "test"})
    assert result["pagination_complete"] is False
    assert len(result["errors"]) > 0
    assert "repeated page content" in result["errors"][0]

def test_grantsgov_api_malformed_pagination(monkeypatch):
    from core.structured_sources import grantsgov_paginated_search
    # Test F: Malformed pagination metadata
    def mock_post_json(url, json_body, **kwargs):
        return {"errorcode": 0, "data": {"hitCount": "abc", "oppHits": [{"id": "1", "title": "Opp 1"}]}}, url
    monkeypatch.setattr(ss, "post_json", mock_post_json)
    result = grantsgov_paginated_search({"keyword": "test"})
    assert result["pagination_complete"] is False
    assert len(result["errors"]) > 0
    assert "malformed hitCount" in result["errors"][0]

def test_grantsgov_api_http_exception(monkeypatch):
    from core.structured_sources import grantsgov_paginated_search
    # Test G: HTTP exception
    def mock_post_json(url, json_body, **kwargs):
        raise ConnectionResetError("Connection reset by peer")
    monkeypatch.setattr(ss, "post_json", mock_post_json)
    result = grantsgov_paginated_search({"keyword": "test"})
    assert len(result["hits"]) == 0
    assert result["pagination_complete"] is False
    assert len(result["errors"]) > 0
    assert "API request failed" in result["errors"][0]

def test_grantsgov_api_non_json_response(monkeypatch):
    from core.structured_sources import grantsgov_paginated_search
    # Test H: Non-JSON response
    def mock_post_json(url, json_body, **kwargs):
        # According to http_client.py, this would raise ValueError or be caught as Exception
        # We can just simulate returning a string instead of a dict from post_json
        # Wait, post_json normally returns (dict, str), but if the server is weird:
        return "<html>Bad Gateway</html>", url
    monkeypatch.setattr(ss, "post_json", mock_post_json)
    result = grantsgov_paginated_search({"keyword": "test"})
    assert len(result["hits"]) == 0
    assert result["pagination_complete"] is False
    assert len(result["errors"]) > 0
    assert "non-JSON/non-dict" in result["errors"][0]

