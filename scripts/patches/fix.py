import re

with open('tests/test_multi_call_evidence_attribution.py', 'r') as f:
    text = f.read()

text = text.replace('["AI", "Security"]', '["Artificial Intelligence", "Security Systems"]')
text = text.replace('["Biomedical", "Data"]', '["Biomedical Research", "Data Availability"]')
text = text.replace('["AI", "Optimization"]', '["Artificial Intelligence", "Optimization Algorithms"]')

with open('tests/test_multi_call_evidence_attribution.py', 'w') as f:
    f.write(text)
