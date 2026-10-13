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
    assert len(entries) == 12
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


def test_curated_sources_are_scoped_to_the_seven_target_areas():
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
            "https://github.com/filecoin-project/devgrants/blob/master/Program%20Resources/Open%20Grants%20README.md",
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
        "NSF CPS Foundations and Connected Communities": (
            "https://www.nsf.gov/funding/opportunities/future-core-computer-information-science-engineering-future-computing/nsf25-543/solicitation",
            {"ZK-IoV"},
        ),
        "I-DAPT Hub Cyber-Physical Systems Calls": (
            "https://idapthub.org/applications/call_for_proposals",
            {"ESG", "ZK-IoV"},
        ),
    }

    assert set(by_name) == set(expected)
    for name, (url, domains) in expected.items():
        entry = by_name[name]
        assert entry["url"].rstrip("/") == url.rstrip("/")
        assert set(entry["research_domains"]) == domains
        assert entry.get("collection", {}).get("mode") in {"web", "api"}


def test_idapt_config_enables_bounded_docx_document_collection():
    by_name = {entry["name"]: entry for entry in _entries()}
    discovery = by_name["I-DAPT Hub Cyber-Physical Systems Calls"]["collection"]["discovery"]

    assert discovery["follow_documents"] is True
    # Recent live diagnostics discovered 12 URLs on this source. Keep a finite
    # budget but allow the discovered frontier to be inspected after fixing DOCX.
    assert 12 <= discovery["max_pages"] <= 30


def test_filecoin_source_uses_the_official_open_grants_program_page():
    by_name = {entry["name"]: entry for entry in _entries()}
    expected_url = (
        "https://github.com/filecoin-project/devgrants/blob/master/"
        "Program%20Resources/Open%20Grants%20README.md"
    )
    entry = by_name["Filecoin Grants and Funding"]
    assert entry["url"] == expected_url
    collection = entry["collection"]
    assert collection["mode"] == "web"
    discovery = collection["discovery"]
    assert discovery["start_urls"] == [expected_url]
    assert discovery["max_pages"] == 1
    assert discovery["follow_detail_pages"] is False


def test_near_page_budget_is_larger_than_the_previous_exhausted_cap():
    by_name = {entry["name"]: entry for entry in _entries()}
    discovery = by_name["NEAR Ecosystem Funding"]["collection"]["discovery"]
    assert discovery["max_pages"] == 20


def test_ripple_ubri_follows_discovered_pdf_evidence():
    by_name = {entry["name"]: entry for entry in _entries()}
    discovery = by_name["Ripple UBRI"]["collection"]["discovery"]
    assert discovery["follow_pdfs"] is True


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
        "birac calls for proposal",
        "icmr calls for proposals",
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
