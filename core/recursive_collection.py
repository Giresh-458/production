from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

from core.http_client import canonicalize_url


@dataclass(slots=True)
class RecursivePage:
    url: str
    title: str
    content: str
    depth: int
    link_score: int
    parent_url: str | None = None


@dataclass(slots=True)
class RecursiveCollectionConfig:
    max_depth: int = 1
    max_pages: int = 4
    max_documents: int | None = None
    max_seconds: int | None = None
    allowed_domains: tuple[str, ...] = ()
    link_keywords: tuple[str, ...] = ()
    relevance_threshold: int = 1
    char_limit_per_page: int = 2500


@dataclass(slots=True)
class RecursiveLinkDecision:
    url: str
    parent_url: str
    anchor_text: str
    score: int
    depth: int
    action: str
    reason: str


RECURSIVE_REVIEW_PATH = Path("outputs/workflow/recursive_expansion_review.json")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _slugify(value: str) -> str:
    return "-".join(part for part in "".join(ch.lower() if ch.isalnum() else "-" for ch in value).split("-") if part)


def _make_review_id(root_url: str, root_name: str) -> str:
    digest = hashlib.sha1(f"{root_url}|{root_name}".encode("utf-8")).hexdigest()[:12]
    return f"recursive:{_slugify(root_name) or 'source'}:{digest}"


def _load_review_payload(path: Path = RECURSIVE_REVIEW_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"generated_at": "", "entries": [], "stats": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"generated_at": "", "entries": [], "stats": {}}


