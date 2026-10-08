import pytest
import time
from unittest.mock import patch, MagicMock
from core.crawl_context import CrawlContext, current_crawl_context

def test_enforcement_max_pages():
    ctx = CrawlContext()
    ctx.begin_source()
    current_crawl_context.set(ctx)
    
    assert ctx.check_limits(source_max_pages=3) == False
    ctx.source_pages_fetched = 1
    assert ctx.check_limits(source_max_pages=3) == False
    ctx.source_pages_fetched = 2
    assert ctx.check_limits(source_max_pages=3) == False
    ctx.source_pages_fetched = 3
    assert ctx.check_limits(source_max_pages=3) == True
    assert "source_max_pages" in ctx.stop_reasons
    current_crawl_context.set(None)

def test_enforcement_max_documents():
    ctx = CrawlContext()
    ctx.begin_source()
    current_crawl_context.set(ctx)
    
    assert ctx.check_limits(source_max_documents=2) == False
    ctx.source_documents_collected = 1
    assert ctx.check_limits(source_max_documents=2) == False
    ctx.source_documents_collected = 2
    assert ctx.check_limits(source_max_documents=2) == True
    assert "source_max_documents" in ctx.stop_reasons
    current_crawl_context.set(None)


def test_enforcement_max_seconds():
    ctx = CrawlContext()
    ctx.begin_source()
    current_crawl_context.set(ctx)
    
    assert ctx.check_limits(source_max_seconds=1) == False
    ctx.source_start_time = time.time() - 2.0
    assert ctx.check_limits(source_max_seconds=1) == True
    assert "source_max_seconds" in ctx.stop_reasons
    current_crawl_context.set(None)

def test_global_fallback():
    ctx = CrawlContext()
    ctx.begin_source()
    current_crawl_context.set(ctx)
    
    # Global fallback kicks in at 30 pages
    ctx.pages_fetched = 30
    assert ctx.check_limits(source_max_pages=None) == True
    assert "global_max_pages" in ctx.stop_reasons
    assert "source_max_pages" not in ctx.stop_reasons
    current_crawl_context.set(None)

def test_recursive_collection_honors_limits():
    from core.recursive_collection import collect_recursive_pages, RecursiveCollectionConfig
    
    ctx = CrawlContext()
    ctx.begin_source()
    current_crawl_context.set(ctx)

    def fake_fetch_html(url: str) -> str:
        ctx.source_pages_fetched += 1
        ctx.pages_fetched += 1
        return f'<a href="{url}/next">link</a>'
        
    pages = collect_recursive_pages(
        root_url="http://fake",
        initial_html='<a href="http://fake/next">link</a>',
        fetch_html=fake_fetch_html,
        extract_page_text=lambda h: ("title", "text"),
        config=RecursiveCollectionConfig(max_pages=3, max_depth=10, max_documents=None, max_seconds=None)
    )
    assert len(pages) <= 3
    assert ctx.pages_fetched <= 3
    current_crawl_context.set(None)
