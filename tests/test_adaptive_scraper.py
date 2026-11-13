from __future__ import annotations

from core import web_collection


def test_unknown_site_discovers_sitemap_and_relevant_deep_page(monkeypatch):
    pages = {
        "https://example.org/": '''<html><head><title>Home</title><link rel="alternate" type="application/xml" href="/sitemap.xml"></head><body><h1>Home Page Content</h1><h2>Welcome</h2><p>Welcome to example.org. This is a longer text to make sure the page is not mistakenly classified as a JS shell by the adaptive scraper threshold logic. We are ensuring the text exceeds the minimum limits. We have enough text here. And maybe a bit more just to be absolutely certain. It really needs to be quite long so that Trafilatura and BeautifulSoup don't discard it as boilerplate, and so the scraper thinks it is a real content page.</p><a href="/about">About</a><a href="/programs">Programs</a></body></html>''',
        "https://example.org/sitemap.xml": '''<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.org/programs/research-call-2026</loc></url></urlset>''',
        "https://example.org/programs/research-call-2026": '''<html><head><title>Research Call 2026</title></head><body><h1>Research Call 2026</h1><p>Applications close 30 September 2026.</p><p>We are making this text sufficiently long so that the scraper does not classify it as a JS shell. This is a very common issue when scraping small placeholder pages. It needs to have at least a few hundred characters to be considered a real page by our strict filters. This should be enough to satisfy it.</p><a href="/programs/research-call-2026.pdf">Full RFP</a></body></html>''',
    }

    class Response:
        def __init__(self, url, text, content_type="text/html"):
            self.url = url
            self.text = text
            self.content = text.encode()
            self.status_code = 200
            self.headers = {"Content-Type": content_type}

    def fake_get(url, **kwargs):
        if url not in pages:
            raise RuntimeError(url)
        return Response(url, pages[url], "application/xml" if url.endswith(".xml") else "text/html")

    monkeypatch.setattr(web_collection, "get_response", fake_get)
    monkeypatch.setattr(web_collection, "_robots_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr(web_collection, "wait_for_host_slot", lambda *args, **kwargs: True)
    monkeypatch.setattr(web_collection, "_extract_with_trafilatura", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_collection, "_render_with_browser", lambda *args, **kwargs: None)

    result = web_collection.collect_source("https://example.org/", keywords=("research", "call", "application", "deadline"), max_depth=2, max_pages=8)
    print("FAILED:", result.failed_urls)
    print("DISCOVERED:", result.discovered_urls)
    print("PAGES:", result.pages)
    urls = {page.url for page in result.pages}
    assert "https://example.org/" in urls
    assert "https://example.org/programs/research-call-2026" in urls


def test_link_scoring_prefers_funding_paths():
    strong = web_collection._score("https://example.org/research/rfp-2026", "Details", ("grant", "rfp", "deadline"))
    weak = web_collection._score("https://example.org/about", "About us", ("grant", "rfp", "deadline"))
    assert strong > weak


def test_recursive_sitemap_discovery(monkeypatch):
    from core import recursive_collection

    html = '<html><title>Home</title><body><a href="/about">About</a></body></html>'
    sitemap = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.org/rfp-2026.pdf</loc></url></urlset>'

    class Response:
        def __init__(self, text):
            self.text = text
            self.content = text.encode()
            self.status_code = 200
            self.url = "https://example.org/sitemap.xml"
            self.headers = {"Content-Type": "application/xml"}

    def fake_get(url, **kwargs):
        if url.endswith("sitemap.xml"):
            return Response(sitemap)
        raise RuntimeError(url)

    monkeypatch.setattr("core.http_client.get_response", fake_get)
    monkeypatch.setattr(recursive_collection, "check_robots", lambda *args, **kwargs: (True, None, 0.0))
    monkeypatch.setattr(recursive_collection, "wait_for_host_slot", lambda *args, **kwargs: True)
    assert "https://example.org/rfp-2026.pdf" in recursive_collection._sitemap_links("https://example.org/")

def test_collect_source_accepts_json_api_dataset_response(monkeypatch):
    payload = {
        "class": "dataset",
        "label": "Electricity supply, transformation and consumption - monthly data",
        "source": "Eurostat",
        "updated": "2026-10-22T23:00:00+0200",
        "dimension": {
            "geo": {"label": "Geopolitical entity"},
            "time": {"label": "Time"},
        },
        "value": {"0": 12.5, "1": 13.1},
    }

    class Response:
        url = "https://api.example.org/nrg_cb_em.json"
        text = __import__("json").dumps(payload)
        content = text.encode("utf-8")
        status_code = 200
        headers = {"Content-Type": "application/json; charset=utf-8"}

    monkeypatch.setattr(web_collection, "get_response", lambda *args, **kwargs: Response())
    monkeypatch.setattr(web_collection, "_robots_allowed", lambda *args, **kwargs: True)

    result = web_collection.collect_source(
        "https://api.example.org/nrg_cb_em.json",
        keywords=("electricity", "dataset", "consumption"),
        max_depth=0,
        max_pages=1,
    )

    assert result.failed_urls == []
    assert len(result.pages) == 1
    assert "Electricity supply" in result.pages[0].title
    assert '"class": "dataset"' in result.pages[0].text
    assert "consumption" in result.pages[0].text.lower()