def _save_review_payload(payload: dict[str, Any], path: Path = RECURSIVE_REVIEW_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _rebuild_review_stats(payload: dict[str, Any]) -> None:
    entries = [entry for entry in payload.get("entries", []) if isinstance(entry, dict)]
    payload["stats"] = {
        "total_reviews": len(entries),
        "saved_artifacts": sum(1 for entry in entries if str(entry.get("outcome", "")).strip().lower() == "saved"),
        "filtered_out": sum(1 for entry in entries if "filtered" in str(entry.get("outcome", "")).strip().lower()),
        "rejected": sum(1 for entry in entries if "reject" in str(entry.get("outcome", "")).strip().lower()),
        "pending_artifacts": sum(1 for entry in entries if not str(entry.get("artifact_markdown_path", "")).strip()),
    }


def ensure_recursive_review_log(path: Path = RECURSIVE_REVIEW_PATH) -> str:
    payload = _load_review_payload(path)
    payload["generated_at"] = _iso_now()
    payload["entries"] = [entry for entry in payload.get("entries", []) if isinstance(entry, dict)]
    _rebuild_review_stats(payload)
    _save_review_payload(payload, path)
    return str(path.resolve())


def _normalize_domain(url: str) -> str:
    return urlparse(url).netloc.lower().replace("www.", "")


def default_allowed_domains(url: str) -> tuple[str, ...]:
    domain = _normalize_domain(url)
    return (domain,) if domain else ()


def _is_crawlable_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        if any(ch.isspace() for ch in url) or parsed.username or parsed.password:
            return False
        return not parsed.path.lower().endswith((
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp",
            ".mp3", ".wav", ".ogg", ".mp4", ".webm", ".ogv", ".avi", ".mov",
            ".zip", ".tar", ".gz", ".rar", ".7z", ".exe", ".dmg", ".css", ".js",
            ".woff", ".woff2", ".ttf", ".otf",
        ))
    except Exception:
        return False


def _score_link(url: str, anchor_text: str, keywords: tuple[str, ...]) -> int:
    lowered = f"{url} {anchor_text}".lower()
    score = 0
    for keyword in keywords:
        token = keyword.lower().strip()
        if token and token in lowered:
            score += 3 if token in anchor_text.lower() else 1
    path = urlparse(url).path.lower()
    if any(x in path for x in ("/rfp", "/grant", "/grants", "/funding", "/opportun", "/call", "/application", "/research", "/program", "/project", "/dataset", "/papers")):
        score += 4
    if any(x in path for x in ("/privacy", "/terms", "/login", "/contact", "/about")):
        score -= 3
    if path.endswith(".pdf"):
        score += 4
    return score


def _load_beautifulsoup() -> Any:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover - depends on optional runtime dependency
        raise RuntimeError("Recursive collection requires beautifulsoup4.") from exc
    return BeautifulSoup


def extract_ranked_links(
    *,
    base_url: str,
    html: str,
    allowed_domains: tuple[str, ...],
    link_keywords: tuple[str, ...],
    relevance_threshold: int,
) -> list[tuple[str, int, str]]:
    BeautifulSoup = _load_beautifulsoup()
    soup = BeautifulSoup(html, "xml" if str(html).lstrip().startswith("<?xml") else "html.parser")
    seen: set[str] = set()
    ranked: list[tuple[str, int, str]] = []

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        absolute = canonicalize_url(urljoin(base_url, href))
        if not _is_crawlable_url(absolute):
            continue
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        normalized_domain = _normalize_domain(absolute)
        if allowed_domains and normalized_domain not in allowed_domains:
            continue
        path_lower = parsed.path.lower()
        if path_lower.endswith((
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
            ".mp3", ".wav", ".ogg", ".mp4", ".webm", ".ogv", ".avi", ".mov",
            ".zip", ".tar", ".gz", ".exe", ".dmg", ".css", ".js",
        )):
            continue
        if absolute in seen:
            continue

        anchor_text = " ".join(anchor.get_text(" ", strip=True).split())
        score = _score_link(absolute, anchor_text, link_keywords)
        if score < relevance_threshold:
            continue
        seen.add(absolute)
        ranked.append((absolute, score, anchor_text))

    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked


def build_recursive_review(
    *,
    root_url: str,
    root_name: str,
    config: RecursiveCollectionConfig,
    pages: list[RecursivePage],
    followed_links: list[RecursiveLinkDecision],
    skipped_links: list[RecursiveLinkDecision],
) -> dict[str, Any]:
    return {
        "review_id": _make_review_id(root_url, root_name),
        "generated_at": _iso_now(),
        "root_url": root_url,
        "root_name": root_name,
        "config": {
            "max_depth": config.max_depth,
            "max_pages": config.max_pages,
            "allowed_domains": list(config.allowed_domains),
            "link_keywords": list(config.link_keywords),
            "relevance_threshold": config.relevance_threshold,
            "char_limit_per_page": config.char_limit_per_page,
        },
        "page_count": len(pages),
        "followed_links": [
            {
                "url": decision.url,
                "parent_url": decision.parent_url,
                "anchor_text": decision.anchor_text,
                "score": decision.score,
                "depth": decision.depth,
                "action": decision.action,
                "reason": decision.reason,
            }
            for decision in followed_links
        ],
        "skipped_links": [
            {
                "url": decision.url,
                "parent_url": decision.parent_url,
                "anchor_text": decision.anchor_text,
                "score": decision.score,
                "depth": decision.depth,
                "action": decision.action,
                "reason": decision.reason,
            }
            for decision in skipped_links
        ],
        "pages": [
            {
                "url": page.url,
                "title": page.title,
                "depth": page.depth,
                "link_score": page.link_score,
                "parent_url": page.parent_url,
            }
            for page in pages
        ],
        "artifact_markdown_path": "",
        "artifact_title": "",
        "artifact_source": root_url,
        "selected_area": "",
        "outcome": "collected",
        "outcome_reason": "",
    }


def save_recursive_review(review: dict[str, Any], *, path: Path = RECURSIVE_REVIEW_PATH) -> str:
    payload = _load_review_payload(path)
    entries = [entry for entry in payload.get("entries", []) if isinstance(entry, dict)]
    review_id = str(review.get("review_id", "")).strip()
    if not review_id:
        raise ValueError("recursive review must include review_id")
    updated = False
    for index, entry in enumerate(entries):
        if str(entry.get("review_id", "")).strip() == review_id:
            entries[index] = review
            updated = True
            break
    if not updated:
        entries.append(review)
    payload["generated_at"] = _iso_now()
    payload["entries"] = entries
    _rebuild_review_stats(payload)
    _save_review_payload(payload, path)
    return str(path.resolve())


def update_recursive_review(
    review_id: str,
    *,
    path: Path = RECURSIVE_REVIEW_PATH,
    **updates: Any,
) -> None:
    payload = _load_review_payload(path)
    entries = [entry for entry in payload.get("entries", []) if isinstance(entry, dict)]
    for entry in entries:
        if str(entry.get("review_id", "")).strip() != str(review_id).strip():
            continue
        for key, value in updates.items():
            entry[key] = value
        entry["updated_at"] = _iso_now()
        payload["generated_at"] = _iso_now()
        payload["entries"] = entries
        _rebuild_review_stats(payload)
        _save_review_payload(payload, path)
        return



def _sitemap_links(root_url: str, limit: int = 150) -> list[str]:
    """Best-effort, origin-bound sitemap discovery.

    Prefer Sitemap directives in robots.txt and use /sitemap.xml only as a
    single conservative fallback. Never probe a list of guessed sitemap names.
    """
    try:
        from core.http_client import get_response
        from core.crawl_context import current_crawl_context
        import time
        
        ctx = current_crawl_context.get(None)
        
        def _check_deadline():
            if ctx and ctx.deadline and time.time() >= ctx.deadline:
                return True
            return False

        parsed = urlparse(root_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        candidates: list[str] = []
        try:
            if not _check_deadline():
                import urllib.request
                with urllib.request.urlopen(f"{origin}/robots.txt", timeout=5) as rr:
                    robots = rr.read(64_000).decode("utf-8", errors="replace")
                candidates.extend(line.split(":", 1)[1].strip() for line in robots.splitlines() if line.lower().startswith("sitemap:"))
        except Exception:
            pass
        if not candidates:
            candidates.append(f"{origin}/sitemap.xml")
        candidates = [canonicalize_url(u) for u in candidates if u and urlparse(u).scheme in {"http", "https"} and _normalize_domain(u) == _normalize_domain(root_url)]
        out: list[str] = []
        for candidate in candidates[:3]:
            if _check_deadline():
                break
            try:
                response = get_response(candidate, timeout=(5.0, 15.0), retries=1)
                root = ET.fromstring(response.text)
            except Exception:
                continue
            locs = [str(el.text).strip() for el in root.iter() if el.tag.lower().split("}")[-1] == "loc" and el.text]
            if root.tag.lower().split("}")[-1] == "sitemapindex":
                for child in locs[:10]:
                    if _check_deadline():
                        break
                    if _normalize_domain(child) != _normalize_domain(root_url):
                        continue
                    try:
                        child_response = get_response(child, timeout=(5.0, 15.0), retries=1)
                        child_root = ET.fromstring(child_response.text)
                        locs.extend(str(el.text).strip() for el in child_root.iter() if el.tag.lower().split("}")[-1] == "loc" and el.text)
                    except Exception:
                        continue
            out.extend(locs)
            if len(out) >= limit:
                break
        return list(dict.fromkeys(canonicalize_url(u) for u in out if u))[:limit]
    except Exception:
        return []

def _fetch_recursive_page(url: str, fetch_html: Callable[[str], str], extract_page_text: Callable[[str], tuple[str, str]]) -> tuple[str, str, str]:
    """Return title, text, raw HTML; supports PDFs as a free fallback."""
    if urlparse(url).path.lower().endswith(".pdf"):
        from core.http_client import get_response
        response = get_response(url, timeout=(8.0, 25.0), retries=2)
        try:
            from io import BytesIO
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(response.content))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:30])
            title = Path(urlparse(url).path).stem.replace("_", " ").replace("-", " ").strip()
            return title, text, ""
        except Exception:
            return Path(urlparse(url).path).stem, "", ""
    html = fetch_html(url)
    import inspect
    if "url" in inspect.signature(extract_page_text).parameters:
        title, content = extract_page_text(html, url=url)
    else:
        title, content = extract_page_text(html)
    # Optional high-quality extraction for boilerplate-heavy sites.
    try:
        from trafilatura import bare_extraction
        doc = bare_extraction(html, url=url, include_comments=True, include_tables=True)
        better = str(getattr(doc, "text", "") or "") if doc else ""
        better_title = str(getattr(doc, "title", "") or "") if doc else ""
        if len(better) > max(400, len(content) * 0.35):
            content = better
            title = better_title or title
    except Exception:
        pass
    return title, content, html

