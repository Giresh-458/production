import pytest
import threading
from concurrent.futures import ThreadPoolExecutor
from core.crawl_context import CrawlContext

def test_crawl_context_thread_safety():
    ctx = CrawlContext()
    
    def worker():
        # Do a lot of increments
        for _ in range(100):
            with ctx.lock:
                ctx.browser_attempts += 1
                ctx.http_requests += 1
            ctx.add_page(1024)
            ctx.add_document()
            
    num_threads = 50
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker) for _ in range(num_threads)]
        for f in futures:
            f.result()
            
    expected = 100 * num_threads
    assert ctx.browser_attempts == expected, f"Expected {expected}, got {ctx.browser_attempts}"
    assert ctx.http_requests == expected, f"Expected {expected}, got {ctx.http_requests}"
    assert ctx.pages_fetched == expected, f"Expected {expected}, got {ctx.pages_fetched}"
    assert ctx.documents_collected == expected, f"Expected {expected}, got {ctx.documents_collected}"
    assert ctx.bytes_downloaded == expected * 1024, f"Expected {expected * 1024}, got {ctx.bytes_downloaded}"
