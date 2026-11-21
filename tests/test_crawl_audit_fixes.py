"""Regression tests from the 2026-11-21 live crawl audit (robots.txt and anti-bot detection)."""
from __future__ import annotations

from core.antibot import is_challenge_page
from core.robots_policy import _parse_robots

UA = "RIF/3.1"


# ---------------------------------------------------------------- robots.txt (RFC 9309)
def test_empty_disallow_ends_the_wildcard_group():
    # devpost.com/robots.txt: the '*' group allows everything; the next group bans BLEXBot.
    # The parser used to merge both groups and deny every crawler.
    policy = _parse_robots("User-agent: *\nDisallow:\n\nUser-agent: BLEXBot\nDisallow: /\n")
    assert policy.decision(UA, "https://devpost.com/hackathons")[0] is True
    assert policy.decision("BLEXBot", "https://devpost.com/hackathons")[0] is False


def test_wildcard_rules_are_honoured():
    # airtable.com: 'Disallow: /app*/shr*' (urllib.robotparser ignores '*').
    policy = _parse_robots("User-agent: *\nDisallow: /app*/shr*\n")
    assert policy.decision(UA, "https://airtable.com/apppX/shrY/tblZ")[0] is False
    assert policy.decision(UA, "https://airtable.com/pricing")[0] is True


def test_wildcard_rules_do_not_over_block():
    # w3.org: '/blog/*/feed/' style rules must not deny /TR/ specs.
    policy = _parse_robots("User-agent: *\nDisallow: /*/wp-content/themes/\nDisallow: /blog/*/feed/\n")
    assert policy.decision(UA, "https://www.w3.org/TR/did-core/")[0] is True
    assert policy.decision(UA, "https://www.w3.org/blog/2020/feed/")[0] is False


def test_user_agent_groups_match_the_product_token_not_substrings():
    policy = _parse_robots("User-agent: R\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    assert policy.decision(UA, "https://example.org/a")[0] is True
    own = _parse_robots("User-agent: rif\nDisallow: /private\n\nUser-agent: *\nDisallow: /\n")
    assert own.decision(UA, "https://example.org/public")[0] is True
    assert own.decision(UA, "https://example.org/private/x")[0] is False


def test_funding_crawler_uses_the_rfc_parser(monkeypatch):
    import core.crawl4ai_web as crawl

    class Response:
        status_code = 202  # any 2xx is a successful robots.txt fetch
        text = "User-agent: *\nDisallow: /app*/shr*\n"

        def close(self):
            pass

    monkeypatch.setattr(crawl, "get_response", lambda *a, **k: Response())
    monkeypatch.setattr(crawl, "_wait_for_host_slot", lambda *a, **k: True)
    crawl._ROBOTS_CACHE.clear()
    try:
        assert crawl._get_robots_policy("https://airtable.example/apppX/shrY")[0] is False
        assert crawl._get_robots_policy("https://airtable.example/pricing")[0] is True
    finally:
        crawl._ROBOTS_CACHE.clear()


# ---------------------------------------------------------------- anti-bot detection
LONG_TEXT = "Bank for International Settlements research and statistics. " * 60


def test_normal_pages_with_cloudflare_scripts_and_challenge_wording_are_not_challenges():
    html = ("<html><head><title>Welcome to the BIS</title>"
            "<script src='https://cdnjs.cloudflare.com/ajax/libs/x.js'></script>"
            "<script>a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'</script></head>"
            f"<body>{LONG_TEXT} Hackathon challenge tracks.</body></html>")
    assert not is_challenge_page(html, "Welcome to the BIS", LONG_TEXT + " Hackathon challenge tracks.", status=200)


def test_proof_of_personhood_content_is_not_a_challenge():
    text = "World ID lets you verify you are human without revealing identity. " * 40
    assert not is_challenge_page(f"<p>{text}</p>", "World ID", text, status=200)


def test_real_challenge_pages_are_detected():
    assert is_challenge_page("<script>window._cf_chl_opt={}</script>", "Just a moment...", "Enable JavaScript and cookies to continue", status=403)
    assert is_challenge_page("<html></html>", "Making sure you're not a bot!", "Loading...", status=202)  # dblp (Anubis)
    assert is_challenge_page("<html></html>", "Human Verification", "Please verify", status=405)  # dorahacks
    assert is_challenge_page("", "", "", status=202)  # empty AWS WAF 202 (binance)
    assert is_challenge_page("<p>x</p>", "Page", "x", status=429, headers={"X-Vercel-Mitigated": "challenge"})
    assert is_challenge_page("<p>x</p>", "Page", "x", status=200, headers={"cf-mitigated": "challenge"})


# ---------------------------------------------------------------- focused extraction
def test_focused_content_keeps_best_blocks_of_long_pages_not_just_the_opening():
    from core.focused_extraction import build_focused_content

    opening = [f"Recital {i}: Member States shall cooperate on general matters of the internal market." for i in range(40)]
    key = "Article 48: issuers of e-money tokens shall comply with the reserve requirements for stablecoins."
    text = build_focused_content(title="Law", blocks=opening + [key], focus_keywords=["shall", "requirements", "stablecoins"],
                                 max_blocks=6, neighbor_window=0)
    assert "e-money tokens" in text
    assert "Recital 0" in text  # the opening (scope) is kept too


def test_focused_content_is_unchanged_for_short_pages():
    from core.focused_extraction import build_focused_content

    blocks = ["Intro paragraph about tokenization requirements.", "Second block: compliance must be shown.", "short"]
    text = build_focused_content(title="T", blocks=blocks, focus_keywords=["requirements", "must", "compliance"], min_score=1)
    # Output of the original (pre-fix) implementation for the same input.
    assert text == "T Intro paragraph about tokenization requirements. Second block: compliance must be shown. short"


def test_mica_terms_count_as_stablecoin_evidence():
    from core.schemas import AREA_KEYWORDS

    assert {"e-money token", "asset-referenced token"} <= set(AREA_KEYWORDS["Stablecoins"])


def test_eu_publications_office_xhtml_uses_the_real_title():
    from agents.regulation_agent import extract_page_text

    html = ("<html><head><title>L_2023150EN.01004001.xml</title></head><body>"
            "<div class='eli-main-title'><p>REGULATION (EU) 2023/1114 on markets in crypto-assets</p></div>"
            "<p>Issuers of asset-referenced tokens shall comply with requirements on reserves of assets.</p></body></html>")
    assert extract_page_text(html)[0] == "REGULATION (EU) 2023/1114 on markets in crypto-assets"


# ---------------------------------------------------------------- charset
class _Resp:
    def __init__(self, body: bytes, content_type: str):
        import requests

        self._r = requests.Response()
        self._r._content = body
        self._r.headers["Content-Type"] = content_type
        self._r.encoding = requests.utils.get_encoding_from_headers(self._r.headers)


def test_utf8_page_without_charset_header_is_not_garbled():
    from core.http_client import _fix_missing_charset

    r = _Resp("Colosseum’s hackathons — 🏆".encode("utf-8"), "text/html")._r
    assert r.encoding == "ISO-8859-1"  # requests' default, which garbled 18 configured pages
    _fix_missing_charset(r)
    assert r.text == "Colosseum’s hackathons — 🏆"


def test_meta_charset_and_explicit_header_win():
    from core.http_client import _fix_missing_charset

    r = _Resp('<meta charset="windows-1252"><p>caf\xe9</p>'.encode("cp1252"), "text/html")._r
    _fix_missing_charset(r)
    assert "café" in r.text
    explicit = _Resp("abc".encode("utf-8"), "text/html; charset=ISO-8859-1")._r
    _fix_missing_charset(explicit)
    assert explicit.encoding == "ISO-8859-1"


# ---------------------------------------------------------------- official routes for challenged sites
def test_rss_handler_reads_official_feeds(monkeypatch):
    import core.robots_policy as robots
    import core.structured_sources as ss

    feed = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>Carbon Market Watch</title>'
            b"<item><title>Offsets audit</title><link>https://carbonmarketwatch.org/a</link>"
            b"<pubDate>Mon, 16 Nov 2026</pubDate><description>&lt;p&gt;Double counting found&lt;/p&gt;</description></item>"
            b"</channel></rss>")

    class R:
        content = feed

        def close(self):
            pass

    monkeypatch.setattr(robots, "check_robots", lambda url: (True, None, 0.0))
    monkeypatch.setattr(ss, "get_response", lambda *a, **k: R())
    result = ss._rss("https://carbonmarketwatch.org/feed/")
    assert result.title == "Carbon Market Watch"
    assert "Offsets audit" in result.text and "Double counting found" in result.text
    assert ss._rss("https://carbonmarketwatch.org/publications/") is None


