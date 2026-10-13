"""Regression tests for RIF's domain-scoped funding-source catalogue."""
from pathlib import Path

import yaml

from core.schemas import ALLOWED_RESEARCH_AREAS


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

    # Keep the catalogue intentionally curated; additions require an explicit
    # domain mapping and a corresponding review of scope.
    assert len(entries) == 14
    assert all(names)
    assert all(urls)
    assert len(names) == len(set(names))
    assert len(urls) == len(set(urls))


def test_every_funding_source_is_mapped_to_canonical_rif_domains():
    entries = _entries()
    expected_domains = set(ALLOWED_RESEARCH_AREAS)
    assigned_domains = set()

    for entry in entries:
        domains = entry.get("research_domains")
        assert isinstance(domains, list) and domains, (
            f"{entry.get('name')} must declare research_domains"
        )
        invalid = set(domains) - expected_domains
        assert not invalid, f"{entry.get('name')} has non-canonical domains: {sorted(invalid)}"
        assigned_domains.update(domains)

    assert assigned_domains == expected_domains


def test_curated_sources_are_scoped_to_the_eight_target_areas():
    by_name = {entry["name"]: entry for entry in _entries()}
    expected = {
        "Ethereum ESP RFPs": (
            "https://esp.ethereum.foundation/applicants/rfp",
            {"RWA", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins"},
        ),
        "Ethereum ESP Open Rounds": (
            "https://esp.ethereum.foundation/applicants/open-rounds",
            {"RWA", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins"},
        ),
        "Chainlink Grants": (
            "https://chain.link/community/grants",
            {"RWA", "Stablecoins"},
        ),
        "Solana Grants and Funding": (
            "https://solana.org/grants-funding",
            {"RWA", "DePIN", "MEV", "Stablecoins"},
        ),
        "NEAR Ecosystem Funding": (
            "https://www.near.org/funding",
            {"RWA", "DID", "DePIN", "MEV", "Stablecoins"},
        ),
        "Ripple UBRI": (
            "https://ripple.com/impact/ubri/",
            {"RWA", "DID", "Stablecoins"},
        ),
        "XRPL Grants Program": (
            "https://xrplgrants.org/",
            {"RWA", "Stablecoins"},
        ),
        "Filecoin Grants and Funding": (
            "https://github.com/filecoin-project/devgrants",
            {"DePIN"},
        ),
        "Flashbots MEV Research Grants": (
            "https://github.com/flashbots/mev-research/blob/main/grants.md",
            {"MEV"},
        ),
        "Climate Pitch Climate-Tech Grants": (
            "https://climatepitch.org/home/",
            {"ESG"},
        ),
        "BIRAC Calls for Proposal": (
            "https://www.birac.nic.in/cfp.php",
            {"DigitalHealthCPS"},
        ),
        "ICMR Calls for Proposals": (
            "https://www.icmr.gov.in/call-for-proposals",
            {"DigitalHealthCPS"},
        ),
        "NSF CPS Foundations and Connected Communities": (
            "https://www.nsf.gov/funding/opportunities/cps-cyber-physical-system-foundations-connected-communities",
            {"ZK-IoV", "DigitalHealthCPS"},
        ),
        "I-DAPT Hub Cyber-Physical Systems Calls": (
            "https://idapthub.org/applications/call_for_proposals",
            {"ESG", "ZK-IoV", "DigitalHealthCPS"},
        ),
    }

    assert set(by_name) == set(expected)
    for name, (url, domains) in expected.items():
        entry = by_name[name]
        assert entry["url"].rstrip("/") == url.rstrip("/")
        assert set(entry["research_domains"]) == domains
        assert entry.get("collection", {}).get("mode") in {"web", "api"}


def test_broad_unscoped_portals_are_not_funding_roots():
    names = {str(entry.get("name", "")).casefold() for entry in _entries()}
    forbidden_names = {
        "meity startup hub",
        "idex defence innovation challenges",
        "anrf research funding",
        "dst india calls for proposals",
        "darpa research opportunities",
        "arpa-e",
        "grants.gov public opportunities api",
        "ethereum road to devcon 8 india university program",
        "polygon grants",
        "nist cps / iot testbed",
        "cisa ics",
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
