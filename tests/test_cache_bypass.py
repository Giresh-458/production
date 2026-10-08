import json
import pytest
from core.shared_crawl_manager import SharedCrawlManager, CrawlResult
from core.source_registry import current_force_source_refresh
from unittest.mock import MagicMock, patch

@pytest.fixture(autouse=True)
def fresh_manager():
    # Reset singleton for testing
    SharedCrawlManager._instance = None
    mgr = SharedCrawlManager.get()
    
    # Mock the execute fetch to return a dummy result
    def mock_execute_fetch(url):
        return CrawlResult(
            canonical_url=url,
            final_url=url,
            status_code=200,
            content_type="text/html",
            title="Test",
            html="<p>Test</p>",
            clean_text="Test",
            content_hash="test",
            fetch_method="mock",
            from_cache=False,
            error=None
        )
    mgr._execute_fetch = MagicMock(side_effect=mock_execute_fetch)
    # Clean the in-memory SQLite persistent db mock for testing
    mgr.db_path = ":memory:"
    mgr._init_db()
    
    yield mgr
    SharedCrawlManager._instance = None
def test_case_a_normal_run(fresh_manager):
    # Case A - normal run
    # 1. Seed cache by fetching once
    res1 = fresh_manager.fetch("http://test.com/a")
    assert fresh_manager.stats_actual_fetches == 1
    assert fresh_manager.stats_cache_hits == 0
    assert not res1.from_cache
    
    # 2. Request again with force_refresh=False (the default)
    res2 = fresh_manager.fetch("http://test.com/a")
    # 3. Assert cache is used
    assert res2.from_cache
    # 4. Assert actual network fetch count remains 1
    assert fresh_manager.stats_actual_fetches == 1
    assert fresh_manager.stats_cache_hits == 1

def test_case_b_forced_refresh(fresh_manager):
    # 1. Seed cache
    res1 = fresh_manager.fetch("http://test.com/b")
    assert fresh_manager.stats_actual_fetches == 1
    
    # 2. Request with force_refresh=True
    token = current_force_source_refresh.set(True)
    try:
        res2 = fresh_manager.fetch("http://test.com/b")
        # 3. Assert cached value is NOT returned (from_cache = False)
        assert not res2.from_cache
        # 4. Assert underlying network handler is called again
        assert fresh_manager.stats_actual_fetches == 2
        # 5. Assert actual_network_fetches == 2, cache hits remain 0 for this request
        assert fresh_manager.stats_cache_hits == 0
    finally:
        current_force_source_refresh.reset(token)

def test_case_c_repeated_forced_refresh(fresh_manager):
    token = current_force_source_refresh.set(True)
    try:
        fresh_manager.fetch("http://test.com/c")
        fresh_manager.fetch("http://test.com/c")
        
        # Two actual fetches because they are not strictly in-flight at the exact same time
        assert fresh_manager.stats_actual_fetches == 2
        assert fresh_manager.stats_cache_hits == 0
    finally:
        current_force_source_refresh.reset(token)

def test_case_d_in_flight_deduplication(fresh_manager):
    import threading
    import time
    
    # Force a delay in execute_fetch to ensure they overlap
    original_execute = fresh_manager._execute_fetch
    def slow_execute_fetch(url):
        time.sleep(0.5)
        return original_execute(url)
    
    fresh_manager._execute_fetch = MagicMock(side_effect=slow_execute_fetch)
    
    token = current_force_source_refresh.set(True)
    try:
        t1 = threading.Thread(target=lambda: fresh_manager.fetch("http://test.com/d"))
        t2 = threading.Thread(target=lambda: fresh_manager.fetch("http://test.com/d"))
        
        t1.start()
        t2.start()
        
        t1.join()
        t2.join()
        
        # One actual underlying fetch
        assert fresh_manager.stats_actual_fetches == 1
        # One in-flight deduplication
        assert fresh_manager.stats_in_flight_dedupes == 1
        assert fresh_manager.stats_cache_hits == 0
    finally:
        current_force_source_refresh.reset(token)
