"""Agent-fit scoring and same-site page tracing for the "Add source" page (offline)."""
from __future__ import annotations

from types import SimpleNamespace

from core.agent_fit import best_agents, candidate_links, fits_agent, score_agents, trace_pages


def _top(url, title, text):
    return score_agents(url, title, text)[0].agent


def test_typical_pages_score_for_the_expected_agent():
    assert _top("https://ukri.org/opportunity/", "Funding opportunities",
                "Apply for funding. Call for proposals open. Application deadline 4 March. Eligibility.") == "funding"
    assert _top("https://colosseum.com/hackathon", "Hackathon - Colosseum",
                "Global hackathons. Submission deadline. Prize pool and tracks for builders.") == "hackathon"
    assert _top("https://rekt.news/", "Rekt", "Exploit post-mortem: funds stolen via an attack vector; root cause.") == "failure"
    assert _top("https://github.com/w3c/vc-data-model", "w3c/vc-data-model",
                "Repository. Pull requests. Issues. Contributing. License.") == "opensource"
    assert _top("https://eur-lex.europa.eu/eli/reg/2023/1114/oj", "Regulation (EU) 2023/1114",
                "Regulation (EU) on markets in crypto-assets. Issuers shall comply with obligations. Supervisory authority.") == "regulation"


def test_open_source_only_scores_on_code_hosts():
    fits = {f.agent: f for f in score_agents("https://www.w3.org/TR/did-core/", "DID Core",
                                             "Open source. Issues. Pull requests. Repository. W3C Recommendation.")}
    assert fits["opensource"].score == 0


def test_article_pages_are_penalised_for_programme_agents():
    text = "Grants program and grant application lessons. Funding. Apply for funding. Request for proposals."
    article = {f.agent: f.score for f in score_agents("https://gitcoin.co/research/why-grants", "Why grants", text)}
    programme = {f.agent: f.score for f in score_agents("https://gitcoin.co/grants", "Grants", text)}
    assert programme["funding"] > article["funding"]


def test_too_little_text_is_undetermined():
    assert best_agents(score_agents("https://curve.fi/", "Curve", "Loading")) == []


def test_fits_agent_requires_absolute_and_relative_strength():
    fits = score_agents("https://example.org/", "Investor", "Our portfolio companies. Venture capital. We invest. Hackathon.")
    assert not fits_agent(fits, "hackathon")
    assert fits_agent(fits, "investment")


def test_candidate_links_prefer_agent_pages_on_the_same_site():
    html = ('<a href="/hackathon">Hackathon</a><a href="/portfolio">Portfolio</a>'
            '<a href="https://other.org/hackathon">Other site</a><a href="/login">Sign in</a>')
    links = candidate_links(html, "https://colosseum.com/", "hackathon")
    assert [link for link, _, _ in links] == ["https://colosseum.com/hackathon"]


def test_trace_pages_finds_the_better_page():
    pages = {
        "https://colosseum.com/": SimpleNamespace(final_url="https://colosseum.com/", title="Colosseum", error=None,
                                                  clean_text="Our portfolio. Venture capital. We invest. Hackathon.",
                                                  html='<a href="/hackathon">Hackathon</a>'),
        "https://colosseum.com/hackathon": SimpleNamespace(final_url="https://colosseum.com/hackathon", title="Hackathon",
                                                           error=None, html="",
                                                           clean_text="Hackathons. Submission deadline. Prize pool. Tracks."),
    }
    ranked = trace_pages("https://colosseum.com/", "hackathon", fetch=lambda url: pages[url])
    assert ranked[0].url == "https://colosseum.com/hackathon"
    assert ranked[0].score > ranked[1].score and ranked[1].note == "the page you gave"


def test_scan_site_reports_each_linked_page_with_its_own_agent():
    from core.agent_fit import scan_site

    pages = {
        "https://solana.org/": SimpleNamespace(final_url="https://solana.org/", title="The Solana Foundation", error=None,
                                               clean_text="Welcome.",
                                               html='<a href="/grants-funding">Grants and funding</a>'
                                                    '<a href="/hackathons">Hackathons</a><a href="/about">About</a>'),
        "https://solana.org/grants-funding": SimpleNamespace(final_url="https://solana.org/grants-funding", title="Grants and Funding",
                                                             error=None, html="",
                                                             clean_text="Grants program. Apply for funding. Request for proposals. Deadline."),
        "https://solana.org/hackathons": SimpleNamespace(final_url="https://solana.org/hackathons", title="Hackathons", error=None,
                                                         html="", clean_text="Global hackathons. Prize pool. Submission deadline."),
    }
    fetched = []

    def fetch(url):
        fetched.append(url)
        return pages[url]

    given, found, _notes = scan_site("https://solana.org/", fetch=fetch)
    assert given.agent is None  # "the URL you gave fits no agent"
    by_url = {f.url: f.agent for f in found}
    assert by_url["https://solana.org/grants-funding"] == "funding"
    assert by_url["https://solana.org/hackathons"] == "hackathon"
    assert "https://solana.org/about" not in fetched  # unrelated links are not crawled


def test_brand_names_and_partial_words_do_not_score():
    fits = {f.agent: f.score for f in score_agents("https://colosseum.com/companies/rekt", "Rekt", "Rekt is a portfolio company.")}
    assert fits["failure"] == 0
    invest = {f.agent: f.evidence for f in score_agents("https://ukri.org/x", "UKRI", "We fund innovation across the UK.")}
    assert not any("fund i" in e for e in invest["investment"])


def test_articles_and_case_studies_are_never_programme_suggestions():
    text = "Grants program. Grant application. Apply for funding. Request for proposals. Hackathon. Prize pool."
    for path in ("/research/why-grants", "/case-studies/gg24-retrospective", "/blog/2026/11/rounds"):
        fits = {f.agent: f.score for f in score_agents(f"https://gitcoin.co{path}", "Article", text)}
        assert fits["funding"] == 0 and fits["hackathon"] == 0, path
    assert {f.agent: f.score for f in score_agents("https://gitcoin.co/grants", "Grants", text)}["funding"] > 8


def test_domains_are_detected_only_with_enough_evidence():
    from core.agent_fit import detect_domains

    assert detect_domains("Carbon credits registry and MRV for emissions.")[0][0] == "ESG"
    assert detect_domains("We discuss settlement and reserve vaults.") == []  # generic words only
    assert detect_domains("Our platform is fast.") == []


def test_scan_findings_carry_detected_domains():
    from core.agent_fit import scan_site

    pages = {
        "https://x.org/": SimpleNamespace(final_url="https://x.org/", title="X", error=None, clean_text="Welcome.",
                                          html='<a href="/grants">Grants</a>'),
        "https://x.org/grants": SimpleNamespace(final_url="https://x.org/grants", title="Grants", error=None, html="",
                                                clean_text="Grants program. Apply for funding. Carbon credits and MRV."),
    }
    _given, found, _notes = scan_site("https://x.org/", fetch=lambda url: pages[url])
    assert found[0].agent == "funding" and found[0].domains[0][0] == "ESG"
