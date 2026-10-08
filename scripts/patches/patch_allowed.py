with open('core/web_collection.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace(
'''def collect_source(
    root_url: str,
    *,
    keywords: tuple[str, ...],
    max_depth: int = 2,
    max_pages: int = 24,
    char_limit: int = 12000,
    timeout: tuple[float, float] = (8.0, 25.0),
) -> WebCollectionResult:''',
'''def collect_source(
    root_url: str,
    *,
    keywords: tuple[str, ...],
    allowed_domains: tuple[str, ...] | None = None,
    max_depth: int = 2,
    max_pages: int = 24,
    char_limit: int = 12000,
    timeout: tuple[float, float] = (8.0, 25.0),
) -> WebCollectionResult:'''
)

text = text.replace(
'''    root_url = canonicalize_url(root_url)
    allowed = {_domain(root_url)}''',
'''    root_url = canonicalize_url(root_url)
    allowed = {_domain(root_url)}
    if allowed_domains:
        allowed.update(_domain(d) for d in allowed_domains)'''
)

text = text.replace(
'''            if _domain(final_url) not in allowed:
                if depth == 0 and url == root_url:
                    allowed.add(_domain(final_url))
                else:
                    raise ValueError("redirected_to_untrusted_domain")''',
'''            if _domain(final_url) not in allowed:
                raise ValueError("redirected_to_untrusted_domain")'''
)

text = text.replace(
'''                        else:
                            if depth == 0 and url == root_url:
                                allowed.add(_domain(browser_final_url))
                                final_url = browser_final_url
                            else:
                                failed.append({"url": url, "reason": "browser_redirected_to_untrusted_domain"})
                                continue''',
'''                        else:
                            failed.append({"url": url, "reason": "browser_redirected_to_untrusted_domain"})
                            continue'''
)

text = text.replace(
'''                if _domain(final_url) not in allowed:
                    if depth == 0 and url == root_url:
                        allowed.add(_domain(final_url))
                    else:
                        failed.append({"url": url, "reason": "browser_redirected_to_untrusted_domain"})
                        continue''',
'''                if _domain(final_url) not in allowed:
                    failed.append({"url": url, "reason": "browser_redirected_to_untrusted_domain"})
                    continue'''
)

with open('core/web_collection.py', 'w', encoding='utf-8') as f:
    f.write(text)
