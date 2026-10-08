with open('agents/funding_agent.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace(
'''class FundingSource:
    name: str
    url: str
    tier: str
    direct_page: bool = False
    focus: str = ""''',
'''class FundingSource:
    name: str
    url: str
    tier: str
    direct_page: bool = False
    focus: str = ""
    allowed_domains: tuple[str, ...] = field(default_factory=tuple)'''
)

text = text.replace(
'''            direct_page = bool(entry.get("direct_page", False))
            focus = cleaned_text(str(entry.get("focus", "")))
            if name and url:
                sources.append(FundingSource(name=name, url=url, tier=tier, direct_page=direct_page, focus=focus))''',
'''            direct_page = bool(entry.get("direct_page", False))
            focus = cleaned_text(str(entry.get("focus", "")))
            allowed_domains = tuple(str(d).strip() for d in entry.get("allowed_domains", []) if str(d).strip())
            if name and url:
                sources.append(FundingSource(name=name, url=url, tier=tier, direct_page=direct_page, focus=focus, allowed_domains=allowed_domains))'''
)

text = text.replace(
'''            try:
                adaptive = collect_source(
                    source.url,
                    keywords=tuple(RECURSIVE_DIRECT_PAGE_CONFIG.link_keywords),
                    max_depth=2,
                    max_pages=posts_per_source * 2,
                )''',
'''            try:
                adaptive = collect_source(
                    source.url,
                    keywords=tuple(RECURSIVE_DIRECT_PAGE_CONFIG.link_keywords),
                    allowed_domains=source.allowed_domains,
                    max_depth=2,
                    max_pages=posts_per_source * 2,
                )'''
)

with open('agents/funding_agent.py', 'w', encoding='utf-8') as f:
    f.write(text)
