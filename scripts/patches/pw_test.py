import threading
import time

def test_pw():
    from playwright.sync_api import sync_playwright
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    print(f"Thread {threading.current_thread().name} starting pw")
    p = sync_playwright().start()
    b = p.chromium.launch(headless=True)
    page = b.new_page()
    page.goto("https://example.com")
    print(f"Thread {threading.current_thread().name} got title: {page.title()}")
    
threads = []
for i in range(2):
    t = threading.Thread(target=test_pw, name=f"Worker-{i}")
    threads.append(t)
    t.start()
    
for t in threads:
    t.join()
print("Done")
