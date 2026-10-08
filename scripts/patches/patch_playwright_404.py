with open('core/web_collection.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace(
'''                title, text, links, final_url = rendered
                final_url = canonicalize_url(final_url or url)
                if _domain(final_url) not in allowed:''',
'''                title, text, links, final_url = rendered
                if _is_soft_404("", title, text):
                    failed.append({"url": url, "reason": "SOFT_404"})
                    continue
                final_url = canonicalize_url(final_url or url)
                if _domain(final_url) not in allowed:'''
)

text = text.replace(
'''                        if _domain(browser_final_url) in allowed:
                            final_url = browser_final_url''',
'''                        if _is_soft_404("", title, text):
                            failed.append({"url": url, "reason": "SOFT_404"})
                            continue
                        if _domain(browser_final_url) in allowed:
                            final_url = browser_final_url'''
)

with open('core/web_collection.py', 'w', encoding='utf-8') as f:
    f.write(text)
