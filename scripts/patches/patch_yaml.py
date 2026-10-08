with open('sources/funding_sources.yaml', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace(
'''  - name: Polygon Grants
    url: https://polygon.technology/village/startups''',
'''  - name: Polygon Grants
    url: https://polygon.technology/village/startups
    allowed_domains:
      - hadronfc.com'''
)

with open('sources/funding_sources.yaml', 'w', encoding='utf-8') as f:
    f.write(text)