def test_iso_open_data_handler_uses_cached_committee_rows(tmp_path, monkeypatch):
    import json

    import core.structured_sources as ss

    monkeypatch.setenv("RIF_ISO_CACHE_DIR", str(tmp_path))
    (tmp_path / "committee_6266604.json").write_text(json.dumps({
        "heading": "ISO/TC 307 Blockchain and distributed ledger technologies",
        "scope": "Standardisation of blockchain technologies.",
        "rows": [
            {"reference": "ISO 22739:2024", "title": "Vocabulary", "status": "published", "publicationDate": "2024-01-01", "scope": "Terms."},
            {"reference": "ISO 22739:2020", "title": "Vocabulary", "status": "withdrawn", "publicationDate": "2020-07-13", "scope": "Old."},
        ],
    }), encoding="utf-8")
    result = ss._iso_open_data("https://www.iso.org/committee/6266604.html")
    assert result.title.startswith("ISO/TC 307")
    assert "ISO 22739:2024" in result.text and "ISO 22739:2020" not in result.text  # withdrawn excluded
    assert ss._iso_open_data("https://example.org/committee/1.html") is None


def test_iso_stage_codes():
    from core.structured_sources import _iso_stage

    assert _iso_stage(6060) == "published"
    assert _iso_stage(9093) == "published"
    assert _iso_stage(9599) == "withdrawn"
    assert _iso_stage(3020) == "under development"


def test_challenged_sources_use_official_routes():
    import yaml

    def urls(fname, root):
        data = yaml.safe_load(open(f"sources/{fname}", encoding="utf-8"))[root]
        return {e["name"]: e["url"] for sec in data.values() for e in sec}

    reg = urls("regulation_sources.yaml", "regulation_sources")
    assert reg["SEC Climate Disclosure"] == "https://www.sec.gov/news/pressreleases.rss"
    assert reg["ISO/SAE 21434"] == "https://www.iso.org/standard/70918.html"
    assert urls("failure_sources.yaml", "failure_sources")["Carbon Market Watch"] == "https://carbonmarketwatch.org/feed/"
    assert urls("hackathon_sources.yaml", "hackathon_sources")["Solana Hackathons"] == "https://arena.colosseum.org/hackathon"
