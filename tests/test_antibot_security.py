import pytest
from core.web_collection import collect_source

def test_vercel_security_checkpoint_rejected(monkeypatch):
    class Response:
        def __init__(self, url, text):
            self.url = url
            self.text = text
            self.headers = {"Content-Type": "text/html"}
    
    def fake_get(*a, **k):
        return Response("https://example.com/vercel", "<html><title>Just a moment...</title><body>Vercel Security Checkpoint. We're verifying your browser.</body></html>")
    
    monkeypatch.setattr("core.web_collection.get_response", fake_get)
    monkeypatch.setattr("core.web_collection._robots_allowed", lambda *a, **k: True)
    monkeypatch.setattr("core.web_collection._render_with_browser", lambda *a, **k: ("Just a moment...", "Vercel Security Checkpoint. We're verifying your browser. And here is some extra text to simulate the browser receiving the full challenge page, making it longer than the initial static response.", [], "https://example.com/vercel"))
    
    result = collect_source("https://example.com/vercel", keywords=("test",))
    print("FAILED URLS:", result.failed_urls)
    assert len(result.pages) == 0
    assert any("anti-bot" in str(f["reason"]) for f in result.failed_urls)
