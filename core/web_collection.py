from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import deque
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib import robotparser

from core.http_client import canonicalize_url, get_response


@dataclass(slots=True)
class WebPage:
    url: str
    title: str
    text: str
    depth: int
    parent_url: str | None
    discovered_from: str
    score: float = 0.0
    published_at: str | None = None
    modified_at: str | None = None
    is_playwright: bool = False


@dataclass(slots=True)
class WebCollectionResult:
    root_url: str
    pages: list[WebPage]
    discovered_urls: list[str]
    failed_urls: list[dict[str, str]]
    structured_data: list[dict[str, Any]]



def _is_crawlable_url(url: str) -> bool:
    """Reject malformed URLs and non-page assets before they reach the queue."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        if any(ch.isspace() for ch in url):
            return False
        if parsed.username or parsed.password:
            return False
        path = parsed.path.lower()
        blocked = (
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp",
            ".mp3", ".wav", ".ogg", ".mp4", ".webm", ".ogv", ".avi", ".mov",
            ".zip", ".tar", ".gz", ".rar", ".7z", ".exe", ".dmg", ".css", ".js",
            ".woff", ".woff2", ".ttf", ".otf",
        )
        return not path.endswith(blocked)
    except Exception:
        return False

def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _same_domain(url: str, allowed: set[str]) -> bool:
    return not allowed or _domain(url) in allowed


def _score(url: str, anchor: str, keywords: tuple[str, ...]) -> float:
    hay = f"{url} {anchor}".lower()
    score = 0.0
    positive = tuple(k.lower().strip() for k in keywords if k.strip())
    for k in positive:
        if k in hay:
            score += 3.0 if k in anchor.lower() else 1.5
    path = urlparse(url).path.lower()
    strong_paths = ("/rfp", "/grant", "/grants", "/funding", "/opportun", "/call", "/research", "/program", "/fellow", "/apply", "/application", "/dataset", "/papers", "/publications", "/projects")
    weak_paths = ("/news/", "/blog/", "/press/", "/about", "/contact", "/privacy", "/terms", "/login")
    if any(x in path for x in strong_paths):
        score += 4.0
    if any(x in path for x in weak_paths):
        score -= 2.0
    if url.lower().endswith((".pdf", ".xml", ".rss", ".atom")):
        score += 2.0
    return score



def _is_soft_404(html: str, title: str, text: str) -> bool:
    lower_text = text.lower()
    lower_title = title.lower()
    if 'rel="canonical" href="https://chain.link/404"' in html or 'href="/404"' in html: return True
    if "404" in lower_title and "not found" in lower_title: return True
    if "page not found" in lower_title: return True
    if len(text) < 500:
        if "not found" in lower_text or "page doesn\'t exist" in lower_text: return True
    if "domain is for sale" in lower_text or "buy this domain" in lower_text: return True
    return False

def _extract(html: str) -> tuple[str, str, list[dict[str, Any]], list[tuple[str, str]]]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    title = " ".join((soup.title.get_text(" ", strip=True) if soup.title else "").split())
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = " ".join(main.stripped_strings)
    text = re.sub(r"\s+", " ", unescape(text)).strip()
    structured: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        try:
            data = json.loads(raw)
            structured.extend(data if isinstance(data, list) else [data])
        except Exception:
            continue
    links: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = str(a.get("href", "")).strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        links.append((href, " ".join(a.stripped_strings)))
    return title, text, structured, links


def _metadata(structured: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    pub = mod = None
    for item in structured:
        if not isinstance(item, dict):
            continue
        pub = pub or item.get("datePublished") or item.get("startDate")
        mod = mod or item.get("dateModified") or item.get("dateUpdated")
        if isinstance(item.get("mainEntity"), dict):
            ent = item["mainEntity"]
            pub = pub or ent.get("datePublished")
            mod = mod or ent.get("dateModified")
    return (str(pub) if pub else None, str(mod) if mod else None)



def _robots_allowed(root_url: str, target_url: str, user_agent: str = "RIF/3.1",
                    cache: dict[str, robotparser.RobotFileParser | None] | None = None,
                    timeout: float = 5.0, fail_open: bool = True) -> bool:
    try:
        parsed = urlparse(root_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if cache is not None and origin in cache:
            rp = cache[origin]
            return True if rp is None else rp.can_fetch(user_agent, target_url)

        import urllib.request
        rp = robotparser.RobotFileParser()
        robots_url = f"{origin}/robots.txt"
        try:
            with urllib.request.urlopen(robots_url, timeout=timeout) as resp:
                raw = resp.read(64_000).decode("utf-8", errors="replace")
            rp.parse(raw.splitlines())
        except Exception:
            if cache is not None:
                cache[origin] = None
            return fail_open

        if cache is not None:
            cache[origin] = rp
        return rp.can_fetch(user_agent, target_url)
    except Exception:
        return fail_open


def _extract_discovery_urls(base_url: str, html: str, robots_text: str = "") -> list[str]:
    """Find explicitly advertised sitemap/feed hints without broad third-party crawling.

    We deliberately do not ask Trafilatura to discover arbitrary feeds/sitemaps: some
    sites expose links to large third-party/user-generated networks and that can turn
    one configured source into an effectively unbounded crawl.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel", [])) if isinstance(link.get("rel"), list) else str(link.get("rel", ""))
        typ = str(link.get("type", "")).lower()
        if "alternate" in rel.lower() and any(x in typ for x in ("rss", "atom", "xml")):
            urls.append(urljoin(base_url, str(link["href"])))
    for line in robots_text.splitlines():
        if line.lower().startswith("sitemap:"):
            value = line.split(":", 1)[1].strip()
            if value:
                urls.append(urljoin(base_url, value))
    # One conservative fallback. Do not probe a family of guessed sitemap names.
    if not any("sitemap" in u.lower() for u in urls):
        urls.append(urljoin(origin, "/sitemap.xml"))
    return list(dict.fromkeys(canonicalize_url(u) for u in urls if u))


