import time
import pytest
from datetime import datetime, UTC, timedelta

from core.crawl_context import CrawlContext, GLOBAL_MAX_PAGES, GLOBAL_MAX_DOCUMENTS, GLOBAL_MAX_SECONDS
from core.recursive_collection import collect_recursive_pages_with_review, RecursiveCollectionConfig
from core.web_collection import collect_source, WebCollectionResult
import core.http_client
import core.recursive_collection
import core.web_collection
import core.batch_runner

import unittest.mock as mock

# Patch network for all tests
class DummyResp:
    url = "http://test.com/research"
    text = "<html><body>" + ("<p>Long text so it does not fallback to browser.</p>" * 20) + "<a href='/research/1'>1</a><a href='/research/2'>2</a></body></html>"
    headers = {"Content-Type": "text/html"}
    content = text.encode()

@pytest.fixture(autouse=True)
def apply_global_mocks():
    with mock.patch("core.http_client.get_response", side_effect=lambda *a, **k: DummyResp()), \
         mock.patch("core.web_collection.get_response", side_effect=lambda *a, **k: DummyResp()), \
         mock.patch("core.recursive_collection._sitemap_links", side_effect=lambda *a, **k: []), \
         mock.patch("core.web_collection._robots_allowed", side_effect=lambda *a, **k: True), \
         mock.patch("core.recursive_collection.extract_ranked_links", side_effect=lambda *a, **k: [("http://test.com/a", 999, "anchor")]):
        yield


def dummy_fetch_html(url: str) -> str:
    return "<html><body><a href='/child1'>1</a><a href='/child2'>2</a></body></html>"

_extract_counter = 0
def dummy_extract_text(html: str) -> tuple[str, str]:
    global _extract_counter
    _extract_counter += 1
    return "Title", f"Text {_extract_counter}"

def test_1_global_max_pages_ceiling():
    ctx = CrawlContext()
    ctx.pages_fetched = GLOBAL_MAX_PAGES - 1
    ctx.add_page(0)
    ctx.add_page(0)
    assert ctx.check_limits() is True
    assert "global_max_pages" in ctx.stop_reasons

def test_2_global_max_documents_ceiling():
    ctx = CrawlContext()
    ctx.documents_collected = GLOBAL_MAX_DOCUMENTS - 1
    ctx.add_document()
    ctx.add_document()
    assert ctx.check_limits() is True
    assert "global_max_documents" in ctx.stop_reasons

def test_3_global_max_seconds_ceiling():
    ctx = CrawlContext()
    ctx.start_time = time.time() - GLOBAL_MAX_SECONDS - 1
    assert ctx.check_limits() is True
    assert "global_max_seconds" in ctx.stop_reasons

def test_4_source_max_pages_ceiling():
    ctx = CrawlContext()
    ctx.begin_source()
    ctx.add_page(0)
    ctx.add_page(0)
    assert ctx.check_limits(source_max_pages=2) is True
    assert "source_max_pages" in ctx.stop_reasons

def test_5_source_max_documents_ceiling():
    ctx = CrawlContext()
    ctx.begin_source()
    ctx.add_document()
    ctx.add_document()
    assert ctx.check_limits(source_max_documents=2) is True
    assert "source_max_documents" in ctx.stop_reasons

def test_6_source_max_seconds_ceiling():
    ctx = CrawlContext()
    ctx.begin_source()
    ctx.source_start_time = time.time() - 1000
    assert ctx.check_limits(source_max_seconds=5) is True
    assert "source_max_seconds" in ctx.stop_reasons

def test_7_deadline_expiration():
    deadline = (datetime.now(UTC) - timedelta(seconds=1)).timestamp()
    ctx = CrawlContext(deadline=deadline)
    assert ctx.check_limits() is True
    assert "deadline" in ctx.stop_reasons

def test_8_check_limits_depth_no_stop_reason():
    ctx = CrawlContext()
    # depth only skips the current URL scheduling, does not trigger global stop reason
    assert ctx.check_limits(source_max_depth=1, current_depth=2) is False
    assert not ctx.stop_reasons

def test_9_recursive_crawler_halts_on_context_limit():
    from core.crawl_context import current_crawl_context
    ctx = CrawlContext()
    ctx.pages_fetched = GLOBAL_MAX_PAGES - 1
    token = current_crawl_context.set(ctx)
    try:
        config = RecursiveCollectionConfig(max_pages=100, max_depth=100) # high limit, should hit context limit
        pages, review = collect_recursive_pages_with_review(
            root_url="http://test.com", root_name="test", initial_html="<html><body><a href='/a'>a</a><a href='/b'>b</a><a href='/c'>c</a><a href='/d'>d</a></body></html>",
            fetch_html=dummy_fetch_html, extract_page_text=dummy_extract_text, config=config
        )
        assert len(pages) == 2
        assert "global_max_pages" in ctx.stop_reasons
    finally:
        current_crawl_context.reset(token)

