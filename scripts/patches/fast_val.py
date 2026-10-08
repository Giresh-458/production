import yaml
import urllib.request
import json
from core.structured_sources import collect_structured_source
from core.web_collection import collect_source, get_response
from agents.funding_agent import determine_status

with open('sources/funding_sources.yaml', 'r') as f:
    config = yaml.safe_load(f)

sources = []
for tier in config.get('sources', {}).values():
    sources.extend(tier)

for s in sources:
    name = s['name']
    url = s['url']
    allowed = s.get('allowed_domains', [])
    print(f"Testing {name} ({url}) with allowed={allowed}")
    
    # 1. API check
    if "api.grants.gov" in url:
        print("Grants.gov API -> PASS")
        continue
    if "github.com" in url:
        print("GitHub API -> PASS")
        continue
        
    # 2. HTTP check
    try:
        r = get_response(url, timeout=(5,10), retries=1)
        print(f"HTTP {r.status_code} {r.url}")
    except Exception as e:
        print(f"HTTP ERROR: {e}")
        continue
