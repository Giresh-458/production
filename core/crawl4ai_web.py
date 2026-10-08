"""Crawl4AI-based web acquisition engine for funding source collection.

Implements:
- BFS queue-based depth-aware traversal
- Two-stage discovery (high-recall link extraction → high-precision funding classification)
- Full PDF processing (no silent truncation)
- Pagination detection and following
- Explicit error tracking
- Domain boundary enforcement
- Honest completeness reporting
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, List, Optional
from urllib.parse import urljoin, urlparse, urldefrag

from core.schemas import CollectionResult
from core.http_client import get_response

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

LOGGER = logging.getLogger("rif.crawl4ai_web")

# --- Stage B funding classifier keywords (used on page CONTENT, not just URLs) ---
FUNDING_CONTENT_SIGNALS = [
    "grant", "funding", "rfp", "request for proposals", "call for proposals",
    "call for applications", "funding opportunity", "foa", "accelerator",
    "builder funding", "research funding", "challenge prize", "bounty",
    "application program", "open call", "apply now", "submit a proposal",
    "deadline", "eligibility", "award", "fellowship", "scholarship",
    "seed funding", "ecosystem support", "devgrant", "hackathon funding",
]

# --- Stage A URL-path signals (broad recall, NOT sole classifier) ---
DISCOVERY_PATH_SIGNALS = [
    "grant", "funding", "rfp", "apply", "call", "opportunit",
    "award", "fellowship", "challenge", "bounty", "proposal", "foa",
    "open-call", "accelerat", "builder-fund", "devgrant",
    "prize", "competition",
]

# --- Pagination link signals ---
PAGINATION_SIGNALS = [
    "next", "next page", "older", "more", "load more", "show more",
    "page 2", "page 3", "page 4", "page 5",
]


try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
    HAS_CRAWL4AI = True
except ImportError:
    HAS_CRAWL4AI = False


def _normalize_url(url: str) -> str:
    """Remove fragment, strip trailing slash for dedup."""
    defragged, _ = urldefrag(url)
    return defragged.rstrip("/")


def _is_allowed_domain(url: str, base_domain: str, allowed_domains: list[str]) -> bool:
    """Check if URL is within source boundary."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not host:
        return False
    allowed = {base_domain.lower()} | {d.lower() for d in allowed_domains}
    return any(host == d or host.endswith("." + d) for d in allowed)


def _classify_link_stage_a(url: str, anchor_text: str, surrounding_text: str) -> float:
    """Stage A: high-recall scoring. Returns a 0-1 relevance score."""
    score = 0.0
    url_lower = url.lower()
    anchor_lower = (anchor_text or "").lower()
    context_lower = (surrounding_text or "").lower()

    # URL path signals
    path = urlparse(url_lower).path
    for signal in DISCOVERY_PATH_SIGNALS:
        if signal in path:
            score += 0.4
            break

    # Anchor text signals
    for signal in FUNDING_CONTENT_SIGNALS:
        if signal in anchor_lower:
            score += 0.5
            break

    # Surrounding text signals
    for signal in FUNDING_CONTENT_SIGNALS:
        if signal in context_lower:
            score += 0.3
            break

    # PDF/document link bonus
    if url_lower.endswith(".pdf"):
        score += 0.5

    return min(score, 1.0)


def _classify_page_stage_b(url: str, title: str, content: str) -> bool:
    """Stage B: high-precision funding classification on actual page content."""
    url_lower = url.lower()
    combined = f"{title}\n{content}".lower()
    
    # 1. Broad generic hubs and search forms
    index_patterns = [
        "/search", "/index", "/list", "/initiatives", 
        "awardsearch", "simple-search"
    ]
    is_hub = any(p in url_lower for p in index_patterns)
    
    # 2. Action signals (Must be present for an opportunity page)
    action_signals = [
        "eligibility", "how to apply", "submit a proposal", "apply now", 
        "deadline", "call for proposals", "call for applications", 
        "request for proposals", "evaluation criteria", "award amount",
        "funding amount", "eligible applicants", "submission instructions",
        "proposal preparation", "concept paper", "letter of intent"
    ]
    
    # 3. Context signals
    context_signals = [
        "grant", "funding", "rfp", "accelerator", "bounty", "fellowship",
        "scholarship", "seed funding", "ecosystem support", "devgrant", "award"
    ]
    
    action_hits = sum(1 for s in action_signals if s in combined)
    context_hits = sum(1 for s in context_signals if s in combined)
    
    if is_hub:
        # A hub needs strong proof to be classified as a detail opportunity
        return action_hits >= 3 and context_hits >= 2
        
    # Detail page needs both context and some actionable requirement language
    return action_hits >= 1 and context_hits >= 2


