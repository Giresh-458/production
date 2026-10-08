with open('core/structured_sources.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace(
'''    repo_data,_=fetch_json(base,headers=headers,timeout=(8,20),retries=1)
    issues,_=fetch_json(base+"/issues",params={"state":"open","per_page":min(limit,100)},headers=headers,timeout=(8,20),retries=1)
    releases,_=fetch_json(base+"/releases",params={"per_page":min(10,limit)},headers=headers,timeout=(8,20),retries=1)''',
'''    repo_data,_=fetch_json(base,headers=headers,timeout=(8,20),retries=1)
    
    from core.http_client import get_response
    issues = []
    try:
        r = get_response(base+"/issues", params={"state":"all","per_page":min(limit,100)}, headers=headers, timeout=(8,20), retries=1)
        issues = r.json()
        if not isinstance(issues, list): issues = []
    except Exception:
        pass
        
    releases = []
    try:
        r = get_response(base+"/releases", params={"per_page":min(10,limit)}, headers=headers, timeout=(8,20), retries=1)
        releases = r.json()
        if not isinstance(releases, list): releases = []
    except Exception:
        pass'''
)

with open('core/structured_sources.py', 'w', encoding='utf-8') as f:
    f.write(text)
