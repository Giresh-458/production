from pathlib import Path

import yaml


def _funding_sources():
    payload = yaml.safe_load(Path('sources/funding_sources.yaml').read_text(encoding='utf-8')) or {}
    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get('name') and node.get('url'):
                out.append((str(node['name']), str(node['url'])))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return out


def test_funding_registry_uses_exact_curated_batch_one_sources():
    sources = dict(_funding_sources())
    expected = {
        "Ethereum ESP RFPs": "https://esp.ethereum.foundation/applicants/rfp",
        "Ethereum ESP Open Rounds": "https://esp.ethereum.foundation/applicants/open-rounds",
        "Solana Grants and Funding": "https://solana.org/grants-funding",
        "Superteam Development Grants": "https://superteam.fun/earn/opportunities/development-grants/",
        "NEAR Ecosystem Funding": "https://near.org/funding",
        "Ripple UBRI": "https://ripple.com/impact/ubri/",
        "XRPL Grants Program": "https://xrplgrants.org/",
        "Filecoin Grants and Funding": "https://fil.org/grants",
        "Flashbots MEV Research Grants": "https://raw.githubusercontent.com/flashbots/mev-research/main/grants.md",
        "Climate KIC Open Calls": "https://www.climate-kic.org/get-involved/open-calls/",
        "NSF SBIR Trusted Identity": "https://www.sbir.gov/topics/1012406",
        "NSF SBIR Climate Change Adaptation and Mitigation": "https://www.sbir.gov/topics/1012419",
        "Open SBIR Funding Topics": "https://www.sbir.gov/topics?status=Open",
        "EU Open and Forthcoming Funding Calls": "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/calls-for-proposals?isExactMatch=false&order=DESC&pageNumber=1&pageSize=50&sortBy=startDate&status=31094501%2C31094502",
        "I-DAPT Hub Cyber-Physical Systems Calls": "https://www.idapthub.org/applications/call_for_proposals",
    }
    assert sources == expected
    assert len(sources) == 15
    assert "https://chain.link/blog/build-program-evolution" not in sources.values()
    assert "https://climatepitch.org/" not in sources.values()
    assert "NSF CPS Foundations and Connected Communities" not in sources

    # Legacy mis-scoped sources remain excluded; broad SBIR/EU portals above are
    # intentionally allowed as discovery hubs and do not bypass per-call evidence gates.
    removed_names = {
        "MeitY Startup Hub",
        "iDEX Defence Innovation Challenges",
        "ANRF Research Funding",
        "DST India Calls for Proposals",
        "DARPA Research Opportunities",
        "ARPA-E",
        "Grants.gov Public Opportunities API",
        "Ethereum Road to Devcon 8 India University Program",
        "BIRAC Calls for Proposal",
        "ICMR Calls for Proposals",
        "Polygon Grants",
        "Ethereum Foundation Academic Grants",
        "NIST CPS / IoT Testbed",
        "CISA ICS",
        "Chainlink Build Program Status",
        "Climate Pitch Climate-Tech Grants",
    }
    assert removed_names.isdisjoint(sources)
    assert not any("/archives/community/grants" in url for url in sources.values())
    assert not any("/grants/grantees" in url for url in sources.values())