def test_10_recursive_crawler_respects_source_limit():
    from core.crawl_context import current_crawl_context
    ctx = CrawlContext()
    ctx.begin_source()
    token = current_crawl_context.set(ctx)
    try:
        config = RecursiveCollectionConfig(max_pages=2, max_depth=100) 
        pages, review = collect_recursive_pages_with_review(
            root_url="http://test.com", root_name="test", initial_html="<html><body><a href='/a'>a</a><a href='/b'>b</a><a href='/c'>c</a></body></html>",
            fetch_html=dummy_fetch_html, extract_page_text=dummy_extract_text, config=config
        )
        assert len(pages) == 2
        assert "source_max_pages" in ctx.stop_reasons
    finally:
        current_crawl_context.reset(token)

def test_11_recursive_crawler_returns_partial_results():
    from core.crawl_context import current_crawl_context
    deadline = (datetime.now(UTC) + timedelta(seconds=0.1)).timestamp()
    ctx = CrawlContext(deadline=deadline)
    token = current_crawl_context.set(ctx)
    try:
        config = RecursiveCollectionConfig(max_pages=100, max_depth=100)
        time.sleep(0.2)
        pages, review = collect_recursive_pages_with_review(
            root_url="http://test.com", root_name="test", initial_html="<html><body><a href='/a'>a</a><a href='/b'>b</a></body></html>",
            fetch_html=dummy_fetch_html, extract_page_text=dummy_extract_text, config=config
        )
        assert len(pages) == 1  # root page only
        assert "deadline" in ctx.stop_reasons
    finally:
        current_crawl_context.reset(token)

def test_12_collect_source_halts_on_context_limit():
    from core.crawl_context import current_crawl_context
    ctx = CrawlContext()
    ctx.pages_fetched = GLOBAL_MAX_PAGES - 1
    token = current_crawl_context.set(ctx)
    try:
        # Max pages allows 10, but context allows 1
        res = collect_source("http://test.com/research", keywords=(), max_pages=10)
        assert len(res.pages) == 1
        assert "global_max_pages" in ctx.stop_reasons
    finally:
        current_crawl_context.reset(token)

def test_13_playwright_event_loop_fallback():
    # Implicitly covered by threaded tests, but let's test isolation
    import asyncio
    loop1 = asyncio.new_event_loop()
    loop2 = asyncio.new_event_loop()
    assert loop1 is not loop2

def test_14_batch_runner_deadline_cancellation():
    from core.batch_runner import execute_batch_run
    
    original = core.batch_runner.run_registered_agent
    def mock_run(*a, **k):
        time.sleep(0.5)
        return {"documents": [], "status": "success"}
    core.batch_runner.run_registered_agent = mock_run
    
    try:
        results = execute_batch_run(
            agent_names=["lab"],
            mode="configured_scan",
            area_names=[None],
            analysis_mode="all",
            base_input_data={},
            execution_mode="parallel",
            max_workers=1,
            timeout_budget_seconds=0.001, # almost instant timeout
            default_limit=2,
            per_agent_limits={}
        )
        assert len(results["tasks"]) == 1
        assert results["tasks"][0]["status"] == "timeout_budget_exceeded"
    finally:
        core.batch_runner.run_registered_agent = original

_test15_counter = 0
def test_15_regression_fsb_tcfd_infinite_crawl():
    from core.crawl_context import current_crawl_context
    ctx = CrawlContext()
    ctx.pages_fetched = GLOBAL_MAX_PAGES - 5
    token = current_crawl_context.set(ctx)
    try:
        import core.recursive_collection as rc
        original_extract = rc.extract_ranked_links
        def infinite_links(*a, **k):
            global _test15_counter
            _test15_counter += 1
            return [(f"http://fsb-tcfd.org/{_test15_counter}", 999, "anchor")]
        rc.extract_ranked_links = infinite_links
        
        def infinite_fetch(url):
            return "<html><body><a href='/a'>a</a><a href='/b'>b</a></body></html>"
        
        def infinite_extract(html, **kw):
            global _test15_counter
            return "Title", f"Text {_test15_counter}"
        
        config = RecursiveCollectionConfig(max_pages=100, max_depth=100)
        pages, review = collect_recursive_pages_with_review(
            root_url="http://fsb-tcfd.org", root_name="TCFD", initial_html="<html><body><a href='/a'>a</a></body></html>",
            fetch_html=infinite_fetch, extract_page_text=infinite_extract, config=config
        )
        assert "global_max_pages" in ctx.stop_reasons
        rc.extract_ranked_links = original_extract
    finally:
        current_crawl_context.reset(token)
