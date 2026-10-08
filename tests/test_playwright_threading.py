import threading
import time
from core.web_collection import _render_with_browser

def test_playwright_multiple_worker_threads():
    results = []
    errors = []

    def worker():
        try:
            # We don't want to actually hit the network in a unit test if possible,
            # but we need to verify thread safety. We can hit example.com.
            res = _render_with_browser("https://example.com")
            results.append(res)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Errors occurred: {errors}"
    assert len(results) == 3
    assert all(r is not None for r in results)
