import pytest
import os
import pathlib
from core.web_collection import collect_source
from core.crawl_context import CrawlContext, current_crawl_context

def test_real_local_js():
    # We create a local HTML file
    test_html = """
    <html>
    <head><title>Test JS</title></head>
    <body>
    <noscript>javascript is required</noscript>
    <script>
    document.body.innerHTML = "<div id='rendered'><p>JS_RENDERED this is a test text that is long enough to bypass trafilatura threshold limits for content extraction and ensure that the js output is recorded properly.</p></div>";
    </script>
    </body>
    </html>
    """
    path = pathlib.Path("test_js.html").absolute()
    path.write_text(test_html, encoding="utf-8")
    
    url = path.as_uri()
    
    ctx = CrawlContext()
    token = current_crawl_context.set(ctx)
    try:
        # Collect source uses local file if uri given (via playwright usually failing for file://, but let's test it).
        # Actually web_collection get_response might not support file://
        # We can spin up a quick http server in a thread.
        import http.server
        import threading
        import socketserver
        
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, format, *args):
                pass
        
        with socketserver.TCPServer(("", 0), Handler) as httpd:
            port = httpd.server_address[1]
            t = threading.Thread(target=httpd.serve_forever)
            t.daemon = True
            t.start()
            
            test_url = f"http://127.0.0.1:{port}/test_js.html"
            result = collect_source(test_url, keywords=("rendered",))
            
            httpd.shutdown()
            
        assert len(result.pages) > 0
        assert "JS_RENDERED" in result.pages[0].text
        
        assert ctx.browser_attempts >= 1
        assert ctx.browser_successes >= 1
        
    finally:
        current_crawl_context.reset(token)
        path.unlink(missing_ok=True)