def collect_recursive_pages(
    *,
    root_url: str,
    initial_html: str,
    fetch_html: Callable[[str], str],
    extract_page_text: Callable[[str], tuple[str, str]],
    config: RecursiveCollectionConfig,
) -> list[RecursivePage]:
    pages, _ = collect_recursive_pages_with_review(
        root_url=root_url,
        root_name=root_url,
        initial_html=initial_html,
        fetch_html=fetch_html,
        extract_page_text=extract_page_text,
        config=config,
    )
    return pages


def collect_recursive_pages_with_review(
    *,
    root_url: str,
    root_name: str,
    initial_html: str,
    fetch_html: Callable[[str], str],
    extract_page_text: Callable[[str], tuple[str, str]],
    config: RecursiveCollectionConfig,
) -> tuple[list[RecursivePage], dict[str, Any]]:
    allowed_domains = config.allowed_domains or default_allowed_domains(root_url)
    pages: list[RecursivePage] = []
    seen_urls: set[str] = set()
    seen_content_hashes: set[str] = set()
    followed_links: list[RecursiveLinkDecision] = []
    skipped_links: list[RecursiveLinkDecision] = []

    import inspect
    if "url" in inspect.signature(extract_page_text).parameters:
        root_title, root_content = extract_page_text(initial_html, url=root_url)
    else:
        root_title, root_content = extract_page_text(initial_html)
    try:
        from trafilatura import bare_extraction
        doc = bare_extraction(initial_html, url=root_url, include_comments=True, include_tables=True)
        better = str(getattr(doc, "text", "") or "") if doc else ""
        root_title = str(getattr(doc, "title", "") or "") or root_title
        if len(better) > max(400, len(root_content or "") * 0.35):
            root_content = better
    except Exception:
        pass
    trimmed_root = (root_content or "").strip()[: config.char_limit_per_page]
    root_hash = hashlib.sha1(trimmed_root.encode("utf-8")).hexdigest() if trimmed_root else ""
    pages.append(
        RecursivePage(
            url=root_url,
            title=root_title,
            content=trimmed_root,
            depth=0,
            link_score=999,
            parent_url=None,
        )
    )
    root_url = canonicalize_url(root_url)
    seen_urls.add(root_url)
    if root_hash:
        seen_content_hashes.add(root_hash)


    queue: deque[tuple[str, str, int, str | None, int]] = deque()
    from core.crawl_context import current_crawl_context
    import time
    
    def _schedule(u, html, d, p_url, p_score):
        ctx = current_crawl_context.get(None)
        if ctx:
            with ctx.lock: ctx.urls_discovered += 1
            if ctx.deadline and time.time() >= ctx.deadline:
                with ctx.lock:
                    ctx.urls_rejected_after_deadline += 1
                    ctx.stop_reasons.add("deadline")
                return False
            with ctx.lock: ctx.urls_scheduled += 1
        queue.append((u, html, d, p_url, p_score))
        return True

    _schedule(root_url, initial_html, 0, None, 999)
    # Sitemaps reveal pages that are not linked from navigation (common for
    # grant PDFs, archived calls and program pages). Keep the crawl bounded.
    for sitemap_url in _sitemap_links(root_url, limit=min(150, config.max_pages * 6)):
        if sitemap_url not in seen_urls and _normalize_domain(sitemap_url) in allowed_domains:
            _schedule(sitemap_url, "", 1, root_url, 3)

    while queue and len(pages) < config.max_pages:
        from core.crawl_context import current_crawl_context
        ctx = current_crawl_context.get(None)
        is_limit = ctx.check_limits(source_max_pages=config.max_pages, source_max_documents=config.max_documents, source_max_seconds=config.max_seconds, source_max_depth=config.max_depth, current_depth=queue[0][2]) if ctx else False
        if is_limit:
            break
            
        current_url, current_html, current_depth, parent_url, parent_score = queue.popleft()
        if current_depth >= config.max_depth:
            continue
        if not current_html:
            try:
                if ctx: 
                    with ctx.lock: ctx.urls_fetched += 1
                _, _, current_html = _fetch_recursive_page(current_url, fetch_html, extract_page_text)
                if ctx: ctx.add_page(len(current_html))
            except Exception:
                continue

        ranked_links = extract_ranked_links(
            base_url=current_url,
            html=current_html,
            allowed_domains=allowed_domains,
            link_keywords=config.link_keywords,
            relevance_threshold=config.relevance_threshold,
        )
        for child_url, score, anchor_text in ranked_links[: min(50, config.max_pages - len(pages))]:
            if ctx and ctx.check_limits(source_max_pages=config.max_pages, source_max_documents=config.max_documents, source_max_seconds=config.max_seconds, source_max_depth=config.max_depth, current_depth=current_depth + 1):
                break
            if len(pages) >= config.max_pages:
                skipped_links.append(
                    RecursiveLinkDecision(
                        url=child_url,
                        parent_url=current_url,
                        anchor_text=anchor_text,
                        score=score,
                        depth=current_depth + 1,
                        action="skipped",
                        reason="max_pages_reached",
                    )
                )
                break
            if child_url in seen_urls:
                skipped_links.append(
                    RecursiveLinkDecision(
                        url=child_url,
                        parent_url=current_url,
                        anchor_text=anchor_text,
                        score=score,
                        depth=current_depth + 1,
                        action="skipped",
                        reason="already_seen_url",
                    )
                )
                continue
            child_url = canonicalize_url(child_url)
            seen_urls.add(child_url)
            try:
                if ctx: 
                    with ctx.lock: ctx.urls_fetched += 1
                child_title, child_content, child_html = _fetch_recursive_page(child_url, fetch_html, extract_page_text)
                if ctx: ctx.add_page(len(child_html) if child_html else 0)
            except Exception:
                skipped_links.append(
                    RecursiveLinkDecision(
                        url=child_url,
                        parent_url=current_url,
                        anchor_text=anchor_text,
                        score=score,
                        depth=current_depth + 1,
                        action="skipped",
                        reason="fetch_failed",
                    )
                )
                continue
            trimmed_content = (child_content or "").strip()[: config.char_limit_per_page]
            if not trimmed_content:
                skipped_links.append(
                    RecursiveLinkDecision(
                        url=child_url,
                        parent_url=current_url,
                        anchor_text=anchor_text,
                        score=score,
                        depth=current_depth + 1,
                        action="skipped",
                        reason="empty_content",
                    )
                )
                continue
            content_hash = hashlib.sha1(trimmed_content.encode("utf-8")).hexdigest()
            if content_hash in seen_content_hashes:
                skipped_links.append(
                    RecursiveLinkDecision(
                        url=child_url,
                        parent_url=current_url,
                        anchor_text=anchor_text,
                        score=score,
                        depth=current_depth + 1,
                        action="skipped",
                        reason="duplicate_content",
                    )
                )
                continue
            seen_content_hashes.add(content_hash)
            pages.append(
                RecursivePage(
                    url=child_url,
                    title=child_title,
                    content=trimmed_content,
                    depth=current_depth + 1,
                    link_score=score,
                    parent_url=current_url,
                )
            )
            if ctx: ctx.add_document()
            followed_links.append(
                RecursiveLinkDecision(
                    url=child_url,
                    parent_url=current_url,
                    anchor_text=anchor_text,
                    score=score,
                    depth=current_depth + 1,
                    action="followed",
                    reason="ranked_link_selected",
                )
            )
            if current_depth + 1 < config.max_depth:
                _schedule(child_url, child_html, current_depth + 1, current_url, score)

    if len(pages) >= config.max_pages:
        from core.crawl_context import current_crawl_context
        ctx = current_crawl_context.get(None)
        if ctx: 
            with ctx.lock: ctx.stop_reasons.add("source_max_pages")

    review = build_recursive_review(
        root_url=root_url,
        root_name=root_name,
        config=RecursiveCollectionConfig(
            max_depth=config.max_depth,
            max_pages=config.max_pages,
            allowed_domains=allowed_domains,
            link_keywords=config.link_keywords,
            relevance_threshold=config.relevance_threshold,
            char_limit_per_page=config.char_limit_per_page,
        ),
        pages=pages,
        followed_links=followed_links,
        skipped_links=skipped_links,
    )
    return pages, review


def combine_recursive_pages(
    pages: list[RecursivePage],
    *,
    root_name: str = "",
    focus: str = "",
    char_limit: int = 12000,
) -> tuple[str, str]:
    if not pages:
        return root_name or "Untitled", ""

    title = pages[0].title or root_name or "Untitled"
    blocks: list[str] = []
    prefix_bits = [bit for bit in (root_name, focus) if bit]
    for page in pages:
        header = f"[Depth {page.depth}] {page.title} | {page.url}"
        body = page.content.strip()
        if prefix_bits and page.depth == 0:
            body = f"{' '.join(prefix_bits)} {body}".strip()
        blocks.append(f"{header}\n{body}".strip())

    combined = "\n\n".join(blocks).strip()
    return title, combined[:char_limit]
