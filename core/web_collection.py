from __future__ import annotations

import hashlib
import json
import re
import threading
import requests
from collections import deque
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from core.http_client import canonicalize_url, get_response, _safe_redirect_target
from core.robots_policy import check_robots, get_robots_sitemaps, wait_for_host_slot


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



def _is_crawlable_url(url: str, *, allow_loopback: bool = False) -> bool:
    """Reject malformed URLs and non-page assets before they reach the queue."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        if any(ch.isspace() for ch in url):
            return False
        if parsed.username or parsed.password:
            return False
        try:
            _safe_redirect_target(url, url, allow_loopback=allow_loopback)
        except ValueError:
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



def _robots_allowed(
    root_url: str,
    target_url: str,
    user_agent: str = "RIF/3.1",
    cache: dict[str, Any] | None = None,
    timeout: float = 5.0,
    fail_open: bool = False,
) -> bool:
    """Enforce robots.txt for the target origin.

    The legacy fail_open argument remains accepted for call compatibility but
    cannot override the fail-closed handling of server/network failures.
    RFC 9309 permits access when robots.txt is unavailable (4xx), except that
    HTTP 429 is treated conservatively as a rate-limit signal.
    """
    del root_url, fail_open
    allowed, _, _ = check_robots(
        target_url,
        user_agent=user_agent,
        timeout=timeout,
        cache=cache,
    )
    return allowed


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


def _sitemap_urls(
    sitemap_url: str,
    *,
    limit: int = 200,
    allowed_origin: str | None = None,
    depth: int = 0,
    max_depth: int = 3,
    robots_cache: dict[str, Any] | None = None,
    allow_loopback: bool = False,
) -> list[str]:
    if depth >= max_depth:
        return []
    parsed = urlparse(sitemap_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        return []

    current_origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    if allowed_origin is None:
        allowed_origin = current_origin
    allowed_origin = allowed_origin.lower()
    if current_origin != allowed_origin:
        return []

    try:
        current_url = canonicalize_url(sitemap_url)
        redirect_statuses = {301, 302, 303, 307, 308}
        for _ in range(11):
            allowed, _, crawl_delay = check_robots(current_url, cache=robots_cache)
            if not allowed:
                return []
            if not wait_for_host_slot(current_url, crawl_delay=crawl_delay):
                return []
            response = get_response(
                current_url,
                timeout=(5.0, 15.0),
                retries=1,
                allow_redirects=False,
            )
            if response.status_code not in redirect_statuses:
                break
            location = response.headers.get("Location")
            close_response = getattr(response, "close", None)
            if callable(close_response):
                close_response()
            if not location:
                return []
            next_url = _safe_redirect_target(current_url, location, allow_loopback=allow_loopback)
            next_parsed = urlparse(next_url)
            if (
                next_parsed.scheme not in ("http", "https")
                or not next_parsed.netloc
                or next_parsed.username
                or next_parsed.password
                or f"{next_parsed.scheme.lower()}://{next_parsed.netloc.lower()}" != allowed_origin
            ):
                return []
            current_url = next_url
        else:
            return []

        if response.status_code < 200 or response.status_code >= 300:
            response.raise_for_status()
            return []
        final_url = canonicalize_url(str(response.url or current_url))
        final_parsed = urlparse(final_url)
        if f"{final_parsed.scheme.lower()}://{final_parsed.netloc.lower()}" != allowed_origin:
            return []
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
    locs = [
        str(el.text).strip()
        for el in root.iter()
        if el.tag.lower().split("}")[-1] == "loc" and el.text
    ]
    if tag == "sitemapindex":
        out: list[str] = []
        for child in locs[:20]:
            out.extend(
                _sitemap_urls(
                    child,
                    limit=max(1, limit - len(out)),
                    allowed_origin=allowed_origin,
                    depth=depth + 1,
                    max_depth=max_depth,
                    robots_cache=robots_cache,
                    allow_loopback=allow_loopback,
                )
            )
            if len(out) >= limit:
                break
        return out[:limit]
    return [canonicalize_url(x) for x in locs[:limit]]


def _close_response(response: Any) -> None:
    close_response = getattr(response, "close", None)
    if callable(close_response):
        close_response()


def _pdf_text(response: Any) -> str:
    try:
        pdf_bytes = response.content
    finally:
        _close_response(response)
    try:
        from io import BytesIO
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(pdf_bytes))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:30]).strip()
    except Exception as exc:
        raise ValueError(
            f"pdf_extraction_failed: {type(exc).__name__}: {exc}"
        ) from exc
    if not text:
        raise ValueError("pdf_contains_no_extractable_text")
    return text


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

def _get_browser_context(timeout_ms: int = 30_000):
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
        _pw_local.browser = _pw_local.playwright.chromium.launch(
            headless=True,
            timeout=max(1, int(timeout_ms)),
        )
    return _pw_local.browser.new_context(user_agent="RIF/3.1 (+research-intelligence-pipeline)")

def _render_with_browser(
    url: str,
    *,
    allowed_domains: set[str] | None = None,
    allow_loopback: bool = False,
) -> tuple[str, str, list[tuple[str, str]], str] | None:
    """Render a JS shell without bypassing robots policy or the active deadline."""
    try:
        from core.crawl_context import current_crawl_context

        crawl_ctx = current_crawl_context.get(None)
        remaining = (
            max(0.0, crawl_ctx.deadline - __import__("time").time())
            if crawl_ctx and crawl_ctx.deadline else 30.0
        )
        if remaining <= 0.1:
            if crawl_ctx:
                with crawl_ctx.lock:
                    crawl_ctx.stop_reasons.add("deadline")
            return None

        browser_ctx = _get_browser_context(
            timeout_ms=max(1, int(min(30.0, remaining) * 1000))
        )
        browser_allowed_domains = allowed_domains or {_domain(url)}
        page = browser_ctx.new_page()
        try:
            if crawl_ctx and crawl_ctx.deadline:
                remaining = crawl_ctx.deadline - __import__("time").time()
            if remaining <= 0.5:
                if crawl_ctx:
                    with crawl_ctx.lock:
                        crawl_ctx.stop_reasons.add("deadline")
                return None
            timeout_ms = max(1, int(min(30.0, remaining) * 1000))
            page.set_default_navigation_timeout(timeout_ms)

            def guard_document_navigation(route):
                request = route.request
                if request.resource_type == "document":
                    try:
                        _safe_redirect_target(url, request.url, allow_loopback=allow_loopback)
                    except ValueError:
                        route.abort("blockedbyclient")
                        return
                    if _domain(request.url) not in browser_allowed_domains:
                        route.abort("blockedbyclient")
                        return
                    allowed, _, crawl_delay = check_robots(request.url)
                    if not allowed:
                        route.abort("blockedbyclient")
                        return
                    deadline = crawl_ctx.deadline if crawl_ctx else None
                    if not wait_for_host_slot(
                        request.url,
                        crawl_delay=crawl_delay,
                        deadline=deadline,
                    ):
                        if crawl_ctx:
                            with crawl_ctx.lock:
                                crawl_ctx.stop_reasons.add("deadline")
                        route.abort("blockedbyclient")
                        return
                route.continue_()

            page.route("**/*", guard_document_navigation)
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if response is not None and not 200 <= response.status < 300:
                return None
            final_url = page.url
            _safe_redirect_target(url, final_url, allow_loopback=allow_loopback)
            if not check_robots(final_url)[0]:
                return None
            if crawl_ctx and crawl_ctx.deadline:
                remaining = crawl_ctx.deadline - __import__("time").time()
            if remaining <= 0:
                return None
            try:
                page.wait_for_load_state(
                    "networkidle", timeout=max(1, int(min(8.0, remaining) * 1000))
                )
            except Exception:
                pass
            if crawl_ctx and crawl_ctx.deadline and __import__("time").time() >= crawl_ctx.deadline:
                return None
            html = page.content()
        finally:
            page.close()
            browser_ctx.close()
        title, text, _, links = _extract(html)
        return title, text, links, final_url
    except Exception:
        return None

def render_page_with_browser(url: str) -> tuple[str, str, list[tuple[str, str]], str] | None:
    """Render one page after a JS-shell or transport failure.

    HTTP status responses such as 401, 403, 404 and 429 are not retried through
    a browser. Top-level document navigation is checked against robots.txt and
    remains within the configured origin/domain.
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
    allow_loopback_for_tests: bool = False,
) -> WebCollectionResult:
    """Adaptive, bounded, free web collection.

    Strategy: robots-aware HTTP -> structured/sitemap discovery -> scored links
    -> PDF extraction -> optional Trafilatura extraction -> optional Playwright
    only for a successful JS shell or a transport failure, never after an HTTP
    status response. It is intentionally domain-bound and keeps provenance.
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
    robots_cache: dict[str, Any] = {}

    def push(url: str, depth: int, parent: str | None, source: str, score: float) -> None:
        url = canonicalize_url(url)
        if not _is_crawlable_url(url, allow_loopback=allow_loopback_for_tests) or url in seen or not _same_domain(url, allowed):
            return
        queue.append((score, url, depth, parent, source))
        queue.sort(key=lambda x: (-x[0], x[2], x[1]))

    while queue and len(pages) < max_pages:
        from core.crawl_context import current_crawl_context
        ctx = current_crawl_context.get(None)
        if ctx and ctx.check_limits(source_max_pages=max_pages, source_max_documents=max_documents, source_max_seconds=max_seconds, source_max_depth=max_depth, current_depth=queue[0][2]):
            break
            
        score, url, depth, parent, source = queue.pop(0)
        if not _is_crawlable_url(url, allow_loopback=allow_loopback_for_tests) or url in seen or not _same_domain(url, allowed):
            continue
        seen.add(url)
        if not _robots_allowed(root_url, url, cache=robots_cache):
            failed.append({"url": url, "reason": "robots_disallow"})
            continue
        _, _, crawl_delay = check_robots(url, cache=robots_cache)
        if not wait_for_host_slot(
            url,
            crawl_delay=crawl_delay,
            deadline=ctx.deadline if ctx else None,
        ):
            failed.append({"url": url, "reason": "host_wait_unavailable_before_request"})
            continue

        if ctx:
            ctx.add_page(0)
        used_playwright = False
        html = ""
        try:
            # Follow redirects one hop at a time so each destination is
            # checked against its own robots.txt before it is requested.
            current_url = url
            redirect_statuses = {301, 302, 303, 307, 308}
            for redirect_count in range(11):
                response = get_response(
                    current_url, timeout=timeout, retries=2, allow_redirects=False
                )
                if response.status_code not in redirect_statuses:
                    break
                location = response.headers.get("Location")
                close_response = getattr(response, "close", None)
                if callable(close_response):
                    close_response()
                if not location:
                    raise ValueError(f"HTTP {response.status_code} redirect missing Location")
                next_url = _safe_redirect_target(
                    current_url, location, allow_loopback=allow_loopback_for_tests
                )
                parsed_next = urlparse(next_url)
                if (
                    parsed_next.scheme not in {"http", "https"}
                    or not parsed_next.netloc
                    or parsed_next.username
                    or parsed_next.password
                ):
                    raise ValueError("redirect_to_invalid_or_unsafe_url")
                if not _same_domain(next_url, allowed):
                    raise ValueError("redirected_to_untrusted_domain")
                if not _robots_allowed(root_url, next_url, cache=robots_cache):
                    failed.append({"url": next_url, "reason": "robots_disallow_redirect"})
                    response = None
                    break
                _, _, redirect_crawl_delay = check_robots(next_url, cache=robots_cache)
                if not wait_for_host_slot(
                    next_url,
                    crawl_delay=redirect_crawl_delay,
                    deadline=ctx.deadline if ctx else None,
                ):
                    failed.append({"url": next_url, "reason": "host_wait_unavailable_before_redirect"})
                    response = None
                    break
                current_url = next_url
            else:
                raise ValueError("too_many_redirects")
            if response is None:
                continue
            if response.status_code < 200 or response.status_code >= 300:
                try:
                    response.raise_for_status()
                finally:
                    _close_response(response)
                raise RuntimeError(f"Unexpected HTTP status {response.status_code}")
            if ctx:
                with ctx.lock:
                    ctx.bytes_downloaded += len(response.content)
            content_type = str(response.headers.get("Content-Type", "")).lower()
            final_url = canonicalize_url(str(response.url or current_url))
            if not _robots_allowed(root_url, final_url, cache=robots_cache):
                _close_response(response)
                failed.append({"url": final_url, "reason": "robots_disallow_redirect_target"})
                continue
            if _domain(final_url) not in allowed:
                _close_response(response)
                raise ValueError("redirected_to_untrusted_domain")
            is_pdf = "pdf" in content_type or final_url.lower().endswith(".pdf")
            if is_pdf:
                response_text = ""
            else:
                try:
                    response_text = str(getattr(response, "text", "") or "")
                finally:
                    _close_response(response)
            is_json = (
                "json" in content_type
                or final_url.lower().endswith(".json")
                or response_text.lstrip().startswith(("{", "["))
            )
            if not ("html" in content_type or is_pdf or is_json):
                failed.append({"url": url, "reason": f"unsupported_content_type:{content_type or 'unknown'}"})
                continue
            if is_pdf:
                text = _pdf_text(response)
                title = Path(urlparse(final_url).path).stem.replace("_", " ").replace("-", " ").strip()
                links: list[tuple[str, str]] = []
                structured: list[dict[str, Any]] = []
            elif is_json:
                try:
                    payload = json.loads(response_text)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    failed.append({"url": url, "reason": f"invalid_json_response:{str(exc)[:160]}"})
                    continue
                if isinstance(payload, dict):
                    title = str(payload.get("label") or payload.get("title") or payload.get("id") or "JSON Data")
                else:
                    title = "JSON Data"
                text = json.dumps(payload, ensure_ascii=False, indent=2)
                links = []
                structured = []
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
                    challenge_text = (h + " " + t).lower()
                    if (
                        ("cloudflare" in challenge_text and ("ray id" in challenge_text or "challenge" in challenge_text))
                        or ("verify you are" in challenge_text and "human" in challenge_text)
                        or ("just a moment" in challenge_text and "enable javascript" in challenge_text)
                        or "pardon our interruption" in challenge_text
                        or "vercel security checkpoint" in challenge_text
                        or "checking if the site connection is secure" in challenge_text
                        or "we're verifying your browser" in challenge_text
                    ):
                        return False
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
                    rendered = _render_with_browser(
                        final_url,
                        allowed_domains=allowed,
                        allow_loopback=allow_loopback_for_tests,
                    )
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
            # HTTP responses (especially 429) are authoritative. Do not use a
            # browser to retry, evade, or reinterpret an HTTP access decision.
            http_response = getattr(exc, "response", None)
            http_status = getattr(http_response, "status_code", None)
            if http_status is not None:
                retry_after = (getattr(http_response, "headers", {}) or {}).get("Retry-After")
                reason = f"http_status_{http_status}"
                if retry_after:
                    reason += f": retry_after={retry_after}"
                failed.append({"url": url, "reason": reason})
                continue
            if (
                not isinstance(exc, (requests.ConnectionError, requests.Timeout))
                or isinstance(exc, (requests.exceptions.ProxyError, requests.exceptions.SSLError))
            ):
                failed.append({"url": url, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"})
                continue
            if ctx and ctx.check_limits():
                failed.append({"url": url, "reason": "deadline_exceeded_before_browser_fallback"})
                continue
            if ctx:
                with ctx.lock:
                    ctx.browser_attempts += 1
                    ctx.browser_escalations += 1
            rendered = _render_with_browser(
                        url,
                        allowed_domains=allowed,
                        allow_loopback=allow_loopback_for_tests,
                    )
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

        def _is_challenge(t: str, head: str, raw_html: str = "") -> bool:
            c = (t + " " + head + " " + raw_html).lower()
            return bool(
                ("verify you are" in c and "human" in c) or 
                ("cloudflare" in c and "ray id" in c) or
                ("just a moment" in c and "enable javascript" in c) or
                "pardon our interruption" in c or
                "checking if the site connection is secure" in c or
                "vercel security checkpoint" in c or
                "we're verifying your browser" in c
            )

        if _is_challenge(text, title, html):
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
            sitemap_directives = get_robots_sitemaps(final_url, cache=robots_cache)
            robots_text = "\n".join(f"Sitemap: {sitemap}" for sitemap in sitemap_directives)
            for hint in _extract_discovery_urls(final_url, html, robots_text):
                if hint.endswith("/robots.txt"):
                    continue
                if "sitemap" in urlparse(hint).path.lower():
                    for discovered_url in _sitemap_urls(
                        hint,
                        limit=max_pages * 6,
                        allowed_origin=f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}",
                        robots_cache=robots_cache,
                        allow_loopback=allow_loopback_for_tests,
                    ):
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

