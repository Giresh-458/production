import contextvars
import time
from core.crawl_context import CrawlContext, current_crawl_context
from core.shared_crawl_manager import SharedCrawlManager
from core.web_collection import WebPage

def test_context_propagation_to_pools():
    ctx = CrawlContext(deadline=time.time() + 10.0)
    token = current_crawl_context.set(ctx)
    
    # Check if context propagates to worker threads using ThreadPoolExecutor
    manager = SharedCrawlManager()
    try:
        # Mock actual HTTP fetch to just read from current context
        def fake_fetch(*args):
            c = current_crawl_context.get(None)
            if c:
                c.http_requests += 1
                return c.http_requests
            return -1
            
        future = manager.http_pool.submit(contextvars.copy_context().run, fake_fetch)
        res = future.result(timeout=5.0)
        assert res == 1
        assert ctx.http_requests == 1
    finally:
        current_crawl_context.reset(token)