def _sitemap_urls(sitemap_url: str, *, limit: int = 200, allowed_origin: str | None = None, depth: int = 0, max_depth: int = 3) -> list[str]:
    if depth >= max_depth:
        return []
    parsed = urlparse(sitemap_url)
    if parsed.scheme not in ("http", "https"):
        return []
    
    current_origin = f"{parsed.scheme}://{parsed.netloc}"
    if allowed_origin is None:
        allowed_origin = current_origin
    if current_origin != allowed_origin:
        return []
        
    try:
        response = get_response(sitemap_url, timeout=(5.0, 15.0), retries=1)
        text = response.text
    except Exception:
        return []
    if "<html" in text[:500].lower():
        return []
        
    try:
        import defusedxml.ElementTree as ET
    except ImportError:
        import xml.etree.ElementTree as ET
        
    try:
        root = ET.fromstring(text)
    except Exception:
        return []
        
    tag = root.tag.lower().split("}")[-1]
    locs = [str(el.text).strip() for el in root.iter() if el.tag.lower().split("}")[-1] == "loc" and el.text]
    if tag == "sitemapindex":
        out: list[str] = []
        for child in locs[:20]:
            out.extend(_sitemap_urls(child, limit=max(1, limit - len(out)), allowed_origin=allowed_origin, depth=depth + 1, max_depth=max_depth))
            if len(out) >= limit:
                break
        return out[:limit]
    return [canonicalize_url(x) for x in locs[:limit]]


def _pdf_text(response: Any) -> str:
    try:
        from io import BytesIO
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(response.content))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:30])
    except Exception:
        return ""


def _extract_with_trafilatura(html: str, url: str) -> tuple[str, str] | None:
    try:
        from trafilatura import bare_extraction
        doc = bare_extraction(html, url=url, include_comments=True, include_tables=True)
        if doc is None:
            return None
        title = str(getattr(doc, "title", "") or "").strip()
        text = str(getattr(doc, "text", "") or "").strip()
        return title, text
    except Exception:
        return None

