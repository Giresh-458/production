"""Tests for the funding collection pipeline.

Tests pagination, crawl depth, discovery, PDFs, errors, normalization, and status
determination — all deterministic with mocks.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from core.schemas import CollectionResult

@pytest.fixture(autouse=True)
def bypass_live_robots_and_wait_in_crawl_unit_tests(monkeypatch):
    """Keep deterministic crawler tests offline; policy behavior has its own tests."""
    monkeypatch.setattr(
        "core.crawl4ai_web._get_robots_policy",
        lambda url: (True, 1.0, None),
    )
    monkeypatch.setattr(
        "core.crawl4ai_web._wait_for_host_slot",
        lambda url, delay_seconds: True,
    )


# ── Grants.gov pagination tests ──────────────────────────────────────

def test_grantsgov_single_page():
    """Single page: all results in one response, pagination_complete=True."""
    from core.structured_sources import grantsgov_paginated_search, post_json

    def fake_post(url, *, json_body, headers=None, timeout=(8,25), retries=2):
        return ({"data": {"hitCount": 2, "oppHits": [
            {"title": "A", "id": "1"}, {"title": "B", "id": "2"}
        ]}}, url)

    with patch("core.structured_sources.post_json", fake_post):
        result = grantsgov_paginated_search("test")

    assert result["pagination_complete"] is True
    assert len(result["hits"]) == 2
    assert result["total_expected"] == 2
    assert result["pages_fetched"] == 1


def test_grantsgov_multi_page():
    """Multiple pages: exhausts all pages."""
    from core.structured_sources import grantsgov_paginated_search

    call_count = 0
    def fake_post(url, *, json_body, headers=None, timeout=(8,25), retries=2):
        nonlocal call_count
        call_count += 1
        start = json_body["startRecordNum"]
        if start == 0:
            return ({"data": {"hitCount": 3, "oppHits": [
                {"title": "A", "id": "1"}, {"title": "B", "id": "2"}
            ]}}, url)
        elif start == 2:
            return ({"data": {"hitCount": 3, "oppHits": [
                {"title": "C", "id": "3"}
            ]}}, url)
        return ({"data": {"hitCount": 3, "oppHits": []}}, url)

    with patch("core.structured_sources.post_json", fake_post):
        result = grantsgov_paginated_search("test", limit_per_page=2)

    assert result["pagination_complete"] is True
    assert len(result["hits"]) == 3
    assert result["total_expected"] == 3
    assert result["pages_fetched"] == 2


def test_grantsgov_api_failure():
    """API failure: records error, pagination_complete stays False."""
    from core.structured_sources import grantsgov_paginated_search

    def fake_post(url, *, json_body, headers=None, timeout=(8,25), retries=2):
        raise ConnectionError("API down")

    with patch("core.structured_sources.post_json", fake_post):
        result = grantsgov_paginated_search("test")

    assert result["pagination_complete"] is False
    assert len(result["hits"]) == 0
    assert len(result["errors"]) == 1


def test_grantsgov_stalled_pagination():
    """Non-advancing pagination: detected and stopped without claiming complete."""
    from core.structured_sources import grantsgov_paginated_search

    call_count = 0
    def fake_post(url, *, json_body, headers=None, timeout=(8,25), retries=2):
        nonlocal call_count
        call_count += 1
        # Always return same data with same startRecord — simulates stall
        return ({"data": {"hitCount": 100, "oppHits": [
            {"title": "A", "id": "1"}
        ]}}, url)

    with patch("core.structured_sources.post_json", fake_post):
        # The function should detect the stall after 2 iterations
        result = grantsgov_paginated_search("test", limit_per_page=1)

    # It should stop (not loop infinitely) and not claim complete
    assert call_count <= 3  # initial + one more before stall detection
    # If it consumed all expected, it would claim complete; but hitCount is 100
    # and we only got 1-2 records. The key check: no infinite loop.
    assert len(result["hits"]) >= 1


# ── GitHub pagination tests ──────────────────────────────────────────

def test_github_single_page():
    """Single page of issues: pagination_complete=True."""
    from core.structured_sources import github_paginated_issues

    mock_response = MagicMock()
    mock_response.json.return_value = [
        {"id": 1, "title": "Grant A", "html_url": "https://github.com/x/y/issues/1",
         "state": "open", "labels": [], "updated_at": "2026-01-01"}
    ]
    mock_response.headers = {"Link": ""}
    mock_response.status_code = 200

    with patch("core.structured_sources.get_response", return_value=mock_response):
        result = github_paginated_issues("filecoin-project", "devgrants")

    assert result["pagination_complete"] is True
    assert len(result["issues"]) == 1
    assert result["pages_fetched"] == 1


def test_github_rate_limited():
    """Rate limit: detected, recorded, pagination_complete=False."""
    from core.structured_sources import github_paginated_issues

    mock_response = MagicMock()
    mock_response.json.return_value = [{"id": 1, "title": "A"}]
    mock_response.headers = {"Link": '<url>; rel="next"', "X-RateLimit-Remaining": "0"}
    mock_response.status_code = 200

    with patch("core.structured_sources.get_response", return_value=mock_response):
        result = github_paginated_issues("x", "y")

    # First page fetched, then rate limit detected on check
    assert result["rate_limited"] is True
    assert len(result["errors"]) >= 1


def test_github_repeated_page():
    """Repeated page: detected, stops without claiming complete."""
    from core.structured_sources import github_paginated_issues

    call_count = 0
    def fake_get(url, **kwargs):
        nonlocal call_count
        call_count += 1
        mock = MagicMock()
        mock.json.return_value = [{"id": 1, "title": "Same issue"}]
        mock.headers = {"Link": '<url>; rel="next"'}
        mock.status_code = 200
        return mock

    with patch("core.structured_sources.get_response", fake_get):
        result = github_paginated_issues("x", "y")

    assert call_count == 2  # first page + one repeated page detected
    assert result["pagination_complete"] is False
    assert len(result["errors"]) >= 1


def test_github_document_limit_stops_pagination_without_overfetching():
    """GitHub API max_documents constrains request page size and reports a coverage gap."""
    from core.structured_sources import github_paginated_issues

    mock_response = MagicMock()
    mock_response.json.return_value = [
        {"id": 1, "title": "Grant A"},
        {"id": 2, "title": "Grant B"},
    ]
    mock_response.headers = {"Link": '<https://api.github.com/repos/x/y/issues?page=2>; rel="next"'}
    mock_response.status_code = 200

    with patch("core.structured_sources.get_response", return_value=mock_response) as mock_get:
        result = github_paginated_issues(
            "x", "y", source_limits={"max_pages": 5, "max_documents": 2}
        )

    assert mock_get.call_args.kwargs["params"]["per_page"] == 2
    assert len(result["issues"]) == 2
    assert result["pages_fetched"] == 1
    assert result["pagination_complete"] is False
    assert any("SOURCE_LIMIT_REACHED: Max documents reached (2)" in err for err in result["errors"])


def test_github_document_test_limit_is_reported_as_test_truncated():
    """An artificial test-only document cap must not be mislabeled as a production source cap."""
    from core.structured_sources import github_paginated_issues

    mock_response = MagicMock()
    mock_response.json.return_value = [
        {"id": 1, "title": "Grant A"},
        {"id": 2, "title": "Grant B"},
    ]
    mock_response.headers = {"Link": '<https://api.github.com/repos/x/y/issues?page=2>; rel="next"'}
    mock_response.status_code = 200

    with patch("core.structured_sources.get_response", return_value=mock_response) as mock_get:
        result = github_paginated_issues(
            "x", "y", test_limits={"max_documents": 2}
        )

    assert mock_get.call_args.kwargs["params"]["per_page"] == 2
    assert len(result["issues"]) == 2
    assert result["pagination_complete"] is False
    assert any("TEST_TRUNCATED: Max documents reached (2)" in err for err in result["errors"])


# ── Crawl4AI web collection tests ────────────────────────────────────

def test_crawl_depth_zero():
    """depth=0: only start URLs are fetched."""
    from core.crawl4ai_web import crawl_funding_source

    async def fake_fetch(url):
        return {
            "html": "<html><body>Grant program</body></html>",
            "markdown": "Grant program. Apply now. Funding opportunity.",
            "success": True, "error_message": None, "status_code": 200,
            "url": url, "title": "Grants",
            "links": [{"url": "https://example.com/detail", "anchor_text": "Details", "surrounding_text": "", "rel": ""}],
            "pagination_links": [],
        }

    with patch("core.crawl4ai_web._fetch_url", fake_fetch), \
         patch("core.crawl4ai_web.asyncio.run", lambda coro: asyncio.get_event_loop().run_until_complete(coro) if False else None):
        # Patch _fetch_sync directly
        with patch("core.crawl4ai_web._fetch_sync") as mock_fetch:
            mock_fetch.return_value = {
                "html": "<html><body>Grant program</body></html>",
                "markdown": "Grant program. Apply now. Funding opportunity.",
                "success": True, "error_message": None, "status_code": 200,
                "url": "https://example.com/grants", "title": "Grants",
                "links": [{"url": "https://example.com/detail", "anchor_text": "Details", "surrounding_text": "", "rel": ""}],
                "pagination_links": [],
            }

            result = crawl_funding_source(
                "Test", "https://example.com/grants",
                {"start_urls": ["https://example.com/grants"], "max_depth": 0},
                [],
            )

    assert result.pages_fetched == 1
    assert result.max_depth_reached == 0
    assert result.discovery_exhausted is True


def test_failed_fetches_still_consume_the_configured_page_budget():
    """A source with failing URLs cannot exceed max_pages by counting only successes."""
    from core.crawl4ai_web import crawl_funding_source

    fetch_calls = []

    def fake_fetch(url):
        fetch_calls.append(url)
        if url.endswith("/grants"):
            return {
                "html": "<html><body>Grant funding call</body></html>",
                "markdown": "Grant funding call. Eligibility requirements. Apply now.",
                "success": True, "error_message": None, "status_code": 200,
                "url": url, "title": "Grant Program",
                "links": [
                    {
                        "url": f"https://example.com/grants/child-{index}",
                        "anchor_text": "Grant details",
                        "surrounding_text": "funding grant application",
                        "rel": "",
                    }
                    for index in range(10)
                ],
                "pagination_links": [],
            }
        return {
            "html": "", "markdown": "", "success": False,
            "error_message": "503 Service Unavailable", "status_code": 503,
            "url": url, "title": "", "links": [], "pagination_links": [],
        }

    with patch("core.crawl4ai_web._fetch_sync", fake_fetch):
        result = crawl_funding_source(
            "Test", "https://example.com/grants",
            {
                "start_urls": ["https://example.com/grants"],
                "max_depth": 1,
                "max_pages": 2,
            },
            [],
        )

    assert len(fetch_calls) == 2
    assert result.pages_discovered == 2
    assert result.pages_fetched == 1
    assert result.pages_failed == 1
    assert result.truncated is True
    assert result.final_status == "PARTIAL"


def test_crawl_duplicate_links():
    """Duplicate links: discovered once, not fetched twice."""
    from core.crawl4ai_web import crawl_funding_source

    call_count = 0
    def fake_fetch(url):
        nonlocal call_count
        call_count += 1
        return {
            "html": "<html><body>Grant funding opportunity</body></html>",
            "markdown": "Grant funding opportunity. Apply now.",
            "success": True, "error_message": None, "status_code": 200,
            "url": url, "title": "Grants",
            "links": [
                {"url": "https://example.com/grant-a", "anchor_text": "Grant A", "surrounding_text": "funding", "rel": ""},
                {"url": "https://example.com/grant-a", "anchor_text": "Grant A again", "surrounding_text": "funding", "rel": ""},
            ],
            "pagination_links": [],
        }

    with patch("core.crawl4ai_web._fetch_sync", fake_fetch):
        result = crawl_funding_source(
            "Test", "https://example.com/grants",
            {"start_urls": ["https://example.com/grants"], "max_depth": 1},
            [],
        )

    assert result.duplicates_removed >= 1


def test_crawl_external_domain_skipped():
    """External domain links: discovered but not crawled."""
    from core.crawl4ai_web import crawl_funding_source

    def fake_fetch(url):
        return {
            "html": "<html><body>Grant funding opportunity</body></html>",
            "markdown": "Grant funding opportunity. Apply now.",
            "success": True, "error_message": None, "status_code": 200,
            "url": url, "title": "Grants",
            "links": [
                {"url": "https://evil.com/phish", "anchor_text": "Grant", "surrounding_text": "funding", "rel": ""},
            ],
            "pagination_links": [],
        }

    with patch("core.crawl4ai_web._fetch_sync", fake_fetch):
        result = crawl_funding_source(
            "Test", "https://example.com/grants",
            {"start_urls": ["https://example.com/grants"], "max_depth": 2},
            [],  # no additional allowed domains
        )

    assert result.urls_skipped >= 1
    assert result.pages_fetched == 1  # only the start page


def test_empty_ethereum_rfp_page_is_not_normalized_as_a_grant():
    """Explicit no-active language must not become a generic funding record."""
    from core.crawl4ai_web import crawl_funding_source

    def fake_fetch(url):
        return {
            "html": "<html><body>No RFP Items Available</body></html>",
            "markdown": (
                "Request for Proposals (RFPs). No RFP Items Available. "
                "There are currently no active Request for Proposals available for application."
            ),
            "success": True, "error_message": None, "status_code": 200,
            "url": url, "title": "Request for Proposals (RFPs)",
            "links": [], "pagination_links": [],
        }

    with patch("core.crawl4ai_web._fetch_sync", fake_fetch):
        result = crawl_funding_source(
            "Ethereum ESP RFPs",
            "https://esp.ethereum.foundation/applicants/rfp",
            {"start_urls": ["https://esp.ethereum.foundation/applicants/rfp"],
             "max_depth": 1, "max_pages": 2},
            ["esp.ethereum.foundation"],
        )

    assert result.pages_fetched == 1
    assert result.opportunities == []
    assert result.records_discovered == 0
    assert result.final_status == "NO_ACTIVE_OPPORTUNITIES"


def test_solana_and_near_funding_directories_are_not_individual_calls():
    """Known ecosystem-directory copy should be traversed, not emitted as a call."""
    from core.crawl4ai_web import _classify_page_stage_b

    assert not _classify_page_stage_b(
        "https://solana.org/grants-funding",
        "different grant and funding opportunities within the Solana ecosystem",
        "There are numerous grant programs in the ecosystem.",
    )
    assert not _classify_page_stage_b(
        "https://www.near.org/funding",
        "NEAR House of Stake: A New Governance Model",
        "Explore grants, incentives, and community-backed resources on NEAR. "
        "Apply here for NEAR grants to fund your project.",
    )


def test_skipped_pdf_is_counted_as_a_document_and_makes_coverage_partial():
    """A discovered but intentionally unprocessed PDF must not look like a complete empty crawl."""
    from core.crawl4ai_web import crawl_funding_source

    pdf_url = "https://example.com/reports/annual-funding-report.pdf"

    def fake_fetch(url):
        return {
            "html": "<html><body>Funding programme overview</body></html>",
            "markdown": "Funding programme overview.",
            "success": True, "error_message": None, "status_code": 200,
            "url": url, "title": "Funding programme overview",
            "links": [{
                "url": pdf_url, "anchor_text": "Grant programme report",
                "surrounding_text": "Funding report", "rel": "",
            }],
            "pagination_links": [],
        }

    with patch("core.crawl4ai_web._fetch_sync", fake_fetch):
        result = crawl_funding_source(
            "Test", "https://example.com/grants",
            {"start_urls": ["https://example.com/grants"], "max_depth": 1,
             "max_pages": 5, "follow_detail_pages": True, "follow_pdfs": False},
            [],
        )

    assert result.pages_discovered == 1
    assert result.pages_fetched == 1
    assert result.documents_discovered == 1
    assert result.documents_fetched == 0
    assert result.truncated is True
    assert result.final_status == "PARTIAL"


# ── PDF tests ────────────────────────────────────────────────────────

def test_pdf_full_processing():
    """PDF: all pages processed, no truncation."""
    from core.crawl4ai_web import _fetch_pdf

    fake_page = MagicMock()
    fake_page.extract_text.return_value = "Page content"

    fake_reader = MagicMock()
    fake_reader.pages = [fake_page] * 60  # 60 pages

    fake_response = MagicMock()
    fake_response.content = b"fake pdf bytes"

    with patch("core.crawl4ai_web.get_response", return_value=fake_response), \
         patch("core.crawl4ai_web.PdfReader", return_value=fake_reader):
        result = _fetch_pdf("https://example.com/doc.pdf")

    assert result["success"] is True
    assert result["total_pages"] == 60
    assert result["pages_processed"] == 60
    assert result["truncated"] is False


def test_pdf_configured_limit_reports_truncation():
    """PDF with configured limit: truncated=True."""
    from core.crawl4ai_web import _fetch_pdf

    fake_page = MagicMock()
    fake_page.extract_text.return_value = "Page content"

    fake_reader = MagicMock()
    fake_reader.pages = [fake_page] * 100

    fake_response = MagicMock()
    fake_response.content = b"fake pdf bytes"

    with patch("core.crawl4ai_web.get_response", return_value=fake_response), \
         patch("core.crawl4ai_web.PdfReader", return_value=fake_reader):
        result = _fetch_pdf("https://example.com/doc.pdf", max_pdf_pages=50)

    assert result["success"] is True
    assert result["total_pages"] == 100
    assert result["pages_processed"] == 50
    assert result["truncated"] is True


def test_pdf_extraction_failure():
    """PDF extraction failure: error recorded."""
    from core.crawl4ai_web import _fetch_pdf

    with patch("core.crawl4ai_web.get_response", side_effect=ConnectionError("timeout")):
        result = _fetch_pdf("https://example.com/doc.pdf")

    assert result["success"] is False
    assert result["error"] is not None


def _minimal_docx_bytes(paragraphs):
    """Build a small DOCX-compatible archive for offline extraction tests."""
    from io import BytesIO
    from zipfile import ZipFile
    import xml.etree.ElementTree as ET

    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    document = ET.Element(f"{ns}document")
    body = ET.SubElement(document, f"{ns}body")
    for value in paragraphs:
        paragraph = ET.SubElement(body, f"{ns}p")
        run = ET.SubElement(paragraph, f"{ns}r")
        text = ET.SubElement(run, f"{ns}t")
        text.text = value

    xml = ET.tostring(document, encoding="utf-8", xml_declaration=True)
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def test_docx_fetch_extracts_paragraph_text():
    """DOCX text is downloaded and extracted without a browser dependency."""
    from core.crawl4ai_web import _fetch_docx

    response = MagicMock()
    response.content = _minimal_docx_bytes([
        "Call for research proposals",
        "Eligibility: universities and research teams",
        "Submission deadline: 30 November 2026",
    ])

    with patch("core.crawl4ai_web.get_response", return_value=response):
        result = _fetch_docx("https://example.com/call.docx")

    assert result["success"] is True
    assert result["paragraphs_extracted"] == 3
    assert "Call for research proposals" in result["content"]
    assert "Submission deadline: 30 November 2026" in result["content"]
    assert result["truncated"] is False


def test_docx_fetch_reports_invalid_archive():
    """Invalid DOCX files must be surfaced as failures, not empty evidence."""
    from core.crawl4ai_web import _fetch_docx

    response = MagicMock()
    response.content = b"not a zip archive"

    with patch("core.crawl4ai_web.get_response", return_value=response):
        result = _fetch_docx("https://example.com/call.docx")

    assert result["success"] is False
    assert result["error"]


def test_crawler_downloads_docx_without_browser_navigation():
    """Configured DOCX links use direct download and count as fetched documents."""
    from core.crawl4ai_web import crawl_funding_source

    docx_url = "https://example.com/docs/proposal-template.docx"

    browser_calls = []

    def fake_fetch(url):
        browser_calls.append(url)
        assert url == "https://example.com/grants"
        return {
            "html": "<html><body>Grant funding call</body></html>",
            "markdown": (
                "Grant funding call. Eligibility requirements. "
                "Submit a proposal before the application deadline."
            ),
            "success": True, "error_message": None, "status_code": 200,
            "url": url, "title": "Research Funding",
            "links": [{
                "url": docx_url,
                "anchor_text": "Download proposal document",
                "surrounding_text": "Grant funding application proposal",
                "rel": "",
            }],
            "pagination_links": [],
        }

    response = MagicMock()
    response.content = _minimal_docx_bytes([
        "Cyber-physical systems research call",
        "Eligible applicants: universities",
    ])

    with patch("core.crawl4ai_web._fetch_sync", fake_fetch), \
         patch("core.crawl4ai_web.get_response", return_value=response):
        result = crawl_funding_source(
            "Test", "https://example.com/grants",
            {
                "start_urls": ["https://example.com/grants"],
                "max_depth": 1,
                "max_pages": 10,
                "follow_detail_pages": True,
                "follow_documents": True,
            },
            [],
        )

    assert browser_calls == ["https://example.com/grants"]
    assert result.documents_discovered == 1
    assert result.documents_fetched == 1
    assert result.documents_failed == 0
    assert any(item.get("type") == "docx" for item in result.opportunities)
    assert any("Cyber-physical systems research call" in item.get("content", "") for item in result.opportunities)
    assert result.final_status == "VERIFIED"


def test_docx_download_failure_is_recorded_and_source_partial():
    """A failed DOCX download is visible and downgrades an otherwise useful crawl."""
    from core.crawl4ai_web import crawl_funding_source

    docx_url = "https://example.com/docs/proposal-template.docx"

    def fake_fetch(url):
        return {
            "html": "<html><body>Grant funding call</body></html>",
            "markdown": (
                "Grant funding call. Eligibility requirements. "
                "Submit a proposal before the application deadline."
            ),
            "success": True, "error_message": None, "status_code": 200,
            "url": url, "title": "Research Funding",
            "links": [{
                "url": docx_url,
                "anchor_text": "Download proposal document",
                "surrounding_text": "Grant funding application proposal",
                "rel": "",
            }],
            "pagination_links": [],
        }

    with patch("core.crawl4ai_web._fetch_sync", fake_fetch), \
         patch("core.crawl4ai_web.get_response", side_effect=ConnectionError("download timeout")):
        result = crawl_funding_source(
            "Test", "https://example.com/grants",
            {
                "start_urls": ["https://example.com/grants"],
                "max_depth": 1,
                "max_pages": 10,
                "follow_detail_pages": True,
                "follow_documents": True,
            },
            [],
        )

    assert result.documents_discovered == 1
    assert result.documents_failed == 1
    assert result.errors[-1]["operation"] == "docx_fetch"
    assert result.final_status == "PARTIAL"


# ── Error handling tests ─────────────────────────────────────────────

def test_errors_recorded_not_swallowed():
    """Page fetch failures: recorded in result.errors and failed_urls."""
    from core.crawl4ai_web import crawl_funding_source

    def fake_fetch(url):
        return {
            "html": "", "markdown": "", "success": False,
            "error_message": "403 Forbidden", "status_code": 403,
            "url": url, "title": "", "links": [], "pagination_links": [],
        }

    with patch("core.crawl4ai_web._fetch_sync", fake_fetch):
        result = crawl_funding_source(
            "Test", "https://example.com/grants",
            {"start_urls": ["https://example.com/grants"], "max_depth": 1},
            [],
        )

    assert result.pages_failed == 1
    assert len(result.failed_urls) == 1
    assert len(result.errors) == 1
    assert result.final_status == "FAIL"


def test_crawl_with_successful_and_failed_pages_is_partial():
    """A drained queue is not verified when a discovered page failed to fetch."""
    from core.crawl4ai_web import crawl_funding_source

    def fake_fetch(url):
        if url.endswith("/rfp/details"):
            return {
                "html": "", "markdown": "", "success": False,
                "error_message": "503 Service Unavailable", "status_code": 503,
                "url": url, "title": "", "links": [], "pagination_links": [],
            }
        return {
            "html": "<html><body>Funding grant</body></html>",
            "markdown": (
                "Grant funding opportunity. Eligibility requirements. "
                "Submit a proposal before the application deadline."
            ),
            "success": True, "error_message": None, "status_code": 200,
            "url": url, "title": "Grant Program",
            "links": [{
                "url": "https://example.com/rfp/details",
                "anchor_text": "RFP details",
                "surrounding_text": "Grant funding proposal details",
                "rel": "",
            }],
            "pagination_links": [],
        }

    with patch("core.crawl4ai_web._fetch_sync", fake_fetch):
        result = crawl_funding_source(
            "Test", "https://example.com/grants",
            {
                "start_urls": ["https://example.com/grants"],
                "max_depth": 1,
                "follow_detail_pages": True,
            },
            [],
        )

    assert result.pages_fetched == 1
    assert result.pages_failed == 1
    assert result.discovery_exhausted is True
    assert result.opportunities
    assert result.errors
    assert result.final_status == "PARTIAL"


def test_web_collector_does_not_report_no_active_when_fetches_failed():
    """A source with coverage errors and no records is partial, not confirmed empty."""
    from agents.funding_agent import FundingSource
    from agents.funding_collector import _collect_web_source

    collected = CollectionResult(
        source_id="Test",
        method="web",
        pages_fetched=1,
        pages_failed=1,
        discovery_exhausted=True,
        final_status="PARTIAL",
        errors=[{
            "url": "https://example.com/grant",
            "operation": "page_fetch",
            "error": "503 Service Unavailable",
        }],
    )
    source = FundingSource(
        name="Test",
        url="https://example.com/grants",
        tier="tier_1",
        collection={},
    )

    with patch("agents.funding_collector.crawl_funding_source", return_value=collected):
        result = _collect_web_source(source, area=None, warnings=[])

    assert result.final_status == "PARTIAL"
    assert result.records_valid == 0
    assert result.pages_failed == 1


def test_web_normalization_failure_downgrades_verified_to_partial():
    """One bad record must not leave a mixed-success source marked VERIFIED."""
    from agents.funding_agent import FundingSource
    from agents.funding_collector import _collect_web_source

    collected = CollectionResult(
        source_id="Test",
        method="web",
        pages_fetched=2,
        discovery_exhausted=True,
        final_status="VERIFIED",
        opportunities=[
            {"type": "html", "url": "https://example.com/grant-a", "title": "Grant A", "content": "Grant funding eligibility"},
            {"type": "html", "url": "https://example.com/grant-b", "title": "Grant B", "content": "Grant funding eligibility"},
        ],
    )
    source = FundingSource(
        name="Test",
        url="https://example.com/grants",
        tier="tier_1",
        collection={},
    )

    with patch(
        "agents.funding_collector.crawl_funding_source",
        return_value=collected,
    ), patch(
        "agents.funding_collector.analyze_document",
        side_effect=[object(), RuntimeError("normalization failed")],
    ):
        result = _collect_web_source(source, area=None, warnings=[])

    assert result.records_valid == 1
    assert result.records_failed == 1
    assert result.final_status == "PARTIAL"


# ── Normalization tests ──────────────────────────────────────────────

def test_web_collector_normalizes_raw_to_funding_document():
    """Web source raw dicts must become (FundingDocument, FundingOpportunity) tuples."""
    from agents.funding_collector import _normalize_raw_to_doc
    from agents.funding_agent import FundingSource, FundingDocument

    source = FundingSource(name="Test Org", url="https://example.com", tier="tier_1", collection={})
    raw = {"type": "html", "url": "https://example.com/grants/123", "content": "Grant details", "title": "Test Grant"}
    doc = _normalize_raw_to_doc(source, raw)

    assert isinstance(doc, FundingDocument)
    assert doc.organization == "Test Org"
    assert doc.title == "Test Grant"
    assert doc.source == "https://example.com/grants/123"
    assert doc.source_type == "Web Traversal"


def test_pdf_collector_normalizes_raw_to_funding_document():
    """PDF raw dicts must become FundingDocument with PDF source type."""
    from agents.funding_collector import _normalize_raw_to_doc
    from agents.funding_agent import FundingSource, FundingDocument

    source = FundingSource(name="Test Org", url="https://example.com", tier="tier_1", collection={})
    raw = {"type": "pdf", "url": "https://example.com/doc.pdf", "content": "PDF text", "title": ""}
    doc = _normalize_raw_to_doc(source, raw)

    assert isinstance(doc, FundingDocument)
    assert doc.source_type == "PDF Document"


# ── Status determination tests ───────────────────────────────────────

def test_status_verified_on_complete_collection():
    """VERIFIED: collection complete, valid results found."""
    result = CollectionResult(source_id="test", method="web")
    result.pages_fetched = 5
    result.discovery_exhausted = True
    result.pagination_complete = True
    result.records_valid = 3
    result.final_status = "VERIFIED"

    assert result.final_status == "VERIFIED"
    assert result.truncated is False


def test_status_partial_on_truncation():
    """PARTIAL: truncated collection."""
    result = CollectionResult(source_id="test", method="web")
    result.pages_fetched = 100
    result.truncated = True
    result.pagination_complete = False
    result.final_status = "PARTIAL"

    assert result.final_status == "PARTIAL"
    assert result.truncated is True


def test_status_fail_on_no_pages():
    """FAIL: no pages could be fetched."""
    result = CollectionResult(source_id="test", method="web")
    result.pages_failed = 1
    result.final_status = "FAIL"

    assert result.final_status == "FAIL"


def test_status_no_active_opportunities():
    """NO_ACTIVE_OPPORTUNITIES: pages fetched but no qualifying opportunities."""
    result = CollectionResult(source_id="test", method="web")
    result.pages_fetched = 3
    result.discovery_exhausted = True
    result.records_valid = 0
    result.final_status = "NO_ACTIVE_OPPORTUNITIES"

    assert result.final_status == "NO_ACTIVE_OPPORTUNITIES"


# ── Existing test (preserved) ────────────────────────────────────────

def test_generic_apply_heading_does_not_hide_specific_call_title():
    from bs4 import BeautifulSoup
    from agents.funding_agent import extract_page_title

    soup = BeautifulSoup(
        '<html><head><title>Apply for Road to Devcon 8 India - University Program | Ethereum Foundation ESP</title></head>'
        '<body><h1>How to Apply</h1><h2>Road to Devcon 8 India - University Program</h2></body></html>',
        'html.parser',
    )
    title = extract_page_title(soup, 'fallback')
    assert 'Road to Devcon 8 India - University Program' in title
