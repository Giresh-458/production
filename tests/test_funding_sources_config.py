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


def test_funding_sources_yaml_has_no_duplicate_top_level_source_groups():
    # PyYAML silently keeps only the last value for a duplicate mapping key.
    # That can make valid source definitions disappear without a parse error.
    import re

    text = CATALOG_PATH.read_text(encoding="utf-8")
    groups = re.findall(r"^  ([A-Za-z0-9_-]+):\s*$", text, flags=re.MULTILINE)
    assert groups
    assert len(groups) == len(set(groups)), (
        "Duplicate top-level source groups would be silently overwritten by YAML parsing"
    )


def test_funding_source_catalogue_has_unique_names_and_urls():
    entries = _entries()
    names = [str(entry.get("name", "")).strip().casefold() for entry in entries]
    urls = [str(entry.get("url", "")).strip().rstrip("/").casefold() for entry in entries]

    # Keep the catalogue intentionally curated; additions require an explicit
    # domain mapping and a corresponding review of scope.
    assert len(entries) == 15
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
        "Ethereum ESP RFPs": ("https://esp.ethereum.foundation/applicants/rfp", {"RWA", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins"}),
        "Ethereum ESP Open Rounds": ("https://esp.ethereum.foundation/applicants/open-rounds", {"RWA", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins"}),
        "Solana Grants and Funding": ("https://solana.org/grants-funding", {"RWA", "DePIN", "MEV", "Stablecoins"}),
        "Superteam Development Grants": ("https://superteam.fun/earn/opportunities/development-grants/", {"RWA", "DePIN", "MEV", "Stablecoins"}),
        "NEAR Ecosystem Funding": ("https://near.org/funding", {"RWA", "DID", "DePIN", "MEV", "Stablecoins"}),
        "Ripple UBRI": ("https://ripple.com/impact/ubri/", {"RWA", "DID", "Stablecoins"}),
        "XRPL Grants Program": ("https://xrplgrants.org/", {"RWA", "Stablecoins"}),
        "Filecoin Grants and Funding": ("https://fil.org/grants", {"DePIN"}),
        "Flashbots MEV Research Grants": ("https://raw.githubusercontent.com/flashbots/mev-research/main/grants.md", {"MEV"}),
        "Climate KIC Open Calls": ("https://www.climate-kic.org/get-involved/open-calls/", {"ESG"}),
        "NSF SBIR Trusted Identity": ("https://www.sbir.gov/topics/1012406", {"DID"}),
        "NSF SBIR Climate Change Adaptation and Mitigation": ("https://www.sbir.gov/topics/1012419", {"ESG"}),
        "Open SBIR Funding Topics": ("https://www.sbir.gov/topics?status=Open", {"RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins"}),
        "EU Open and Forthcoming Funding Calls": ("https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/calls-for-proposals?isExactMatch=false&order=DESC&pageNumber=1&pageSize=50&sortBy=startDate&status=31094501%2C31094502", {"RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins"}),
        "I-DAPT Hub Cyber-Physical Systems Calls": ("https://www.idapthub.org/applications/call_for_proposals", {"ESG", "ZK-IoV"}),
    }

    assert set(by_name) == set(expected)
    for name, (url, domains) in expected.items():
        entry = by_name[name]
        assert entry["url"].rstrip("/") == url.rstrip("/")
        assert set(entry["research_domains"]) == domains
        assert entry.get("collection", {}).get("mode") in {"web", "api"}


def test_eu_funding_api_has_bounded_budget_for_all_targeted_queries():
    by_name = {entry["name"]: entry for entry in _entries()}
    source = by_name["EU Open and Forthcoming Funding Calls"]
    limits = source["collection"]["limits"]
    queries = source["collection"]["api"]["search_texts"]

    # Production allocates a meaningful per-query budget; the total remains finite
    # and the API adapter must report PARTIAL when any individual query is capped.
    assert len(queries) == 10
    assert limits["max_pages"] >= len(queries) * 10
    assert limits["max_documents"] >= limits["max_pages"] * 50


def test_idapt_config_enables_bounded_docx_document_collection():
    by_name = {entry["name"]: entry for entry in _entries()}
    entry = by_name["I-DAPT Hub Cyber-Physical Systems Calls"]
    discovery = entry["collection"]["discovery"]

    assert entry["collection"]["status_verification_urls"] == [entry["url"]]
    assert discovery["follow_documents"] is True
    # Recent live diagnostics discovered 12 URLs on this source. Keep a finite
    # budget but allow the discovered frontier to be inspected after fixing DOCX.
    assert 12 <= discovery["max_pages"] <= 30


def test_idapt_stale_awards_and_historical_fellows_are_excluded():
    by_name = {entry["name"]: entry for entry in _entries()}
    discovery = by_name["I-DAPT Hub Cyber-Physical Systems Calls"]["collection"]["discovery"]
    patterns = discovery.get("exclude_url_patterns", [])
    assert any("idapthub" in pattern and "/awards" in pattern for pattern in patterns)

    import re
    historical_fellows_url = "https://idapthub.org/portfolio/chanakya_fellowships"
    assert any(re.search(pattern, historical_fellows_url) for pattern in patterns)


def test_filecoin_source_crawls_program_details_and_official_grants_directory():
    by_name = {entry["name"]: entry for entry in _entries()}
    official_url = "https://fil.org/grants"
    readme_url = (
        "https://raw.githubusercontent.com/filecoin-project/devgrants/master/"
        "Program%20Resources/Open%20Grants%20README.md"
    )
    next_step_url = (
        "https://raw.githubusercontent.com/filecoin-project/devgrants/master/"
        "Program%20Resources/Builder%20Next%20Step%20Grants.md"
    )
    entry = by_name["Filecoin Grants and Funding"]
    assert entry["url"] == official_url
    collection = entry["collection"]
    assert collection["mode"] == "web"
    assert entry.get("direct_page") is False
    assert {"raw.githubusercontent.com", "fil.org"}.issubset(
        set(collection["allowed_domains"])
    )
    assert "ipfsgrants.io" not in collection["allowed_domains"]
    discovery = collection["discovery"]
    assert discovery["start_urls"] == [
        official_url,
        readme_url,
        next_step_url,
    ]
    assert discovery["max_depth"] == 3
    assert discovery["max_pages"] == 30
    assert discovery["direct_page_urls"] == [readme_url, next_step_url]
    assert discovery["follow_detail_pages"] is True
    assert any("devgrants/" in pattern and "milestones" in pattern for pattern in discovery["exclude_url_patterns"])

def test_flashbots_markdown_source_uses_bounded_raw_content_endpoint():
    by_name = {entry["name"]: entry for entry in _entries()}
    name = "Flashbots MEV Research Grants"
    url = "https://raw.githubusercontent.com/flashbots/mev-research/main/grants.md"
    entry = by_name[name]
    collection = entry["collection"]
    discovery = collection["discovery"]
    assert entry["url"] == url
    assert entry.get("direct_page") is True
    assert discovery["start_urls"] == [url]
    assert "raw.githubusercontent.com" in collection["allowed_domains"]
    assert discovery["max_depth"] == 0
    assert discovery["max_pages"] == 1
    assert discovery["follow_detail_pages"] is False
    assert discovery["follow_pdfs"] is False
    assert discovery["pagination"] is False

def test_near_page_budget_is_raised_after_exhausting_the_previous_cap():
    by_name = {entry["name"]: entry for entry in _entries()}
    discovery = by_name["NEAR Ecosystem Funding"]["collection"]["discovery"]
    assert discovery["max_pages"] == 40


def test_ripple_ubri_follows_discovered_pdf_evidence():
    by_name = {entry["name"]: entry for entry in _entries()}
    discovery = by_name["Ripple UBRI"]["collection"]["discovery"]
    assert discovery["follow_pdfs"] is True
    assert any("Ripple-UBRI-Research" in pattern for pattern in discovery["exclude_url_patterns"])


def test_known_irrelevant_or_mis_scoped_portals_are_not_funding_roots():
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
        # Large discovery hubs have explicit higher caps; all crawls remain bounded.
        if entry.get("name") == "Superteam Development Grants":
            page_cap = 70
        elif entry.get("name") == "Climate KIC Open Calls":
            page_cap = 600
        elif entry.get("name") == "Open SBIR Funding Topics":
            page_cap = 120
        elif entry.get("name") == "NEAR Ecosystem Funding":
            page_cap = 40
        elif entry.get("name") == "XRPL Grants Program":
            page_cap = 24
        else:
            page_cap = 30
        assert 1 <= int(discovery["max_pages"]) <= page_cap
        assert 0 <= int(discovery.get("max_depth", 0)) <= 8

def test_live_source_adjustments_use_direct_program_pages_and_bounded_frontiers():
    import re
    by_name = {entry["name"]: entry for entry in _entries()}
    assert "Chainlink Build Program Status" not in by_name
    assert "Climate Pitch Climate-Tech Grants" not in by_name
    assert "NSF CPS Foundations and Connected Communities" not in by_name

    solana = by_name["Solana Grants and Funding"]
    # This URL is a generic grant directory and must be traversed, not emitted as a grant.
    assert solana.get("direct_page", False) is False
    assert solana["url"] == "https://solana.org/grants-funding"
    assert solana["collection"]["discovery"]["start_urls"] == [solana["url"]]
    assert "superteam.fun" not in solana["collection"]["allowed_domains"]
    assert any("/news/" in p for p in solana["collection"]["discovery"]["exclude_url_patterns"])
    assert solana["collection"]["discovery"]["max_pages"] == 30

    superteam = by_name["Superteam Development Grants"]
    assert superteam["url"] == "https://superteam.fun/earn/opportunities/development-grants/"
    assert superteam["collection"]["discovery"]["start_urls"] == [superteam["url"]]
    assert {"superteam.fun", "earn.superteam.fun", "solana.org", "solana.foundation"}.issubset(set(superteam["collection"]["allowed_domains"]))
    assert superteam["collection"]["discovery"]["max_pages"] == 70
    assert any("docs" in p and "document" in p for p in superteam["collection"]["discovery"]["exclude_url_patterns"])
    assert superteam.get("direct_page", False) is False

    near_entry = by_name["NEAR Ecosystem Funding"]
    assert near_entry["url"] == "https://near.org/funding"
    near = near_entry["collection"]["discovery"]
    assert near["max_pages"] == 40 and near["max_depth"] == 3 and near["exclude_url_patterns"]
    assert {"near.org", "www.near.org", "nearn.io", "app.potlock.org"}.issubset(set(near_entry["collection"]["allowed_domains"]))
    assert "https://near.org/funding" in near["start_urls"]
    assert "https://nearn.io/devhub/" in near["start_urls"]
    assert "https://app.potlock.org/pots" in near["start_urls"]

    xrpl_entry = by_name["XRPL Grants Program"]
    xrpl = xrpl_entry["collection"]
    assert {"xrplgrants.org", "submit.xrplgrants.org", "brinc.io", "tenity.com"}.issubset(set(xrpl["allowed_domains"]))
    assert xrpl["discovery"]["max_depth"] == 3 and xrpl["discovery"]["max_pages"] == 24
    assert "https://xrplgrants.org/" in xrpl["discovery"]["start_urls"]
    assert "https://submit.xrplgrants.org/submit" in xrpl["discovery"]["start_urls"]
    assert not any("brinc.io" in u or "tenity.com" in u for u in xrpl["discovery"]["start_urls"])
    assert any("awardees" in p for p in xrpl["discovery"]["exclude_url_patterns"])
    assert any("submittable" in p and "what-is-submittable" in p for p in xrpl["discovery"]["exclude_url_patterns"])
    assert any(re.search(p, "https://manager.submittable.com/user/submissions", re.IGNORECASE) for p in xrpl["discovery"]["exclude_url_patterns"])

    filecoin = by_name["Filecoin Grants and Funding"]
    assert filecoin["url"] == "https://fil.org/grants"
    assert filecoin["collection"]["discovery"]["start_urls"][0] == filecoin["url"]

    climate = by_name["Climate KIC Open Calls"]
    assert climate["url"] == "https://www.climate-kic.org/get-involved/open-calls/"
    climate_discovery = climate["collection"]["discovery"]
    assert "https://www.climate-kic.org/open-call/enhance-neb-2027-open-call/" in climate_discovery["start_urls"]
    assert "https://www.eitfood.eu/innovation" in climate_discovery["start_urls"]
    current_eit_culture_call = "https://eit-culture-creativity.eu/your-opportunities/calls-funding/financial-support-startups-and-scaleups-shape-acceleration-scale"
    assert current_eit_culture_call in climate_discovery["start_urls"]
    assert current_eit_culture_call in climate_discovery["direct_page_urls"]
    assert climate_discovery["max_depth"] == 8 and climate_discovery["max_pages"] == 600
    assert climate_discovery["pagination"] is True
    assert climate_discovery["follow_documents"] is True
    assert climate.get("direct_page", False) is False

    nsf_identity = by_name["NSF SBIR Trusted Identity"]
    assert nsf_identity["url"] == "https://www.sbir.gov/topics/1012406"
    assert nsf_identity["research_domains"] == ["DID"]
    assert nsf_identity.get("direct_page") is True
    assert nsf_identity["collection"]["discovery"]["max_pages"] == 1

    nsf_climate = by_name["NSF SBIR Climate Change Adaptation and Mitigation"]
    assert nsf_climate["url"] == "https://www.sbir.gov/topics/1012419"
    assert nsf_climate["research_domains"] == ["ESG"]
    assert nsf_climate.get("direct_page") is True
    assert nsf_climate["collection"]["discovery"]["max_pages"] == 1

    sbir = by_name["Open SBIR Funding Topics"]
    assert sbir["url"] == "https://www.sbir.gov/topics?status=Open"
    assert sbir.get("direct_page", False) is False
    assert set(sbir["research_domains"]) == set(ALLOWED_RESEARCH_AREAS)
    assert sbir["collection"]["discovery"]["max_pages"] == 120
    sbir_exclusions = sbir["collection"]["discovery"]["exclude_url_patterns"]
    assert any("/awards" in pattern for pattern in sbir_exclusions)
    assert any("/apply" in pattern for pattern in sbir_exclusions)
    assert any("data" in pattern and "sbir" in pattern for pattern in sbir_exclusions)

    eu = by_name["EU Open and Forthcoming Funding Calls"]
    assert "status=31094501%2C31094502" in eu["url"]
    assert eu.get("direct_page", False) is False
    assert set(eu["research_domains"]) == set(ALLOWED_RESEARCH_AREAS)
    assert eu["collection"]["mode"] == "api"
    assert eu["collection"]["provider"] == "eu_funding_tenders"
    assert eu["collection"]["api"]["types"] == ["1", "2", "8"]
    assert eu["collection"]["api"]["statuses"] == ["31094501", "31094502"]
    assert eu["collection"]["limits"]["max_pages"] == 200
    assert eu["collection"]["limits"]["max_documents"] == 10000
    assert len(eu["collection"]["api"]["search_texts"]) == 10


def test_load_sources_preserves_direct_page_for_collection_and_checkpoint_fingerprint(tmp_path, monkeypatch):
    from dataclasses import replace

    from agents.funding_agent import load_sources, _funding_sources_fingerprint

    catalogue = {
        "sources": {
            "tier_1": [{
                "name": "Explicit Direct Program",
                "url": "https://example.com/grants",
                "direct_page": True,
                "research_domains": ["DePIN"],
                "collection": {
                    "mode": "web",
                    "discovery": {"start_urls": ["https://example.com/grants"], "max_depth": 0, "max_pages": 1},
                },
            }]
        }
    }
    path = tmp_path / "funding_sources.yaml"
    path.write_text(yaml.safe_dump(catalogue), encoding="utf-8")
    monkeypatch.setattr("agents.funding_agent.apply_refresh_policy", lambda _agent, sources: sources)

    sources = load_sources(path)
    assert len(sources) == 1
    assert sources[0].direct_page is True
    assert _funding_sources_fingerprint(sources) != _funding_sources_fingerprint(
        [replace(sources[0], direct_page=False)]
    )


def test_filecoin_configured_directory_is_not_suppressed_by_deprecated_registry_state():
    import json

    registry_path = REPO_ROOT / "sources" / "source_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    entries = [
        entry for entry in registry.get("entries", [])
        if entry.get("agent_name") == "funding"
        and entry.get("url") == "https://fil.org/grants"
    ]

    assert len(entries) == 1
    assert entries[0]["status"] == "active"
    assert entries[0]["active_config"] is True

def test_live_truncated_source_caps_are_assigned_to_the_intended_sources():
    by_name = {entry["name"]: entry for entry in _entries()}

    solana_discovery = by_name["Solana Grants and Funding"]["collection"]["discovery"]
    superteam_discovery = by_name["Superteam Development Grants"]["collection"]["discovery"]
    climate_kic_discovery = by_name["Climate KIC Open Calls"]["collection"]["discovery"]

    # Keep the Solana directory crawl bounded independently of Superteam.
    assert solana_discovery["max_pages"] == 30
    assert superteam_discovery["max_pages"] == 70
    # The depth-8 crawl follows bounded EIT pagination to official call details; max_pages remains the hard cap.
    assert climate_kic_discovery["max_depth"] == 8
    assert climate_kic_discovery["max_pages"] == 600
    # The SBIR discovery portal needs its restored bounded depth/page budget.
    sbir_discovery = by_name["Open SBIR Funding Topics"]["collection"]["discovery"]
    assert sbir_discovery["max_depth"] == 4 and sbir_discovery["max_pages"] == 120


def test_open_sbir_excludes_generic_navigation_without_excluding_solicitations():
    import re

    by_name = {entry["name"]: entry for entry in _entries()}
    discovery = by_name["Open SBIR Funding Topics"]["collection"]["discovery"]
    patterns = discovery.get("exclude_url_patterns", [])

    def excluded(url):
        return any(re.search(pattern, url, flags=re.IGNORECASE) for pattern in patterns)

    assert excluded("https://www.grants.gov/search-grants.html?agencyCode=NSF")
    assert excluded("https://apply07.grants.gov/apply/spoExit.jsp?p=learn-grants")
    assert excluded("https://www.grants.gov/help/search-grants/site-search-results")
    # Preserve current opportunity detail pages and their solicitation attachments.
    assert not excluded("https://www.simpler.grants.gov/opportunity/active-example")
    assert excluded("https://www.nsf.gov/funding/postdocs")
    assert excluded("https://new.nsf.gov/funding/undergraduates")
    assert excluded("https://www.nsf.gov/funding/opportunities/sbirsttr-phase-i-nsf-small-business-innovation-research-small-business/505960")
    assert excluded("https://resources.research.gov/common/attachment/Desktop/Single_ID_Help_Public.pdf")
    assert excluded("https://www.nsf.gov/funding/opportunities/mps-astro-mps-astronomical-sciences-research-programs/nsf26-522/solicitation")
    assert excluded("https://www.nsf.gov/funding/opportunities/grfp-nsf-graduate-research-fellowship-program/nsf26-526/solicitation")
    assert excluded("https://www.whitehouse.gov/wp-content/uploads/2021/06/M-21-26.pdf")
    assert excluded("https://www.congress.gov/117/plaws/publ58/PLAW-117publ58.pdf")
    assert excluded("https://www.nsf.gov/funding/opportunities/sbirsttr-phase-i-nsf-small-business-innovation-research-small-business/505960/nsf23-515/solicitation")
    # Individual current NSF SBIR solicitations remain traversable.
    assert not excluded(
        "https://www.nsf.gov/funding/opportunities/sbirsttr-phase-i/505960/nsf24-579/solicitation"
    )


def test_climate_kic_excludes_stale_partner_posts_and_social_links():
    import re

    by_name = {entry["name"]: entry for entry in _entries()}
    discovery = by_name["Climate KIC Open Calls"]["collection"]["discovery"]
    patterns = discovery.get("exclude_url_patterns", [])

    def excluded(url):
        return any(re.search(pattern, url, flags=re.IGNORECASE) for pattern in patterns)

    assert excluded("https://innovationstarterbox.bg/blog/11-granta-ot-eit-climate-kic-otivat-v-balgarski-startapi/")
    assert excluded("https://impacthub.ro/2025/07/02/start-la-inscrieri-in-romania-climaccelerator/")
    assert excluded("https://www.f6s.com/medclimaccelerator2022/apply")
    assert excluded("https://www.instagram.com/bgiaccelerator/")
    assert not excluded("https://www.climate-kic.org/open-call/enhance-neb-2027-open-call/")
    assert excluded("https://ec.europa.eu/info/funding-tenders/opportunities/data/topic-list.html")
    assert excluded("https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/HORIZON-CL5-2026-01")
    assert excluded("https://6740820.extforms.netsuite.com/app/site/hosting/scriptlet.nl?script=1509&deploy=1")
    assert excluded("https://eit-hei.eu/funding-support/funding-opportunities/past-calls/call-for-proposals-3/")
    assert excluded("https://eit-hei.eu/events/introduction-to-heinnovate-call-2026-info-sessions/")
    assert excluded("https://opencalls.fund/open-calls/closed")
    assert excluded("https://www.heinnovate.eu/en/events-webinars/heinnovate-information-session-eit-higher-education-initiative-call-2026")
    assert excluded("https://socialtechlab.org/socialtech-labs-social-impact-generator-hub-selected-as-a-finalist-for-the-new-european-bauhaus-prizes-2026/")
    # Keep an individual current EIT opportunity traversable; only generic/historical navigation is excluded.
    assert not excluded("https://www.eit.europa.eu/our-activities/opportunities/enhance-neb-open-call-2027")



def test_batch1_known_navigation_exclusions_preserve_call_pages():
    import re

    by_name = {entry["name"]: entry for entry in _entries()}
    near = by_name["NEAR Ecosystem Funding"]["collection"]["discovery"]["exclude_url_patterns"]
    climate = by_name["Climate KIC Open Calls"]["collection"]["discovery"]["exclude_url_patterns"]
    sbir = by_name["Open SBIR Funding Topics"]["collection"]["discovery"]["exclude_url_patterns"]

    def excluded(patterns, url):
        return any(re.search(pattern, url, flags=re.IGNORECASE) for pattern in patterns)

    assert excluded(near, "https://app.potlock.org/campaign/[campaignId]?tab=leaderboard")
    assert not excluded(near, "https://app.potlock.org/campaign/12345")
    assert excluded(sbir, "https://www.grants.gov/")
    # SAM.gov registration is ancillary application setup, not a separate funding call.
    assert excluded(sbir, "https://sam.gov/SAM/")
    # The generic NSF index is out of scope; individual NSF solicitations remain discoverable.
    assert excluded(sbir, "https://www.nsf.gov/funding/opportunities")
    assert not excluded(sbir, "https://www.nsf.gov/funding/opportunities/sbirsttr-phase-i/505960/nsf24-579/solicitation")
    assert not excluded(sbir, "https://www.simpler.grants.gov/opportunity/active-example")
    assert excluded(climate, "https://apps.apple.com/us/app/eu-funding-me/id6733238007")
    assert excluded(climate, "https://www.eit.europa.eu/library/list-awarded-contracts-2024")
    assert excluded(climate, "https://opencalls.fund/open-call/ofelia/faq")
    assert not excluded(climate, "https://www.eitfood.eu/open-calls/seedbed-teamup-exploration-phase")
    assert not excluded(climate, "https://assets.eitfood.eu/files/OPEN-CALLS/FFDI_MERGED_CALL_GUIDELINES.pdf")


def test_climate_kic_batch1_coverage_routes_and_non_call_filters():
    import re

    source = next(entry for entry in _entries() if entry["name"] == "Climate KIC Open Calls")
    discovery = source["collection"]["discovery"]
    patterns = discovery.get("exclude_url_patterns", [])
    domains = set(source["collection"]["allowed_domains"])

    def excluded(url):
        return any(re.search(pattern, url, flags=re.IGNORECASE) for pattern in patterns)

    assert discovery["max_depth"] == 8
    assert discovery["max_pages"] == 600
    assert discovery["pagination"] is True
    # The current 2027 SME Market Expansion call is seeded canonically, rather
    # than relying on a deep link from a historical investment-news page.
    current_eit_um_call = "https://www.eiturbanmobility.eu/call-for-proposals/sme-market-expansion-open-call-2027/"
    assert current_eit_um_call in discovery["start_urls"]
    assert current_eit_um_call in discovery["direct_page_urls"]
    assert not excluded(current_eit_um_call)
    assert excluded("https://eit-culture-creativity.eu/open-call-acceleration-and-post-acceleration-programmes/")
    assert excluded("https://www.eitfood.eu/open-calls/fast-track-to-market")
    assert excluded("https://www.eitfood.eu/open-calls/fast-track-to-market-2024")
    assert excluded("https://www.desirmed.eu/announcements/desirmed-launches-a-photo-contest-capture-climate-adaptation-a-photo-exhibition-on-mediterranean-transformation-to-address-climate-adaptation")
    assert excluded("https://nbracer.eu/voices-of-the-youth-2/")
    assert excluded("https://wptemplate.impacthub.net/impact-stories/")
    assert excluded("https://assets.eitfood.eu/files/OPEN-CALLS/Declaration-on-AI-template_Attachment-to-Lower-Silesia-360%C2%B0-Integrated-Farm-Advisory-Implementation-Copy.docx")
    assert excluded("https://www.eit.europa.eu/our-activities/opportunities/raw-materials-and-circular-societies-prize-call-2021")
    # Historical news/results, expired accelerator routes, generic portals, and
    # policy-only PDFs are excluded without excluding the current canonical call.
    assert excluded("https://www.eit.europa.eu/news-events/news/eit-climate-kics-accelerator-programme-secures-key-partners")
    assert excluded("https://www.climate-kic.org/news/eit-climate-kic-new-accelerator-programme-expressions-of-interest-partners/")
    assert excluded("https://www.eit.europa.eu/our-activities/opportunities/partnering-eit-climate-kic-new-accelerator-call-expression-interest")
    assert excluded("https://www.eiturbanmobility.eu/eit-urban-moblity-startup-investments-2021-q2/")
    assert excluded("https://www.eiturbanmobility.eu/accessible-cities-cleaner-road-transport-and-safer-trams-meet-our-2026-eit-awards-nominees/")
    assert excluded("https://www.bettermobility.eu/accelerator/")
    assert excluded("https://eiturbanmobility.awardsplatform.com/")
    assert excluded("https://www.eiturbanmobility.eu/wp-content/uploads/2024/11/EIT-UM-Eligibility-expenditure.pdf")
    assert excluded("https://www.eiturbanmobility.eu/call-for-proposals/scaling-startups-programme-call/")
    # Crawl official partner detail pages and official attachments, not just the directory.
    assert "eit-culture-creativity.eu" in domains
    assert "eitcc-dev.wmstudio.space" in domains
    assert "eit-girlsgostem.eu" in domains
    assert "assets.eitfood.eu" in domains
    assert not excluded("https://eit-culture-creativity.eu/your-opportunities/calls-funding/innovation-projects")
    assert not excluded("https://assets.eitfood.eu/files/OPEN-CALLS/FFDI_MERGED_CALL_GUIDELINES.pdf")
    assert not excluded("https://www.eitfood.eu/open-calls/seedbed-teamup-exploration-phase")
    # Generic taxonomy, procurement archives, result pages and login routes are not call details.
    assert excluded("https://www.eit.europa.eu/tags/opportunity-0")
    assert excluded("https://www.eit.europa.eu/our-activities/opportunities/erma-booster-call-2025")
    assert excluded("https://www.eit.europa.eu/our-activities/opportunities/eit-health-drive-2022-apply-now")
    assert excluded("https://www.eit.europa.eu/news-events/news/eit-food-selected-65-startups-2026")
    assert excluded("https://apply.eithealth.eu/s_Login.jsp")
    assert excluded("https://eit.europa.eu/sites/default/files/2024-03/List%20of%20contracts%20awarded%20in%202023.pdf")
    assert excluded("https://opencalls.fund/open-call/results/ofelia")
    assert excluded("https://webgate.ec.europa.eu/funding-tenders-opportunities/display/IT/Duplicate+Profile+and+Account+Deletion")
    assert excluded("https://www.myeitfood.eu/s/login/?ec=302")
    assert excluded("https://wkf.ms/4ohKxeg")
    assert excluded("https://forms.monday.com/forms/7ba45f0c5473520c6f4ef1ad733807c1?r=euc1")
    assert excluded("https://eitcc-dev.wmstudio.space/private/login")
    assert not excluded("https://eitcc-dev.wmstudio.space/sites/default/files/2026-10/EITCC_Open%20call_Innovation%20projects_21.10.2026.pdf")
    assert excluded("https://assets.eitfood.eu/files/EIT-Food-LumpSum-Subgrant-Agreement_Seedbed-Venture-Catalyst.pdf")


def test_filecoin_excludes_github_issue_query_navigation_and_separate_ipfs_programme():
    import re

    by_name = {entry["name"]: entry for entry in _entries()}
    entry = by_name["Filecoin Grants and Funding"]
    discovery = entry["collection"]["discovery"]
    patterns = discovery.get("exclude_url_patterns", [])
    assert "ipfsgrants.io" not in entry["collection"]["allowed_domains"]
    assert discovery["max_depth"] == 3
    assert discovery["max_pages"] == 30

    def excluded(url):
        return any(re.search(pattern, url, flags=re.IGNORECASE) for pattern in patterns)

    assert excluded("https://github.com/filecoin-project/devgrants/issues?q=is%3Aissue+state%3Aopen")
    assert excluded("https://github.com/filecoin-project/devgrants/issues/293")
    assert excluded("https://github.com/filecoin-project/devgrants/milestones")
    assert excluded("https://github.com/ipfs/devgrants/issues/293")
    assert excluded("https://github.com/ipfs/devgrants/tree/main/open-grants")
    assert not excluded("https://github.com/filecoin-project/devgrants")
    assert excluded("https://ipfsgrants.io/")


def test_eu_bounded_audit_keeps_detail_fetch_failures_visible_without_blocking_other_valid_records():
    workflow = (REPO_ROOT / ".github" / "workflows" / "eu-funding-tests.yml").read_text(encoding="utf-8")
    assert 'error.get("operation") == "fetch_detail_metadata"' in workflow
    assert '"detail_fetch_warnings": detail_fetch_warnings' in workflow
    assert '"non_cap_errors": non_cap_errors' in workflow
    # Root/search API acquisition errors remain blocking; only detail-fetch errors
    # are separated as warnings, and the audit still fails when no valid records remain.
    assert 'if non_cap_errors:' in workflow
    assert 'if result.records_valid == 0:' in workflow

def test_eit_urban_mobility_companion_page_is_configured_for_eligibility_context():
    climate = next(
        entry for entry in _entries()
        if entry["name"] == "Climate KIC Open Calls"
    )
    discovery = climate["collection"]["discovery"]
    primary_url = (
        "https://www.eiturbanmobility.eu/call-for-proposals/"
        "sme-market-expansion-open-call-2027/"
    )
    context_url = "https://www.eiturbanmobility.eu/sme-market-expansion-open-call-2027/"

    assert context_url in discovery["start_urls"]
    assert context_url in discovery["direct_page_urls"]
    assert {
        "primary_url": primary_url,
        "context_url": context_url,
        "fields": ["eligibility"],
    } in climate["collection"]["related_context_pages"]


def test_eit_urban_mobility_sample_form_and_info_session_are_not_call_records():
    import re

    climate = next(
        entry for entry in _entries()
        if entry["name"] == "Climate KIC Open Calls"
    )
    patterns = climate["collection"]["discovery"]["exclude_url_patterns"]
    sample_form = (
        "https://www.eiturbanmobility.eu/wp-content/uploads/2026/10/"
        "SAMPLE_Application_form_SME-Market-Expansion-Call-2027.pdf"
    )
    info_session = (
        "https://www.eiturbanmobility.eu/wp-content/uploads/2026/10/"
        "SME-Market-Expansion-Call-2027-Info-session-presentation.pdf"
    )
    canonical_call = (
        "https://www.eiturbanmobility.eu/call-for-proposals/"
        "sme-market-expansion-open-call-2027/"
    )
    call_manual = (
        "https://www.eiturbanmobility.eu/wp-content/uploads/2026/10/"
        "Call-Manual_SME-Market-Expansion-Call-2027_final_09102026.pdf"
    )

    assert any(re.search(pattern, sample_form, re.IGNORECASE) for pattern in patterns)
    assert any(re.search(pattern, info_session, re.IGNORECASE) for pattern in patterns)
    assert not any(re.search(pattern, canonical_call, re.IGNORECASE) for pattern in patterns)
    assert not any(re.search(pattern, call_manual, re.IGNORECASE) for pattern in patterns)




def test_climate_kic_excludes_only_verified_expired_eit_archive_routes():
    """Archived EIT links must not create false active-coverage blockers, while current roots stay in scope."""
    import re

    climate = next(
        entry for entry in _entries()
        if entry["name"] == "Climate KIC Open Calls"
    )
    discovery = climate["collection"]["discovery"]
    patterns = discovery["exclude_url_patterns"]

    def excluded(url):
        return any(re.search(pattern, url, re.IGNORECASE) for pattern in patterns)

    expired = [
        "https://www.eit.europa.eu/our-activities/opportunities/teaching-factories-competition-2025-call-companies-challenges",
        "https://www.eit.europa.eu/our-activities/opportunities/call-proposals-establish-social-license-operate-slo-hub",
        "https://www.eit.europa.eu/our-activities/opportunities/global-mba-materials-management",
        "https://www.eit.europa.eu/our-activities/opportunities/call-proposals-develop-raw-and-circular-economy-expedition-race",
        "https://www.eit.europa.eu/our-activities/opportunities/eit-digital-entrepreneurial-academy-2025",
        "https://www.eit.europa.eu/our-activities/opportunities/eit-manufacturing-call-proposals-2025",
        "https://www.eit.europa.eu/our-activities/opportunities/call-santander-x-global-challenge-countdown-2-zero",
        "https://www.eiturbanmobility.eu/business-creation-call-for-proposals-2022/",
        "https://www.eiturbanmobility.eu/academy-call-for-proposals-2022/",
        "https://www.eiturbanmobility.eu/call-for-proposals-bp-2022-2024-just-launched/",
        "https://www.eiturbanmobility.eu/launch-of-call-for-proposals-for-factory-challengemycity/",
    ]
    for url in expired:
        assert excluded(url), f"Known expired route still fans out: {url}"

    # The source must continue to inspect the live root and explicitly seeded
    # current call pages. Exclusions are exact routes, not blanket domain bans.
    current = [
        "https://www.climate-kic.org/get-involved/open-calls/",
        "https://www.climate-kic.org/open-call/enhance-neb-2027-open-call/",
        "https://www.eiturbanmobility.eu/call-for-proposals/sme-market-expansion-open-call-2027/",
        "https://www.eiturbanmobility.eu/sme-market-expansion-open-call-2027/",
        "https://eit-culture-creativity.eu/your-opportunities/calls-funding/financial-support-startups-and-scaleups-shape-acceleration-scale",
    ]
    for url in current:
        assert not excluded(url), f"Current explicit source route was excluded: {url}"

    # Direct Podio forms are application endpoints, not independent calls. The
    # parent call page retains the link as application evidence.
    assert excluded("https://podio.com/webforms/30468642/2572395?fields[id]=3255970275")



def test_superteam_token_icon_endpoint_is_not_crawled_as_a_funding_page():
    import re

    source = next(entry for entry in _entries() if entry["name"] == "Superteam Development Grants")
    patterns = source["collection"]["discovery"]["exclude_url_patterns"]
    url = "https://superteam.fun/api/token-icon?url=https%3A%2F%2Fassets.coingecko.com%2Fcoins%2Fimages%2F51281.png"
    assert any(re.search(pattern, url, re.IGNORECASE) for pattern in patterns)


def test_solana_placeholder_database_and_application_form_are_excluded():
    import re

    source = next(entry for entry in _entries() if entry["name"] == "Solana Grants and Funding")
    patterns = source["collection"]["discovery"]["exclude_url_patterns"]
    urls = [
        "https://airtable.com/apppDmK2Pin9WX8jV/shrDbfJ1wktQ7pB6f/tbli2ERM3sdhyHJYB",
        "https://airtable.com/apppDmK2Pin9WX8jV/shrolVvhFsrwvq3rA?prefill_Related+RFP=recFe5gmTrKteofrx",
    ]
    for url in urls:
        assert any(re.search(pattern, url, re.IGNORECASE) for pattern in patterns)