_pw_local = threading.local()

def _get_browser_context():
    if not hasattr(_pw_local, "playwright"):
        from playwright.sync_api import sync_playwright
        import asyncio
        # Create a new event loop for this thread if it doesn't have one
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        _pw_local.playwright = sync_playwright().start()
        _pw_local.browser = _pw_local.playwright.chromium.launch(headless=True)
    return _pw_local.browser.new_context(user_agent="RIF/3.1 (+research-intelligence-pipeline)")

def _render_with_browser(url: str) -> tuple[str, str, list[tuple[str, str]], str] | None:
    """Optional last-resort JS rendering; never required for the normal path."""
    try:
        ctx = _get_browser_context()
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            html = page.content()
            final_url = page.url
        finally:
            page.close()
            ctx.close()
        title, text, _, links = _extract(html)
        return title, text, links, final_url
    except Exception:
        return None

def render_page_with_browser(url: str) -> tuple[str, str, list[tuple[str, str]], str] | None:
    """Render one page with the optional Playwright fallback.

    Intended for servers that reject ordinary HTTP clients (for example 403)
    while still serving the page to a normal browser. Robots policy is checked
    by callers because this function is a low-level renderer.
    """
    return _render_with_browser(url)


def collect_source(
    root_url: str,
    *,
    keywords: tuple[str, ...],
    allowed_domains: tuple[str, ...] | None = None,
    max_depth: int = 2,
    max_pages: int = 24,
    max_documents: int | None = None,
    max_seconds: int | None = None,
    char_limit: int = 12000,
    timeout: tuple[float, float] = (8.0, 25.0),
) -> WebCollectionResult:
    """Adaptive, bounded, free web collection.

    Strategy: robots-aware HTTP -> structured/sitemap discovery -> scored links
    -> PDF extraction -> optional Trafilatura extraction -> optional Playwright
    only when the static response is empty/JS-shell-like. It is intentionally
    domain-bound and keeps provenance for every page.
    """
    root_url = canonicalize_url(root_url)
    allowed = {_domain(root_url)}
    if allowed_domains:
        allowed.update((_domain(d) if '://' in d else d.lower().strip()) for d in allowed_domains)
    pages: list[WebPage] = []
    failed: list[dict[str, str]] = []
    discovered: list[str] = []
    structured_all: list[dict[str, Any]] = []
    seen: set[str] = set()
    hashes: set[str] = set()
    queue: list[tuple[float, str, int, str | None, str]] = [(999.0, root_url, 0, None, "root")]
    robots_cache: dict[str, robotparser.RobotFileParser | None] = {}
    robots_text_cache: dict[str, str] = {}

    def push(url: str, depth: int, parent: str | None, source: str, score: float) -> None:
        url = canonicalize_url(url)
        if not _is_crawlable_url(url) or url in seen or not _same_domain(url, allowed):
            return
        queue.append((score, url, depth, parent, source))
        queue.sort(key=lambda x: (-x[0], x[2], x[1]))

    while queue and len(pages) < max_pages:
        from core.crawl_context import current_crawl_context
        ctx = current_crawl_context.get(None)
        if ctx and ctx.check_limits(source_max_pages=max_pages, source_max_documents=max_documents, source_max_seconds=max_seconds, source_max_depth=max_depth, current_depth=queue[0][2]):
            break
            
        score, url, depth, parent, source = queue.pop(0)
        if not _is_crawlable_url(url) or url in seen or not _same_domain(url, allowed):
            continue
        seen.add(url)
        if not _robots_allowed(root_url, url, cache=robots_cache):
            failed.append({"url": url, "reason": "robots_disallow"})
            continue
            
        if ctx: ctx.add_page(0)
        used_playwright = False
        try:
            response = get_response(url, timeout=timeout, retries=2)
            if ctx:
                with ctx.lock: ctx.bytes_downloaded += len(response.content)
            content_type = str(response.headers.get("Content-Type", "")).lower()
            final_url = canonicalize_url(str(response.url))
            if _domain(final_url) not in allowed:
                raise ValueError("redirected_to_untrusted_domain")
            if not ("html" in content_type or "pdf" in content_type or final_url.lower().endswith(".pdf")):
                failed.append({"url": url, "reason": f"unsupported_content_type:{content_type or 'unknown'}"})
                continue
            if "pdf" in content_type or final_url.lower().endswith(".pdf"):
                text = _pdf_text(response)
                title = Path(urlparse(final_url).path).stem.replace("_", " ").replace("-", " ").strip()
                links: list[tuple[str, str]] = []
                structured: list[dict[str, Any]] = []
            else:
                html = response.text
                title, text, structured, links = _extract(html)
                if _is_soft_404(html, title, text):
                    failed.append({"url": url, "reason": "SOFT_404"})
                    continue

                def quality_score(t_text):
                    s = min(len(t_text), 5000) / 5000 * 3
                    s += sum(1 for kw in keywords if kw.lower() in t_text.lower()) * 0.5
                    s += min(len(re.findall(r"(?m)^#{1,3}\s", t_text)), 5)
                    s -= (t_text.count("cookie") + t_text.count("privacy policy") + t_text.count("subscribe")) * 0.5
                    return s

                better = _extract_with_trafilatura(html, final_url)
                if better and quality_score(better[1]) > quality_score(text):
                    title = better[0] or title
                    text = better[1]
                
                def _needs_browser(h, t):
                    if len(t.strip()) >= 800: return False
                    headings = len(re.findall(r"<h[1-6][^>]*>", h, re.I))
                    s_bytes = sum(len(m) for m in re.findall(r"<script[^>]*>.*?</script>", h, re.I | re.S))
                    if len(t.strip()) < 300 and headings < 2: return True
                    return False

                if _needs_browser(html, text):
                    if ctx:
                        with ctx.lock:
                            ctx.js_shells_detected += 1
                            ctx.browser_attempts += 1
                            ctx.browser_escalations += 1
                    rendered = _render_with_browser(final_url)
                    used_playwright = True
                    if ctx: 
                        with ctx.lock: ctx.playwright_used += 1
                    if rendered and len(rendered[1]) > len(text):
                        if ctx:
                            with ctx.lock: ctx.browser_successes += 1
                        title, text, links, browser_final_url = rendered
                        browser_final_url = canonicalize_url(browser_final_url or final_url)
                        if _is_soft_404("", title, text):
                            failed.append({"url": url, "reason": "SOFT_404"})
                            continue
                        if _domain(browser_final_url) in allowed:
                            final_url = browser_final_url
                        else:
                            failed.append({"url": url, "reason": "browser_redirected_to_untrusted_domain"})
                            continue
                    else:
                        if ctx:
                            with ctx.lock: ctx.browser_failures += 1
                        failed.append({"url": url, "reason": "browser_render_failed", "js_shell_detected": True})
                        continue
        except Exception as exc:
            # Some public sites reject requests/urllib with 403/anti-bot rules
            # but remain accessible to a real browser. Try Playwright once
            # before declaring the source unavailable. This remains bounded and
            # robots-aware because the robots check happened immediately above.
            if ctx:
                with ctx.lock: ctx.browser_attempts += 1
                with ctx.lock: ctx.browser_escalations += 1
            rendered = _render_with_browser(url)
            used_playwright = True
            if ctx:
                with ctx.lock: ctx.playwright_used += 1
            if rendered and rendered[1].strip():
                if ctx:
                    with ctx.lock: ctx.browser_successes += 1
                title, text, links, final_url = rendered
                if _is_soft_404("", title, text):
                    failed.append({"url": url, "reason": "SOFT_404"})
                    continue
                final_url = canonicalize_url(final_url or url)
                if _domain(final_url) not in allowed:
                    failed.append({"url": url, "reason": "browser_redirected_to_untrusted_domain"})
                    continue
                content_type = "text/html"
                html = ""
                structured = []
            else:
                if ctx:
                    with ctx.lock: ctx.browser_failures += 1
                if ctx:
                    with ctx.lock: ctx.playwright_fallback_failed += 1
                failed.append({"url": url, "reason": str(exc)[:240]})
                continue

        def _is_challenge(t: str, head: str) -> bool:
            c = (t + " " + head).lower()
            return bool(
                ("verify you are" in c and "human" in c) or 
                ("cloudflare" in c and "ray id" in c) or
                ("just a moment" in c and "enable javascript" in c) or
                "pardon our interruption" in c or
                "checking if the site connection is secure" in c or
                "vercel security checkpoint" in c or
                "we're verifying your browser" in c
            )

        if _is_challenge(text, title):
            failed.append({"url": url, "reason": "anti-bot/challenge detected"})
            continue

        if not text.strip():
            failed.append({"url": url, "reason": "empty_text"})
            continue
        content_hash = hashlib.sha1(re.sub(r"\s+", " ", text[:char_limit]).encode()).hexdigest()
        if content_hash in hashes:
            continue
        hashes.add(content_hash)
        pub, mod = _metadata(structured)
        page = WebPage(final_url, title, re.sub(r"\s+", " ", unescape(text)).strip()[:char_limit], depth, parent, source, score, pub, mod, used_playwright)
        pages.append(page)
        if ctx: ctx.add_document()
        discovered.append(final_url)
        structured_all.extend(x for x in structured if isinstance(x, dict))

        # Site-map/feed discovery is only needed from the root. It is intentionally
        # conservative: only explicitly advertised sitemaps/feeds are followed.
        if depth == 0 and "html" in content_type:
            # Read robots once for explicit Sitemap directives. Failure is non-fatal.
            domain_key = _domain(final_url)
            if domain_key not in robots_text_cache:
                try:
                    import urllib.request
                    origin = f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}"
                    with urllib.request.urlopen(f"{origin}/robots.txt", timeout=5) as rr:
                        robots_text_cache[domain_key] = rr.read(64_000).decode("utf-8", errors="replace")
                except Exception:
                    robots_text_cache[domain_key] = ""
            for hint in _extract_discovery_urls(final_url, html, robots_text_cache.get(domain_key, "")):
                if hint.endswith("/robots.txt"):
                    continue
                if "sitemap" in urlparse(hint).path.lower():
                    for discovered_url in _sitemap_urls(hint, limit=max_pages * 6, allowed_origin=f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}"):
                        s = _score(discovered_url, "sitemap", keywords)
                        if s > 0.0:
                            push(discovered_url, 0, final_url, "sitemap", s + 1.0)
                else:
                    push(hint, 0, final_url, "feed", 2.0)
        if depth >= max_depth:
            continue
        for href, anchor in links:
            child = canonicalize_url(urljoin(final_url, href))
            if child in seen or not _same_domain(child, allowed):
                continue
            s = _score(child, anchor, keywords)
            # Always allow high-value structural URLs; otherwise require a
            # modest relevance score to keep the crawl bounded.
            if s >= 1.5 or any(token in urlparse(child).path.lower() for token in ("/rfp", "/grant", "/fund", "/research", "/opportun", "/call", "/application", "/dataset", "/papers", "/project")):
                push(child, depth + 1, final_url, anchor or "link", s)

    return WebCollectionResult(root_url, pages, discovered, failed, structured_all)

def combine_pages(result: WebCollectionResult, *, prefix: str = "", char_limit: int = 20000) -> str:
    blocks: list[str] = []
    for p in sorted(result.pages, key=lambda x: (-x.score, x.depth, x.url)):
        meta = " | ".join(x for x in (p.published_at, p.modified_at) if x)
        blocks.append(f"[SOURCE] {p.title or prefix}\nURL: {p.url}\nDepth: {p.depth}\nDates: {meta}\n{p.text}")
    return (prefix + "\n" if prefix else "") + "\n\n".join(blocks)[:char_limit]

