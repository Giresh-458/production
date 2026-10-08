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


def test_funding_registry_uses_current_first_party_program_pages():
    sources = dict(_funding_sources())
    assert sources['Chainlink Grants'] == 'https://chain.link/community/grants'
    assert sources['Polygon Grants'] == 'https://polygon.technology/village/startups'
    assert sources['Filecoin Grants and Funding'] == 'https://github.com/filecoin-project/devgrants'
    assert 'Ethereum Foundation Academic Grants' not in sources
    assert 'Filecoin ProPGF Batch 2' not in sources
    assert not any('/archives/community/grants' in url for url in sources.values())
    assert not any('/grants/grantees' in url for url in sources.values())