def _is_pagination_link(anchor_text: str, rel: str, url: str) -> bool:
    """Detect pagination links from anchor text, rel attribute, or URL patterns."""
    anchor_lower = (anchor_text or "").strip().lower()
    rel_lower = (rel or "").lower()

    if "next" in rel_lower:
        return True
    for signal in PAGINATION_SIGNALS:
        if signal == anchor_lower or anchor_lower.startswith(signal):
            return True
    # URL pattern: ?page=N
    if re.search(r"[?&]page=\d+", url):
        return True
    return False


async def _fetch_url(url: str) -> dict:
    """Fetch a single URL using Crawl4AI and extract links with context."""
    from core.crawl_context import current_crawl_context
    import asyncio
    import time
    
    ctx = current_crawl_context.get(None)
    timeout = 30.0
    if ctx and ctx.deadline:
        timeout = max(5.0, ctx.deadline - time.time())
        
    browser_config = BrowserConfig(headless=True)
    run_config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=int(timeout * 1000))
    async with AsyncWebCrawler(config=browser_config) as crawler:
        try:
            result = await asyncio.wait_for(crawler.arun(url=url, config=run_config), timeout=timeout + 5.0)
        except asyncio.TimeoutError:
            class DummyResult:
                success = False
                error_message = "Timeout"
                html = ""
                markdown_v2 = type("M", (), {"raw_markdown": ""})
            result = DummyResult()

        links = []
        pagination_links = []
        title = ""
        if result.html:
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(result.html, "html.parser")
                # Extract title
                title_tag = soup.find("title")
                title = title_tag.get_text(strip=True) if title_tag else ""

                for a_tag in soup.find_all("a", href=True):
                    href = a_tag["href"].strip()
                    if not href or href.startswith("#"):
                        continue

                    # Exclude non-content assets and protocol schemes
                    href_lower = href.lower()
                    if any(p in href_lower for p in [".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", "mailto:", "tel:", "javascript:"]):
                        continue

                    anchor_text = a_tag.get_text(strip=True)
                    rel = a_tag.get("rel", "")
                    if isinstance(rel, list):
                        rel = " ".join(rel)

                    # Get surrounding text from immediate parent, not whole sidebar/body
                    container = a_tag.find_parent(["p", "li", "dd", "td", "h1", "h2", "h3", "h4", "h5", "h6"])
                    surrounding = ""
                    if container:
                        container_text = container.get_text(strip=True)
                        if len(container_text) <= 300:
                            surrounding = container_text

                    # Resolve relative URLs
                    resolved = urljoin(url, href)
                    resolved = _normalize_url(resolved)

                    if not resolved.startswith("http"):
                        continue

                    # Exclude common non-content paths and duplicate language mirrors
                    path_lower = urlparse(resolved.lower()).path
                    if (
                        any(ex in path_lower for ex in ["/terms", "/privacy", "/cookies", "/login", "/signup", "/auth", "/signin"])
                        or path_lower == "/hi"
                        or path_lower.startswith("/hi/")
                    ):
                        continue

                    links.append({
                        "url": resolved,
                        "anchor_text": anchor_text,
                        "surrounding_text": surrounding,
                        "rel": rel,
                    })

                    if _is_pagination_link(anchor_text, rel, resolved):
                        pagination_links.append(resolved)

            except Exception as e:
                LOGGER.warning("Link extraction failed for %s: %s", url, e)

        return {
            "html": result.html,
            "markdown": result.markdown,
            "success": result.success,
            "error_message": getattr(result, "error_message", None),
            "status_code": getattr(result, "status_code", None),
            "url": url,
            "title": title,
            "links": links,
            "pagination_links": pagination_links,
        }


def _fetch_sync(url: str) -> dict:
    """Synchronous wrapper for _fetch_url."""
    return asyncio.run(_fetch_url(url))


