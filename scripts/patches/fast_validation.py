import yaml
from core.structured_sources import collect_structured_source
from core.web_collection import collect_source, get_response

with open('sources/funding_sources.yaml', 'r') as f:
    config = yaml.safe_load(f)

sources = []
for tier in config.get('sources', {}).values():
    sources.extend(tier)

for s in sources:
    name = s['name']
    url = s['url']
    
    # 1. Try structured
    struct_res = None
    try:
        struct_res = collect_structured_source(url)
    except Exception:
        pass
        
    http_status = 'N/A'
    final_url = url
    try:
        r = get_response(url)
        http_status = r.status_code
        final_url = r.url
    except Exception as e:
        http_status = str(e)
        
    if struct_res and struct_res.text:
        print(f"SOURCE: {name}")
        print(f"CONFIGURED URL: {url}")
        print(f"FINAL URL: {final_url}")
        print(f"METHOD: API")
        print(f"HTTP STATUS: {http_status}")
        print(f"EXTRACTED TITLE: {struct_res.text.splitlines()[0][:100]}")
        print(f"EXTRACTED DESCRIPTION/SUMMARY: {struct_res.text[:100]}")
        print(f"EXTRACTED DATE/DEADLINE: N/A")
        print(f"EVIDENCE URL: {struct_res.provenance_url}")
        print(f"SOURCE TEXT CONFIRMATION: {struct_res.text[:100]}")
        print("---")
    else:
        try:
            web_res = collect_source(url, keywords=('grant', 'funding', 'research', 'rfp', 'proposal', 'program', 'startups'))
            if web_res.pages:
                p = web_res.pages[0]
                print(f"SOURCE: {name}")
                print(f"CONFIGURED URL: {url}")
                print(f"FINAL URL: {final_url}")
                print(f"METHOD: HTML/Browser")
                print(f"HTTP STATUS: {http_status}")
                print(f"EXTRACTED TITLE: {p.title}")
                print(f"EXTRACTED DESCRIPTION/SUMMARY: {p.text[:100]}")
                print(f"EXTRACTED DATE/DEADLINE: {p.published_at or 'N/A'}")
                print(f"EVIDENCE URL: {p.url}")
                print(f"SOURCE TEXT CONFIRMATION: {p.text[:100]}")
                print("---")
            else:
                reason = web_res.failed_urls[0]['reason'] if web_res.failed_urls else "NO_ACTIVE_OPPORTUNITIES"
                print(f"SOURCE: {name}")
                print(f"CONFIGURED URL: {url}")
                print(f"FINAL URL: {final_url}")
                print(f"STATUS: FAIL ({reason})")
                print("---")
        except Exception as e:
            print(f"SOURCE: {name} FAIL {str(e)}")
            print("---")
