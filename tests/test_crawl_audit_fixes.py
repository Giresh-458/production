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
