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


def test_vercel_checkpoint_is_not_sent_to_browser_fallback(monkeypatch):
    html = (
        "<html><title>Just a moment...</title><body>"
        "Vercel Security Checkpoint. We're verifying your browser."
        "</body></html>"
    )

    class Response:
        url = "https://example.com/vercel"
        text = html
        headers = {"Content-Type": "text/html"}
        content = html.encode()

    monkeypatch.setattr("core.web_collection.get_response", lambda *a, **k: Response())
    monkeypatch.setattr("core.web_collection._robots_allowed", lambda *a, **k: True)
    monkeypatch.setattr(
        "core.web_collection._render_with_browser",
        lambda *a, **k: pytest.fail("anti-bot challenge must not trigger browser fallback"),
    )

    result = collect_source("https://example.com/vercel", keywords=("test",))
    assert result.pages == []
    assert any("anti-bot" in str(item["reason"]) for item in result.failed_urls)
