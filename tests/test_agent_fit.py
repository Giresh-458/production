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
    text = "Grants program and grant application lessons. Funding."
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
