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


def test_funding_registry_uses_current_domain_scoped_program_pages():
    sources = dict(_funding_sources())

    assert sources['Chainlink Grants'] == 'https://chain.link/community/grants'
    assert sources['Filecoin Grants and Funding'] == 'https://github.com/filecoin-project/devgrants'
    assert sources['Flashbots MEV Research Grants'] == 'https://github.com/flashbots/mev-research/blob/main/grants.md'
    assert sources['Climate Pitch Climate-Tech Grants'] == 'https://climatepitch.org/home/'
    assert sources['BIRAC Calls for Proposal'] == 'https://www.birac.nic.in/cfp.php'
    assert sources['ICMR Calls for Proposals'] == 'https://www.icmr.gov.in/call-for-proposals'
    assert sources['I-DAPT Hub Cyber-Physical Systems Calls'] == 'https://idapthub.org/applications/call_for_proposals'

    # Broad, unscoped portals and known stale/general sources were deliberately
    # removed. The dedicated current catalog tests verify every remaining
    # source has a canonical RIF research-domain mapping.
    removed_names = {
        'MeitY Startup Hub',
        'iDEX Defence Innovation Challenges',
        'ANRF Research Funding',
        'DST India Calls for Proposals',
        'DARPA Research Opportunities',
        'ARPA-E',
        'Grants.gov Public Opportunities API',
        'Ethereum Road to Devcon 8 India University Program',
        'Polygon Grants',
        'Ethereum Foundation Academic Grants',
        'NIST CPS / IoT Testbed',
        'CISA ICS',
    }
    assert removed_names.isdisjoint(sources)

    assert not any('/archives/community/grants' in url for url in sources.values())
    assert not any('/grants/grantees' in url for url in sources.values())
