import pytest
import urllib.error
import core.web_collection as web_collection

def test_authorized_redirect_allowed(monkeypatch):
    pages = {
        "https://polygon.technology/village/startups": "<html><title>Redirecting</title></html>",
        "https://hadronfc.com/": "<html><title>Hadron Founders Club</title><body>Apply now.</body></html>"
    }

    class Response:
        def __init__(self, url, text):
            self.url = url
            self.text = text
            self.content = text.encode()
            self.headers = {"Content-Type": "text/html"}

    def fake_get(url, **kwargs):
        if url == "https://polygon.technology/village/startups":
            return Response("https://hadronfc.com/", pages["https://hadronfc.com/"])
        elif url == "https://hadronfc.com/":
            return Response(url, pages[url])
        raise urllib.error.URLError("unavailable")

    monkeypatch.setattr(web_collection, "get_response", fake_get)
    monkeypatch.setattr(web_collection, "_robots_allowed", lambda *a, **k: True)

    result = web_collection.collect_source(
        "https://polygon.technology/village/startups",
        keywords=("apply",),
        allowed_domains=("hadronfc.com",)
    )

    assert len(result.pages) == 1
    assert result.pages[0].url == "https://hadronfc.com/"

def test_unauthorized_redirect_rejected(monkeypatch):
    pages = {
        "https://polygon.technology/village/startups": "<html><title>Redirecting</title></html>",
        "https://evil.example/": "<html><title>Evil</title><body>Apply now.</body></html>"
    }

    class Response:
        def __init__(self, url, text):
            self.url = url
            self.text = text
            self.content = text.encode()
            self.headers = {"Content-Type": "text/html"}

    def fake_get(url, **kwargs):
        if url == "https://polygon.technology/village/startups":
            return Response("https://evil.example/", pages["https://evil.example/"])
        elif url == "https://evil.example/":
            return Response(url, pages[url])
        raise urllib.error.URLError("unavailable")

    monkeypatch.setattr(web_collection, "get_response", fake_get)
    monkeypatch.setattr(web_collection, "_robots_allowed", lambda *a, **k: True)
    monkeypatch.setattr(web_collection, "_render_with_browser", lambda *a, **k: ("Evil", "Apply now.", [], "https://evil.example/"))

    result = web_collection.collect_source(
        "https://polygon.technology/village/startups",
        keywords=("apply",),
        allowed_domains=("hadronfc.com",) # authorized hadronfc, but it redirected to evil.example
    )

    assert len(result.pages) == 0
    assert any("redirected_to_untrusted_domain" in f["reason"] for f in result.failed_urls)
