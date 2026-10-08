import sys
sys.path.append('.')
from core.http_client import post_json
import time

payload = {'rows': 5, 'startRecordNum': 0, 'keyword': 'research', 'oppStatuses': 'open|forecasted|posted'}

for i in range(5):
    try:
        data, _ = post_json('https://api.grants.gov/v1/api/search2', json_body=payload)
        if isinstance(data, dict):
            d = data.get('data')
            print(f'Attempt {i+1}: data type = {type(d)}')
            if isinstance(d, str):
                print(f'  String value: {d[:50]}')
            elif isinstance(d, dict):
                print(f'  Dict keys: {list(d.keys())}')
    except Exception as e:
        print(f'Attempt {i+1}: Exception = {e}')
    time.sleep(1)