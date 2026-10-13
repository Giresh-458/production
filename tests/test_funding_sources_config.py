"""Regression checks for the curated funding-source catalogue."""
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO_ROOT / "sources" / "funding_sources.yaml"


def _catalogue():
    return yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")) or {}


def _entries():
    sources = _catalogue().get("sources") or {}
    return [
        entry
        for tier_entries in sources.values()
        if isinstance(tier_entries, list)
        for entry in tier_entries
        if isinstance(entry, dict)
    ]


def test_funding_source_catalogue_has_unique_names_and_urls():
    entries = _entries()
    names = [str(entry.get("name", "")).strip().casefold() for entry in entries]
    urls = [str(entry.get("url", "")).strip().rstrip("/").casefold() for entry in entries]

    assert len(entries) >= 15
    assert all(names)
    assert all(urls)
    assert len(names) == len(set(names))
    assert len(urls) == len(set(urls))


def test_priority_official_and_india_relevant_sources_are_configured():
    by_name = {entry["name"]: entry for entry in _entries()}
    expected = {
        "Ethereum ESP RFPs": "https://esp.ethereum.foundation/applicants/rfp",
        "Ethereum ESP Open Rounds": "https://esp.ethereum.foundation/applicants/open-rounds",
        "Chainlink Grants": "https://chain.link/community/grants",
        "Solana Grants and Funding": "https://solana.org/grants-funding",
        "XRPL Grants Program": "https://xrplgrants.org/",
        "Filecoin Grants and Funding": "https://github.com/filecoin-project/devgrants",
        "MeitY Startup Hub": "https://msh.meity.gov.in/",
        "BIRAC Calls for Proposal": "https://www.birac.nic.in/cfp.php",
        "iDEX Defence Innovation Challenges": "https://www.idex.gov.in/challenges",
        "ANRF Research Funding": "https://anrfonline.in/ANRF/HomePage",
        "DST India Calls for Proposals": "https://onlinedst.gov.in/",
        "DARPA Research Opportunities": "https://www.darpa.mil/work-with-us/opportunities",
        "Grants.gov Public Opportunities API": "https://api.grants.gov/v1/api/search2",
    }

    for name, url in expected.items():
        assert name in by_name, f"Missing source: {name}"
        assert by_name[name]["url"].rstrip("/") == url.rstrip("/")

    assert by_name["Chainlink Grants"]["collection"]["mode"] == "web"
    assert by_name["Grants.gov Public Opportunities API"]["collection"]["limits"]["max_pages"] >= 5
    assert by_name["Filecoin Grants and Funding"]["collection"]["limits"]["max_pages"] >= 3


def test_information_only_or_known_stale_sources_are_not_treated_as_funding_roots():
    names = {str(entry.get("name", "")).casefold() for entry in _entries()}
    forbidden_names = {
        "ethereum foundation esp",
        "polygon grants",
        "nist cps / iot testbed",
        "cisa ics",
        "nsf cyber physical and intelligent systems",
    }
    assert names.isdisjoint(forbidden_names)


def test_web_sources_have_bounded_discovery_depth_and_page_count():
    for entry in _entries():
        collection = entry.get("collection") or {}
        if collection.get("mode") != "web":
            continue
        discovery = collection.get("discovery") or {}
        assert discovery.get("max_pages") is not None, (
            f"{entry.get('name')} is missing a configured page cap"
        )
        assert 1 <= int(discovery["max_pages"]) <= 30
        assert 0 <= int(discovery.get("max_depth", 0)) <= 4
