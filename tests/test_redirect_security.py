from core import web_collection


class Response:
    def __init__(self, url, text="", status_code=200, headers=None):
        self.url = url
        self.text = text
        self.content = text.encode()
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "text/html"}
        self.closed = False

    def close(self):
        self.closed = True


def _patch_policy(monkeypatch):
    monkeypatch.setattr(web_collection, "_robots_allowed", lambda *a, **k: True)
    monkeypatch.setattr(web_collection, "check_robots", lambda *a, **k: (True, None, 0.0))
    monkeypatch.setattr(web_collection, "wait_for_host_slot", lambda *a, **k: True)
    monkeypatch.setattr(web_collection, "get_robots_sitemaps", lambda *a, **k: [])
    monkeypatch.setattr(web_collection, "_extract_discovery_urls", lambda *a, **k: [])


def test_authorized_redirect_allowed(monkeypatch):
    pages = {
        "https://hadronfc.com/": "<html><head><title>Hadron</title></head><body><h1>Hadron</h1><p>Particle physics research.</p></body></html>"
    }
    requested = []

    def fake_get(url, **kwargs):
        requested.append(url)
        if url == "https://polygon.technology/":
            return Response(
                url,
                status_code=302,
                headers={"Location": "https://hadronfc.com/"},
            )
        return Response(url, pages[url])

    monkeypatch.setattr(web_collection, "get_response", fake_get)
    _patch_policy(monkeypatch)

    result = web_collection.collect_source(
        "https://polygon.technology/",
        keywords=("research", "particle"),
        allowed_domains={"hadronfc.com"},
        max_depth=0,
        max_pages=1,
    )

    assert requested == ["https://polygon.technology/", "https://hadronfc.com/"]
    assert any(page.url == "https://hadronfc.com/" for page in result.pages)


def test_unauthorized_redirect_rejected(monkeypatch):
    requested = []

    def fake_get(url, **kwargs):
        requested.append(url)
        return Response(
            url,
            status_code=302,
            headers={"Location": "https://evil.example/"},
        )

    monkeypatch.setattr(web_collection, "get_response", fake_get)
    _patch_policy(monkeypatch)

    result = web_collection.collect_source(
        "https://polygon.technology/",
        keywords=("research",),
        allowed_domains={"hadronfc.com"},
        max_depth=0,
        max_pages=1,
    )

    assert requested == ["https://polygon.technology/"]
    assert any(
        "redirected_to_untrusted_domain" in str(item.get("reason", ""))
        for item in result.failed_urls
    )

@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/private",
        "http://localhost/private",
    ],
)
def test_web_collection_rejects_private_root_before_network(monkeypatch, url):
    requested = []
    monkeypatch.setattr(
        web_collection,
        "get_response",
        lambda target, **kwargs: requested.append(target),
    )
    _patch_policy(monkeypatch)

    result = web_collection.collect_source(
        url,
        keywords=(),
        max_depth=0,
        max_pages=1,
    )

    assert result.pages == []
    assert requested == []

