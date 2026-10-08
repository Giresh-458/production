import json
import urllib.request

url = "https://api.grants.gov/v1/api/search2"
payload = {"rows": 10, "keyword": "research", "oppStatuses": "posted|forecasted"}
body = json.dumps(payload).encode('utf-8')
req = urllib.request.Request(url, data=body, method='POST')
req.add_header('Content-Type', 'application/json')
req.add_header('User-Agent', 'RIF-Structured-Source/1.0')

try:
    with urllib.request.urlopen(req) as resp:
        status = resp.status
        content = resp.read()
        data = json.loads(content)
        print("METHOD: POST")
        print("REQUEST BODY:", payload)
        print("RESPONSE STATUS:", status)
        print("RESPONSE KEYS:", list(data.keys()))
        if 'data' in data and 'oppHits' in data['data']:
            hits = data['data']['oppHits']
            print("OPPORTUNITIES RETURNED:", len(hits))
            if hits:
                print("SAMPLE:", hits[0].get('title'), hits[0].get('number'))
except Exception as e:
    print("ERROR:", e)
