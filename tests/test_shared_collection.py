import pytest
import concurrent.futures
import threading
import time
from pathlib import Path

from core.shared_crawl_manager import SharedCrawlManager, CrawlResult

@pytest.fixture
def mock_db(tmp_path):
    manager = SharedCrawlManager()
    manager.db_path = str(tmp_path / "shared_crawl_cache.db")
    manager._init_db()
    SharedCrawlManager._instance = manager
    return manager

def slow_fetch_func(url):
    time.sleep(0.1) # Simulate slow network
    return CrawlResult(url, url, 200, "text/html", "title", "html", "text", "hash", "http", False)

def test_a_passing_calls(mock_db, monkeypatch):
    monkeypatch.setattr(mock_db, "_do_http_fetch", lambda url: CrawlResult(url, url, 200, "text/html", "title", "html", "text", "hash", "http", False))
    mock_db.fetch("http://test.com")
    mock_db.fetch("http://test.com")
    mock_db.fetch("http://test.com")
        
    stats = mock_db.get_stats()
    assert stats["unique_urls_requested"] == 3
    assert stats["actual_network_fetches"] == 1
    assert stats["cache_hits"] == 2

def test_c_cache_hit(mock_db, monkeypatch):
    monkeypatch.setattr(mock_db, "_do_http_fetch", lambda url: CrawlResult(url, url, 200, "text/html", "title", "html", "text", "hash", "http", False))
    mock_db.fetch("http://test.com")
    mock_db.fetch("http://test.com")

def test_d_inflight_deduplication(mock_db, monkeypatch):
    call_count = [0]
    lock = threading.Lock()
    
    def fetch_spy(url):
        with lock:
            call_count[0] += 1
        return slow_fetch_func(url)
        
    monkeypatch.setattr(mock_db, "_do_http_fetch", fetch_spy)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=11) as executor:
        futures = [executor.submit(mock_db.fetch, "http://test.com") for _ in range(11)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
            
    assert call_count[0] == 1
    stats = mock_db.get_stats()
    assert stats["unique_urls_requested"] == 11
    assert stats["actual_network_fetches"] == 1
    assert stats.get("in_flight_deduplications", stats.get("cache_hits")) == 10

def test_f_fallback_playwright(mock_db, monkeypatch):
    def fetch_http_side_effect(url):
        return CrawlResult(url, url, 403, "text/html", "title", "error", "error", "hash", "http", False)
        
    monkeypatch.setattr(mock_db, "_do_http_fetch", fetch_http_side_effect)
    
    called_pw = [False]
    def fetch_pw_side_effect(url):
        called_pw[0] = True
        return CrawlResult("http://test.com", "http://test.com", 200, "text/html", "title", "html", "text", "hash", "playwright", False)
        
    monkeypatch.setattr(mock_db, "_do_browser_fetch", fetch_pw_side_effect)
    
    res = mock_db.fetch("http://test.com")
    assert res.fetch_method == "playwright"
    assert called_pw[0]

def test_g_fallback_skip(mock_db, monkeypatch):
    def fetch_http_side_effect(url):
        return CrawlResult(url, url, 404, "text/html", "title", "error", "error", "hash", "http", False)
        
    monkeypatch.setattr(mock_db, "_do_http_fetch", fetch_http_side_effect)
    
    called_pw = [False]
    def fetch_pw_side_effect(url):
        called_pw[0] = True
        return CrawlResult("http://test.com", "http://test.com", 200, "text/html", "title", "html", "text", "hash", "playwright", False)
        
    monkeypatch.setattr(mock_db, "_do_browser_fetch", fetch_pw_side_effect)
    
    res = mock_db.fetch("http://test.com")
    assert res.status_code == 404
    assert not called_pw[0]

def test_h_max_bounds(mock_db):
    assert mock_db.http_pool._max_workers == 15
    assert mock_db.browser_pool._max_workers == 3

def test_i_canonical_url(mock_db, monkeypatch):
    call_count = [0]
    def fetch_spy(url):
        call_count[0] += 1
        return CrawlResult(url, url, 200, "text/html", "title", "html", "text", "hash", "http", False)
        
    monkeypatch.setattr(mock_db, "_do_http_fetch", fetch_spy)
    
    mock_db.fetch("http://test.com#anchor")
    mock_db.fetch("http://test.com/")
        
    assert call_count[0] == 1

def test_j_sqlite_wal(mock_db):
    import sqlite3
    with sqlite3.connect(mock_db.db_path) as conn:
        res = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert res.lower() == "wal"

def test_l_pipeline_report(mock_db):
    assert "unique_urls_requested" in mock_db.get_stats()
