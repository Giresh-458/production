"""User-added sources: written in each agent's own format, read by the agent's own loader."""
from __future__ import annotations

import importlib
import shutil
from pathlib import Path

import pytest
import yaml

from core import user_sources as us

PROJECT_SOURCES = Path(__file__).resolve().parents[1] / "sources"


@pytest.fixture()
def sources_dir(tmp_path):
    target = tmp_path / "sources"
    shutil.copytree(PROJECT_SOURCES, target)
    return target


def _url_for(agent: str) -> str:
    return "https://github.com/example-org/example-repo" if agent == "opensource" else f"https://example-{agent}.org/programme"


@pytest.mark.parametrize("agent", list(us.AGENT_FILES))
def test_added_source_is_loaded_by_the_agents_own_loader(sources_dir, agent):
    path = us.add_user_source(agent, f"Test {agent}", _url_for(agent), ["RWA", "DID"], sources_dir=sources_dir)
    module = importlib.import_module({"lab": "agents.lab_agent"}.get(agent, f"agents.{agent}_agent"))
    loaded = [s for s in module.load_sources(path) if getattr(s, "url", "") == _url_for(agent)]
    assert len(loaded) == 1


def test_funding_entry_matches_curated_shape_and_is_bounded(sources_dir):
    path = us.add_user_source("funding", "Example Grants", "example.org/grants", ["ESG"], sources_dir=sources_dir)
    entries = yaml.safe_load(path.read_text(encoding="utf-8"))["sources"]["user_added"]
    entry = entries[0]
    assert entry["url"] == "https://example.org/grants"
    assert entry["research_domains"] == ["ESG"]
    assert entry["user_added"] is True and entry["added_at"]
    # The start page is classified like any other page, not forced to be a programme record.
    assert entry["direct_page"] is False
    discovery = entry["collection"]["discovery"]
    assert entry["collection"]["allowed_domains"] == ["example.org"]
    assert 1 <= discovery["max_pages"] <= 30 and discovery["max_depth"] <= 2


def test_company_source_goes_to_its_domain_section(sources_dir):
    path = us.add_user_source("company", "Example Co", "https://example.com", ["DePIN"], sources_dir=sources_dir)
    section = yaml.safe_load(path.read_text(encoding="utf-8"))["companies"]["depin"]
    assert any(e.get("user_added") and e["name"] == "Example Co" for e in section)


def test_remove_restores_the_file_byte_for_byte(sources_dir):
    before = {p.name: p.read_text(encoding="utf-8") for p in sources_dir.glob("*_sources.yaml")}
    for agent in us.AGENT_FILES:
        us.add_user_source(agent, f"Test {agent}", _url_for(agent), ["MEV"], sources_dir=sources_dir)
    assert len(us.list_user_sources(sources_dir)) == len(us.AGENT_FILES)
    for row in us.list_user_sources(sources_dir):
        us.remove_user_source(row["agent"], row["name"], sources_dir=sources_dir)
    after = {p.name: p.read_text(encoding="utf-8") for p in sources_dir.glob("*_sources.yaml")}
    assert {k: v.rstrip("\n") for k, v in after.items()} == {k: v.rstrip("\n") for k, v in before.items()}


def test_curated_sources_cannot_be_removed(sources_dir):
    with pytest.raises(us.UserSourceError):
        us.remove_user_source("funding", "Ripple UBRI", sources_dir=sources_dir)


def test_duplicates_and_missing_domains_are_rejected(sources_dir):
    with pytest.raises(us.UserSourceError):
        us.add_user_source("funding", "Ripple again", "https://ripple.com/impact/ubri", ["RWA"], sources_dir=sources_dir)
    with pytest.raises(us.UserSourceError):
        us.add_user_source("funding", "No domain", "https://example.org", [], sources_dir=sources_dir)


def test_failed_validation_leaves_the_file_unchanged(sources_dir, monkeypatch):
    path = sources_dir / "funding_sources.yaml"
    original = path.read_text(encoding="utf-8")
    monkeypatch.setattr(us, "_validate_with_agent_loader", lambda agent, p: (_ for _ in ()).throw(RuntimeError("bad")))
    with pytest.raises(RuntimeError):
        us.add_user_source("funding", "Example", "https://example.org", ["ESG"], sources_dir=sources_dir)
    assert path.read_text(encoding="utf-8") == original


def test_url_detection():
    assert us.looks_like_url("https://gitcoin.co/grants")
    assert us.looks_like_url("gitcoin.co")
    assert not us.looks_like_url("Gitcoin grants")
    assert us.normalize_url("gitcoin.co") == "https://gitcoin.co"


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _WikidataSession:
    def get(self, url, params=None, **kwargs):
        if params["action"] == "wbsearchentities":
            return _Response({"search": [{"id": "Q1"}, {"id": "Q2"}]})
        return _Response({"entities": {
            "Q1": {"labels": {"en": {"value": "Gitcoin"}}, "descriptions": {"en": {"value": "funding platform"}},
                   "claims": {"P856": [{"mainsnak": {"datavalue": {"value": "https://gitcoin.co"}}}]}},
            "Q2": {"labels": {"en": {"value": "No website"}}, "claims": {}},
        }})


def test_name_lookup_only_returns_stated_official_websites():
    found = us.find_url_candidates("Gitcoin", session=_WikidataSession())
    assert found == [{"label": "Gitcoin", "description": "funding platform", "url": "https://gitcoin.co",
                      "wikidata": "https://www.wikidata.org/wiki/Q1"}]


class _PageResponse:
    status_code = 200
    url = "https://example.org/grants"
    text = "<title>Example Grants</title><p>Stablecoin payment rails and stablecoins research.</p>"


class _PageSession:
    def get(self, *args, **kwargs):
        return _PageResponse()


def test_inspection_reports_title_domains_and_cross_agent_duplicates(monkeypatch):
    import core.crawl4ai_web as crawl

    monkeypatch.setattr(crawl, "_get_robots_policy", lambda url: (True, 0.0, None))
    monkeypatch.setattr(us, "configured_urls", lambda: {us._url_key("https://example.org/grants"): ["Hackathon: Example"]})
    result = us.inspect_url("https://example.org/grants", "funding", session=_PageSession(), domains=["ESG"])
    assert result.title == "Example Grants"
    assert [d for d, _ in result.domain_evidence] == ["Stablecoins"]
    assert result.problems == []  # another agent using the URL is only a warning
    assert any("another agent" in w for w in result.warnings)
    assert any("domains you picked" in w for w in result.warnings)
    blocked = us.inspect_url("https://example.org/grants", "hackathon", session=_PageSession())
    assert any("Already configured for this agent" in p for p in blocked.problems)