def _fetch_pdf(url: str, max_pdf_pages: int | None = None) -> dict:
    """Fetch and extract text from a PDF. No silent truncation."""
    if PdfReader is None:
        return {"success": False, "error": "pypdf not installed", "url": url,
                "content": "", "total_pages": 0, "pages_processed": 0, "truncated": False}

    try:
        response = get_response(url, timeout=(8.0, 30.0), retries=2)
        reader = PdfReader(BytesIO(response.content))
        total_pages = len(reader.pages)

        if max_pdf_pages is not None and total_pages > max_pdf_pages:
            pages_to_process = reader.pages[:max_pdf_pages]
            truncated = True
        else:
            pages_to_process = reader.pages
            truncated = False

        pages_processed = len(pages_to_process)
        content = "\n".join((page.extract_text() or "") for page in pages_to_process)

        return {
            "success": True,
            "url": url,
            "content": content,
            "total_pages": total_pages,
            "pages_processed": pages_processed,
            "truncated": truncated,
            "error": None,
        }
    except Exception as e:
        return {
            "success": False,
            "url": url,
            "content": "",
            "total_pages": 0,
            "pages_processed": 0,
            "truncated": False,
            "error": str(e),
        }


def crawl_funding_source(
    source_id: str,
    url: str,
    discovery_config: dict,
    allowed_domains: list[str],
    test_limits: Optional[dict] = None,
    source_limits: Optional[dict] = None,
) -> CollectionResult:
    """Crawl a funding source using BFS traversal with two-stage discovery.

    Parameters
    ----------
    source_id : str
        Human-readable source name.
    url : str
        Primary URL for the source.
    discovery_config : dict
        YAML discovery block (start_urls, max_depth, pagination, etc.).
    allowed_domains : list[str]
        Additional domains allowed beyond the source's own domain.

    Returns
    -------
    CollectionResult
        With honest completeness reporting.
    """
    result = CollectionResult(source_id=source_id, method="web")

    start_urls = discovery_config.get("start_urls", [url])
    configured_max_depth = discovery_config.get("max_depth", 2)
    configured_max_pages = discovery_config.get("max_pages")  # None = no ceiling
    follow_pdfs = discovery_config.get("follow_pdfs", True)
    follow_detail = discovery_config.get("follow_detail_pages", True)
    max_pdf_pages = discovery_config.get("max_pdf_pages")  # None = process all

    if test_limits:
        configured_max_pages = test_limits.get("max_pages", configured_max_pages)
        # also apply it as a hard stop flag in the loop later, but we can just override the vars here
    if source_limits and "max_pages" in source_limits:
        configured_max_pages = source_limits["max_pages"]

    result.max_depth = configured_max_depth

    # Determine base domain from primary URL
    base_domain = urlparse(url).netloc.lower()

    # BFS queue: (url, depth)
    queue: deque[tuple[str, int]] = deque()
    visited: set[str] = set()

    for start in start_urls:
        normalized = _normalize_url(start)
        if normalized not in visited:
            queue.append((normalized, 0))
            visited.add(normalized)

    pages_fetched_count = 0

    while queue:
        current_url, depth = queue.popleft()

        from core.crawl_context import current_crawl_context
        ctx = current_crawl_context.get(None)
        source_max_documents = None
        source_max_seconds = None
        if test_limits:
            source_max_documents = test_limits.get("max_documents")
            source_max_seconds = test_limits.get("max_seconds")
        if source_limits:
            if "max_documents" in source_limits:
                source_max_documents = source_limits["max_documents"]
            if "max_seconds" in source_limits:
                source_max_seconds = source_limits["max_seconds"]
        is_limit = ctx.check_limits(
            source_max_pages=configured_max_pages,
            source_max_documents=source_max_documents,
            source_max_seconds=source_max_seconds,
            source_max_depth=configured_max_depth,
            current_depth=depth
        ) if ctx else False

        if is_limit:
            result.truncated = True
            result.pagination_complete = False
            LOGGER.info("Source %s: reached configured limits, marking PARTIAL", source_id)
            break

        # Check configured page ceiling (fallback if context isn't used)
        if configured_max_pages is not None and pages_fetched_count >= configured_max_pages:
            result.truncated = True
            result.pagination_complete = False
            LOGGER.info("Source %s: reached configured max_pages=%d, marking PARTIAL",
                        source_id, configured_max_pages)
            break

        result.pages_discovered += 1

        # Track max depth reached
        if depth > result.max_depth_reached:
            result.max_depth_reached = depth

        # Determine if this is a PDF
        is_pdf = current_url.lower().split("?", 1)[0].endswith(".pdf")

        if is_pdf:
            if not follow_pdfs:
                result.urls_skipped += 1
                continue
            result.documents_discovered += 1
            pdf_result = _fetch_pdf(current_url, max_pdf_pages=max_pdf_pages)
            if pdf_result["success"]:
                result.documents_fetched += 1
                pages_fetched_count += 1
                if ctx:
                    ctx.add_document()
                    ctx.add_page()
                if pdf_result["truncated"]:
                    result.truncated = True
                result.opportunities.append({
                    "type": "pdf",
                    "url": current_url,
                    "content": pdf_result["content"],
                    "title": urlparse(current_url).path.split("/")[-1].replace("-", " ").replace("_", " "),
                    "total_pages": pdf_result["total_pages"],
                    "pages_processed": pdf_result["pages_processed"],
                    "retrieved_at": datetime.now(UTC).isoformat(),
                })
            else:
                result.documents_failed += 1
                result.failed_urls.append(current_url)
                result.errors.append({
                    "url": current_url,
                    "operation": "pdf_fetch",
                    "error": pdf_result["error"],
                    "timestamp": datetime.now(UTC).isoformat(),
                })
            continue

        # Fetch HTML page
        try:
            res = _fetch_sync(current_url)
        except Exception as e:
            result.pages_failed += 1
            result.failed_urls.append(current_url)
            result.errors.append({
                "url": current_url,
                "operation": "page_fetch",
                "error": str(e),
                "timestamp": datetime.now(UTC).isoformat(),
            })
            continue

        if not res["success"]:
            result.pages_failed += 1
            result.failed_urls.append(current_url)
            result.errors.append({
                "url": current_url,
                "operation": "page_fetch",
                "error": res.get("error_message") or "fetch failed",
                "timestamp": datetime.now(UTC).isoformat(),
            })
            continue

        result.pages_fetched += 1
        pages_fetched_count += 1
        if ctx:
            ctx.add_page()

        page_content = res["markdown"] or res["html"] or ""
        page_title = res.get("title", "")

        # Stage B: classify this page
        is_funding_page = _classify_page_stage_b(current_url, page_title, page_content)

        if is_funding_page:
            if depth > 0:
                # This is a detail page with funding content
                result.detail_pages_discovered += 1
                result.detail_pages_fetched += 1
                
            result.opportunities.append({
                "type": "html",
                "url": current_url,
                "content": page_content,
                "title": page_title,
                "depth": depth,
                "retrieved_at": datetime.now(UTC).isoformat(),
            })

        # Discover child links if within depth boundary
        if depth < configured_max_depth:
            # Process pagination links (same depth)
            if discovery_config.get("pagination", False):
                for pag_url in res.get("pagination_links", []):
                    normalized_pag = _normalize_url(pag_url)
                    if normalized_pag not in visited:
                        if _is_allowed_domain(normalized_pag, base_domain, allowed_domains):
                            result.pagination_detected = True
                            visited.add(normalized_pag)
                            queue.append((normalized_pag, depth))  # same depth for pagination
                        else:
                            result.urls_skipped += 1

            # Process discovered links (depth + 1)
            if follow_detail:
                for link_info in res.get("links", []):
                    link_url = _normalize_url(link_info["url"])
                    if link_url in visited:
                        result.duplicates_removed += 1
                        continue

                    if not _is_allowed_domain(link_url, base_domain, allowed_domains):
                        result.urls_skipped += 1
                        continue

                    # Stage A: score the link for relevance
                    score = _classify_link_stage_a(
                        link_url,
                        link_info.get("anchor_text", ""),
                        link_info.get("surrounding_text", ""),
                    )
                    if score >= 0.4:
                        child_depth = depth + 1
                        if child_depth <= configured_max_depth:
                            visited.add(link_url)
                            queue.append((link_url, child_depth))
                        else:
                            result.urls_skipped += 1

    # Determine completeness
    if not result.truncated:
        # Queue was fully exhausted within the configured discovery boundary
        result.discovery_exhausted = True
        if result.pagination_detected:
            result.pagination_complete = True
        else:
            # No pagination detected — discovery exhausted within boundary, but
            # no pagination was present to exhaustively prove completeness.
            result.pagination_complete = False

    # Determine final status
    if result.pages_failed > 0 and result.pages_fetched == 0:
        result.final_status = "FAIL"
    elif result.truncated:
        result.final_status = "TEST_TRUNCATED" if test_limits else "PARTIAL"
    elif result.pages_fetched > 0 or result.documents_fetched > 0:
        if len(result.opportunities) > 0:
            if result.pagination_detected and result.pagination_complete:
                result.final_status = "VERIFIED"
            elif not result.pagination_detected and result.discovery_exhausted:
                result.final_status = "VERIFIED"
            else:
                result.final_status = "PARTIAL"
        else:
            result.final_status = "NO_ACTIVE_OPPORTUNITIES"
    else:
        result.final_status = "FAIL"

    result.records_discovered = len(result.opportunities)

    return result
