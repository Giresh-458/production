with open('core/web_collection.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace(
'''    if allowed_domains:
        allowed.update(_domain(d) for d in allowed_domains)''',
'''    if allowed_domains:
        allowed.update((_domain(d) if '://' in d else d.lower().strip()) for d in allowed_domains)'''
)

with open('core/web_collection.py', 'w', encoding='utf-8') as f:
    f.write(text)
