from __future__ import annotations
import urllib.error
from unittest.mock import patch
from core import web_collection

def test_sitemap_semantics(monkeypatch):
    pages = {
        # Seed URL
        "https://example.org/": '''<html><title>Home</title><body><h1>A</h1><h1>B</h1><a href="/about">About</a><a href="/programs">Programs</a>''' + "A" * 1000 + '''</body></html>''',
        # Sitemap index -> child sitemap
        "https://example.org/sitemap.xml": '''<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://example.org/sitemap_child.xml</loc></sitemap><sitemap><loc>https://example.org/malformed.xml</loc></sitemap></sitemapindex>''',
        "https://example.org/sitemap_child.xml": '''<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://example.org/programs/relevant-call</loc></url>
            <url><loc>https://example.org/irrelevant-stuff</loc></url>
            <url><loc>https://example.org/about</loc></url>
        </urlset>''',
        "https://example.org/malformed.xml": '''not xml''',
        
        # HTML pages
        "https://example.org/about": '''<html><head><title>About</title></head><body><h1>A</h1><h1>B</h1><p>We do things.</p>''' + "A" * 1000 + '''</body></html>''',
        "https://example.org/programs": '''<html><head><title>Programs</title></head><body><h1>A</h1><h1>B</h1><p>Our programs.</p>''' + "A" * 1000 + '''</body></html>''',
        "https://example.org/programs/relevant-call": '''<html><head><title>Research Call</title></head><body><h1>Research Call</h1><h2>A</h2><p>Applications close soon.</p><a href="/programs/relevant-call-deep">Deep</a>''' + "A" * 1000 + '''</body></html>''',
        "https://example.org/irrelevant-stuff": '''<html><head><title>No</title></head><body><h1>A</h1><h1>B</h1><p>Nothing here.</p>''' + "A" * 1000 + '''</body></html>''',
        "https://example.org/programs/relevant-call-deep": '''<html><head><title>Deep Call</title></head><body><h1>A</h1><h1>B</h1><p>Deep details.</p>''' + "A" * 1000 + '''</body></html>''',
    }

    class Response:
        def __init__(self, url, text, content_type="text/html"):
            self.url = url
            self.text = text
            self.content = text.encode()
            self.headers = {"Content-Type": content_type}

    def fake_get(url, **kwargs):
        if url not in pages:
            # Simulate unavailability for other things like robots.txt
            raise urllib.error.URLError("unavailable")
        return Response(url, pages[url], "application/xml" if url.endswith(".xml") else "text/html")

    monkeypatch.setattr(web_collection, "get_response", fake_get)
    monkeypatch.setattr(web_collection, "_robots_allowed", lambda *args, **kwargs: True)
    monkeypatch.setattr(web_collection, "_extract_with_trafilatura", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_collection, "_render_with_browser", lambda url, **kwargs: (pages.get(url, "a")[:10], pages.get(url, "a") * 30, [], url))
    
    # 1. Normal run
    result = web_collection.collect_source("https://example.org/", keywords=("research", "call", "application", "deadline"), max_depth=2, max_pages=10)
    urls = {page.url for page in result.pages}
    
    # Assertions
    assert "https://example.org/" in urls
    assert "https://example.org/programs/relevant-call" in urls # 1. sitemap XML -> relevant deep page, 2. sitemap index -> child -> relevant
    assert "https://example.org/irrelevant-stuff" not in urls # 3. irrelevant URLs are NOT selected
    # 4. malformed sitemap -> collector continues safely (no crash)
    
    # Also check if HTML crawling still works and deduplication
    assert "https://example.org/programs" in urls # Found via HTML
    
    # Check max_depth semantics for sitemap
    # /programs/relevant-call is at depth 0 conceptually from sitemap, pushes /programs/relevant-call-deep at depth 1.
    assert "https://example.org/programs/relevant-call-deep" in urls
    
    # 5. sitemap unavailable -> normal HTML crawling still works
    pages.pop("https://example.org/sitemap.xml")
    result_no_sitemap = web_collection.collect_source("https://example.org/", keywords=("research", "call", "application", "deadline"), max_depth=2, max_pages=10)
    urls_no_sitemap = {page.url for page in result_no_sitemap.pages}
    assert "https://example.org/programs/relevant-call" not in urls_no_sitemap
    assert "https://example.org/programs" in urls_no_sitemap

    # 7. max_pages still respected
    pages["https://example.org/sitemap.xml"] = '''<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.org/programs/relevant-call</loc></url></urlset>'''
    result_limited = web_collection.collect_source("https://example.org/", keywords=("research", "call", "application", "deadline"), max_depth=2, max_pages=1)
    urls_limited = {page.url for page in result_limited.pages}
    assert len(urls_limited) == 1
    assert "https://example.org/" in urls_limited
