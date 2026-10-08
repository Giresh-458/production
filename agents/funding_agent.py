from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import sys
import textwrap
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, date
from pathlib import Path
from typing import Any
from core.funding_selection import FundingCallContext
from core.funding_context import build_agent_specific_funding_context, current_funding_prompt_context, current_funding_queries, generate_funding_queries
from urllib.parse import urljoin, urlparse

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.db import db_connect
from agents.utils import cleaned_text
from core.focused_extraction import build_focused_content, extract_text_blocks
from core.recursive_collection import (
    RecursiveCollectionConfig,
    collect_recursive_pages,
    collect_recursive_pages_with_review,
    combine_recursive_pages,
    default_allowed_domains,
    save_recursive_review,
    update_recursive_review,
)
from core.schemas import resolve_research_area
from core.source_registry import apply_refresh_policy
from core.structured_sources import collect_structured_source
from core.web_collection import collect_source
from core.http_client import canonicalize_url
DEFAULT_OUTPUT_DIR = Path("outputs/funding")
DEFAULT_DB_PATH = DEFAULT_OUTPUT_DIR / "funding_memory.db"
DEFAULT_SOURCES_PATH = Path("sources/funding_sources.yaml")
DEFAULT_POSTS_PER_SOURCE = 8
LOOKBACK_YEARS = 5
RELEVANCE_KEYWORDS = [
    "grant",
    "funding",
    "research",
    "proposal",
    "call for",
    "rfp",
    "application",
    "deadline",
    "apply",
    "ecosystem support",
    "program",
]
AREA_FILTER_KEYWORDS = {
    "RWA": ["rwa", "real-world asset", "real world asset", "tokenized treasury", "tokenized fund", "asset tokenization"],
    "ESG": ["esg", "carbon", "mrv", "climate", "sustainability", "carbon credit"],
    "ZK-IoV": ["zk", "zero-knowledge", "zero knowledge", "vehicular", "internet of vehicles", "iov"],
    "DID": ["did", "decentralized identity", "verifiable credential", "identity"],
    "DePIN": ["depin", "decentralized physical infrastructure", "wireless", "storage network", "sensor network", "mapping"],
    "MEV": ["mev", "block builder", "transaction ordering", "order flow"],
    "Stablecoins": ["stablecoin", "settlement", "tokenized deposit", "digital dollar", "payment rail"],
}
POST_HINTS = [
    "/blog/",
    "/news",
    "/grants",
    "/grant",
    "/research",
    "/program",
    "/funding",
    "/articles/",
    "/post/",
]
FOCUSED_CONTENT_KEYWORDS = [
    "grant",
    "grants",
    "funding",
    "program",
    "call",
    "apply",
    "application",
    "eligibility",
    "deadline",
    "award",
    "research",
    "builder",
    "proposal",
    "rfp",
]
FOCUS_AREAS = ["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"]

GENERIC_PAGE_TITLES = {
    "how to apply", "apply", "home", "overview", "open rounds",
    "request for proposals", "rpf", "funding", "grants", "programs",
}

def extract_page_title(soup: Any, fallback: str) -> str:
    """Choose the call/program title, avoiding generic site chrome headings."""
    candidates: list[str] = []
    for selector in (
        ("meta", {"property": "og:title"}),
        ("meta", {"name": "twitter:title"}),
    ):
        tag = soup.find(*selector)
        if tag and tag.get("content"):
            candidates.append(cleaned_text(str(tag.get("content"))))
    if soup.title:
        candidates.append(cleaned_text(soup.title.get_text(" ", strip=True)))
    for heading in soup.find_all(["h1", "h2", "h3"]):
        candidates.append(cleaned_text(heading.get_text(" ", strip=True)))
    for candidate in candidates:
        normalized = re.sub(r"\s+", " ", candidate).strip()
        if not normalized:
            continue
        if normalized.lower().rstrip(" .:-") in GENERIC_PAGE_TITLES:
            continue
        if len(normalized) > 180:
            normalized = normalized[:180].rsplit(" ", 1)[0]
        return normalized
    return fallback

def is_probable_funding_call_page(url: str, title: str, content: str, application_links: list[str]) -> bool:
    """Require concrete call identity before treating a page as a funding call."""
    path = urlparse(url).path.lower()
    text = f"{title} {content}".lower()
    explicit_call = any(token in text for token in (
        "request for proposals", "call for proposals", "call for applications",
        "application deadline", "submission deadline", "hard requirements",
        "timeline", "eligible", "who can apply", "selected rfp",
        "applications are open", "now accepting applications", "deadline:",
    ))
    url_call = any(token in path for token in ("/rfp/", "/grant", "/funding", "/call/", "/opportun", "/fellowship", "/program/"))
    title_call = any(token in title.lower() for token in ("rfp", "grant", "fund", "program", "fellowship", "call", "challenge"))
    application = bool(application_links)
    # News/blog pages need much stronger evidence than a normal call endpoint.
    editorial = any(token in path for token in ("/blog/", "/news/", "/press/", "/article/"))
    if editorial and not (application and explicit_call and ("deadline" in text or "timeline" in text)):
        return False
    return (url_call and (explicit_call or application)) or (title_call and explicit_call) or (application and explicit_call)
LOGGER = logging.getLogger("rif.funding_agent")
RECURSIVE_DIRECT_PAGE_CONFIG = RecursiveCollectionConfig(
    # Funding/RFP pages often link from a landing page to the actual call,
    # eligibility, FAQ and application portal. Give the crawler enough budget
    # to inspect those pages without turning it into an unrestricted spider.
    max_depth=2,
    max_pages=15,
    allowed_domains=(),
    link_keywords=tuple(sorted(set(["rfp", "request for proposal", "call for proposals", "open call", "application", "apply", "deadline", "grant", "funding opportunity", "fellowship", "competition", "research funding"]))),
    relevance_threshold=2,
    char_limit_per_page=2200,
)


@dataclass(slots=True)
class FundingSource:
    name: str
    url: str
    tier: str
    focus: str = ""
    collection: dict = field(default_factory=dict)


@dataclass(slots=True)
class FundingDocument:
    organization: str
    title: str
    source: str
    source_type: str
    content: str
    year: int
    recursive_review_id: str = ""
    recursive_page_count: int = 1


@dataclass(slots=True)
class FundingOpportunity:
    funding_body: str
    program_name: str
    call_id: str | None
    status: str
    opening_date: str | None
    deadline: str | None
    funding_amount: str | None
    currency: str | None
    duration: str | None
    eligibility: str | None
    geography: str | None
    trl: str | None
    research_priorities: list[str]
    required_partners: str | None
    deliverables: str | None
    evaluation_criteria: str | None
    eligible_costs: str | None
    application_url: str | None
    # Original analysis fields
    opportunity_type: str | None
    focus_area: str
    keywords: list[str]
    research_context: dict | None
    source_method: str
    # Provenance
    source_url: str
    source_title: str
    retrieved_at: str
    published_at: str | None
    last_verified_at: str
    evidence: dict[str, str] = field(default_factory=dict)


class FundingAgentError(Exception):
    """Raised when the funding agent cannot complete a task."""


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "item"


def normalize_tag(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return f"#{value}" if value else ""


def current_year() -> int:
    return datetime.now(UTC).year


def init_funding_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        '''
        CREATE TABLE IF NOT EXISTS funding_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization TEXT NOT NULL,
            title TEXT NOT NULL,
            year INTEGER NOT NULL,
            problem TEXT NOT NULL,
            keywords TEXT NOT NULL,
            url TEXT NOT NULL,
            focus_area TEXT NOT NULL,
            requirements TEXT NOT NULL,
            problem_signature TEXT NOT NULL,
            source_type TEXT NOT NULL,
            path TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        '''
    )
    cursor = connection.cursor()
    cursor.execute("PRAGMA table_info(funding_data)")
    columns = [col[1] for col in cursor.fetchall()]
    
    new_columns = {
        'funding_body': 'TEXT', 'program_name': 'TEXT', 'call_id': 'TEXT', 'status': 'TEXT',
        'opening_date': 'TEXT', 'deadline': 'TEXT', 'funding_amount': 'TEXT', 'currency': 'TEXT',
        'duration': 'TEXT', 'eligibility': 'TEXT', 'geography': 'TEXT', 'trl': 'TEXT',
        'research_priorities': 'TEXT', 'required_partners': 'TEXT', 'deliverables': 'TEXT',
        'evaluation_criteria': 'TEXT', 'eligible_costs': 'TEXT', 'application_url': 'TEXT',
        'source_url': 'TEXT', 'source_title': 'TEXT', 'retrieved_at': 'TEXT', 
        'published_at': 'TEXT', 'last_verified_at': 'TEXT'
    }
    
    for col_name, col_type in new_columns.items():
        if col_name not in columns:
            cursor.execute(f"ALTER TABLE funding_data ADD COLUMN {col_name} {col_type}")
            
    # Legacy databases can contain duplicate URLs from earlier versions where
    # source_url and application_url were treated as different identities.
    # Deduplicate before adding the uniqueness constraint so upgrades never fail
    # halfway through startup.
    duplicates = connection.execute(
        "SELECT url, GROUP_CONCAT(id), COUNT(*) FROM funding_data WHERE url IS NOT NULL AND url <> '' GROUP BY url HAVING COUNT(*) > 1"
    ).fetchall()
    for url, ids_csv, _count in duplicates:
        ids = [int(x) for x in str(ids_csv).split(',') if str(x).isdigit()]
        for duplicate_id in ids[:-1]:
            connection.execute("DELETE FROM funding_data WHERE id = ?", (duplicate_id,))
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_funding_url ON funding_data(url)")
    connection.commit()


def funding_exists(connection: sqlite3.Connection, title: str, source_url: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM funding_data WHERE title = ? OR url = ?",
        (title, source_url),
    ).fetchone()
    return row is not None


def save_funding_record(
    connection: sqlite3.Connection,
    *,
    opportunity: FundingOpportunity,
    path: str,
    source_type: str,
    year: int
) -> None:
    cursor = connection.cursor()
    cursor.execute(
        '''
        INSERT INTO funding_data (
            organization, title, year, problem, keywords, url, focus_area, requirements,
            problem_signature, source_type, path, created_at,
            funding_body, program_name, call_id, status, opening_date, deadline,
            funding_amount, currency, duration, eligibility, geography, trl,
            research_priorities, required_partners, deliverables, evaluation_criteria,
            eligible_costs, application_url, source_url, source_title, retrieved_at,
            published_at, last_verified_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(url) DO UPDATE SET
                organization=excluded.organization,
                title=excluded.title,
                year=excluded.year,
                problem=excluded.problem,
                keywords=excluded.keywords,
                focus_area=excluded.focus_area,
                requirements=excluded.requirements,
                problem_signature=excluded.problem_signature,
                source_type=excluded.source_type,
                path=excluded.path,
                created_at=excluded.created_at,
                funding_body=excluded.funding_body,
                program_name=excluded.program_name,
                call_id=excluded.call_id,
                status=excluded.status,
                opening_date=excluded.opening_date,
                deadline=excluded.deadline,
                funding_amount=excluded.funding_amount,
                currency=excluded.currency,
                duration=excluded.duration,
                eligibility=excluded.eligibility,
                geography=excluded.geography,
                trl=excluded.trl,
                research_priorities=excluded.research_priorities,
                required_partners=excluded.required_partners,
                deliverables=excluded.deliverables,
                evaluation_criteria=excluded.evaluation_criteria,
                eligible_costs=excluded.eligible_costs,
                application_url=excluded.application_url,
                source_url=excluded.source_url,
                source_title=excluded.source_title,
                retrieved_at=excluded.retrieved_at,
                published_at=excluded.published_at,
                last_verified_at=excluded.last_verified_at
        ''',
        (
            opportunity.funding_body or opportunity.program_name or "Unknown", 
            opportunity.program_name or "Unknown", 
            year, 
            (opportunity.research_context.get("inferred_challenges") or [""])[0] if opportunity.research_context else "",
            json.dumps(opportunity.keywords), 
            opportunity.source_url,
            opportunity.focus_area, 
            "", 
            "",
            source_type, 
            path, 
            datetime.now(UTC).isoformat(),
            opportunity.funding_body, opportunity.program_name, opportunity.call_id, opportunity.status,
            opportunity.opening_date, opportunity.deadline, opportunity.funding_amount, opportunity.currency,
            opportunity.duration, opportunity.eligibility, opportunity.geography, opportunity.trl,
            json.dumps(opportunity.research_priorities), opportunity.required_partners, opportunity.deliverables,
            opportunity.evaluation_criteria, opportunity.eligible_costs, opportunity.application_url,
            opportunity.source_url, opportunity.source_title, opportunity.retrieved_at,
            opportunity.published_at, opportunity.last_verified_at
        )
    )
    connection.commit()



def load_requests_and_bs4() -> tuple[object, object]:
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise FundingAgentError(
            "Automatic and URL modes require 'requests' and 'beautifulsoup4'. Install them before running those modes."
        ) from exc
    return requests, BeautifulSoup


def load_sources(sources_path: Path) -> list[FundingSource]:
    if not sources_path.exists():
        raise FundingAgentError(f"Funding sources file not found: {sources_path}")

    try:
        data = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise FundingAgentError(f"Invalid YAML in {sources_path}: {exc}") from exc

    sources_root = data.get("sources", {})
    sources: list[FundingSource] = []
    for tier, entries in sources_root.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = cleaned_text(str(entry.get("name", "")))
            url = cleaned_text(str(entry.get("url", "")))
            focus = cleaned_text(str(entry.get("focus", "")))
            collection = entry.get("collection", {})
            if name and url:
                sources.append(FundingSource(name=name, url=url, tier=str(tier), focus=focus, collection=collection))
    if not sources:
        raise FundingAgentError("No funding sources were loaded from sources/funding_sources.yaml.")
    return apply_refresh_policy("funding", sources)


def fetch_html(url: str) -> str:
    from core.http_client import fetch_text
    try:
        text, final_url = fetch_text(url, timeout=(8.0, 20.0), retries=1)
        LOGGER.debug("Fetched %s -> %s", url, final_url)
        return text
    except Exception as exc:
        # Anti-bot/403 pages can still be available through a real browser.
        # Reuse the shared bounded Playwright fallback rather than silently
        # discarding an otherwise valid public funding source.
        try:
            from core.web_collection import render_page_with_browser
            rendered = render_page_with_browser(url)
            if rendered and rendered[1].strip():
                title, text, links, final_url = rendered
                from html import escape
                link_html = " ".join(f'<a href="{escape(href, quote=True)}">{escape(label)}</a>' for href, label in links[:200])
                return f"<html><head><title>{escape(title)}</title></head><body><h1>{escape(title)}</h1><p>{escape(text)}</p>{link_html}</body></html>"
        except Exception as browser_exc:
            LOGGER.warning("Browser fallback failed for %s: %s", url, browser_exc)
        raise FundingAgentError(f"Unable to fetch page {url}: {exc}") from exc

def parse_date_string(value: str) -> datetime | None:
    """Parse common web/funding date representations conservatively.

    Returns UTC midnight for date-only values. We intentionally do not infer
    missing years because a funding deadline must never be guessed.
    """
    cleaned = cleaned_text(value)
    if not cleaned:
        return None
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    candidates = [cleaned, cleaned.rstrip(".")]
    if cleaned.endswith("Z"):
        candidates.append(cleaned[:-1] + "+00:00")
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            pass

    # ISO/date strings with a time or timezone suffix that fromisoformat may
    # reject because the source contains human text around the date.
    formats = [
        "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y",
        "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
        "%d %B %Y", "%d %b %Y", "%dth %B %Y", "%dst %B %Y",
        "%dnd %B %Y", "%dth %b %Y", "%dst %b %Y", "%dnd %b %Y",
    ]
    for candidate in candidates:
        for fmt in formats:
            try:
                return datetime.strptime(candidate, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue

    # Extract a date embedded in a sentence, e.g.
    # "Applications close at 23:59 IST on 30 September 2026".
    month = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    patterns = [
        rf"\b({month})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,)?\s+(20\d{{2}})\b",
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month})\s+(20\d{{2}})\b",
        r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b",
        r"\b(\d{1,2})[-/](\d{1,2})[-/](20\d{2})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if not m:
            continue
        try:
            if m.group(1).isdigit() and len(m.group(1)) == 4:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=UTC)
            if m.group(3).isdigit() and len(m.group(3)) == 4 and not m.group(1).isdigit():
                month_name, day_num, year_num = m.group(1), int(m.group(2)), int(m.group(3))
                return datetime.strptime(f"{day_num} {month_name} {year_num}", "%d %B %Y").replace(tzinfo=UTC) if month_name.lower() in {x.lower() for x in ["January","February","March","April","May","June","July","August","September","October","November","December"]} else datetime.strptime(f"{day_num} {month_name} {year_num}", "%d %b %Y").replace(tzinfo=UTC)
            if m.group(3).isdigit() and len(m.group(3)) == 4:
                # For numeric D/M/Y, prefer day-first for funding pages.
                return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=UTC)
        except ValueError:
            continue
    return None


def extract_structured_metadata(soup: Any) -> dict[str, Any]:
    """Read JSON-LD/OpenGraph metadata commonly used by modern funding sites."""
    result: dict[str, Any] = {"dates": [], "application_urls": []}
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for key in ("datePosted", "startDate", "validFrom", "endDate", "validThrough", "deadline"):
                value = node.get(key)
                if value:
                    result["dates"].append((key, str(value)))
            for key in ("url", "applicationUrl", "applyUrl", "sameAs"):
                value = node.get(key)
                values = value if isinstance(value, list) else [value]
                for item in values:
                    if isinstance(item, str) and item.startswith(("http://", "https://")):
                        if any(token in item.lower() for token in ("apply", "application", "submit", "proposal", "rfp")):
                            result["application_urls"].append(item)
    # Common OpenGraph/application metadata.
    for tag in soup.find_all("meta"):
        key = str(tag.get("property") or tag.get("name") or "").lower()
        value = str(tag.get("content") or "").strip()
        if value and any(token in key for token in ("deadline", "enddate", "validthrough", "application-url", "apply-url")):
            result["dates"].append((key, value))
            if value.startswith(("http://", "https://")):
                result["application_urls"].append(value)
    result["application_urls"] = list(dict.fromkeys(result["application_urls"]))
    return result


def extract_published_date(url: str, soup: Any) -> datetime | None:
    selectors = [
        ("meta", {"property": "article:published_time"}, "content"),
        ("meta", {"name": "article:published_time"}, "content"),
        ("meta", {"property": "og:published_time"}, "content"),
        ("meta", {"name": "publish-date"}, "content"),
        ("meta", {"name": "date"}, "content"),
    ]
    for tag_name, attrs, key in selectors:
        tag = soup.find(tag_name, attrs=attrs)
        if tag and tag.get(key):
            parsed = parse_date_string(tag.get(key))
            if parsed:
                return parsed

    for time_tag in soup.find_all("time"):
        parsed = parse_date_string(time_tag.get("datetime") or time_tag.get_text(" ", strip=True))
        if parsed:
            return parsed

    url_match = re.search(r"(20\d{2})[/-](\d{2})[/-](\d{2})", url)
    if url_match:
        try:
            return datetime(
                int(url_match.group(1)),
                int(url_match.group(2)),
                int(url_match.group(3)),
                tzinfo=UTC,
            )
        except ValueError:
            return None
    return None


def is_recent(published_at: datetime | None) -> bool:
    if published_at is None:
        return False
    return published_at.year >= current_year() - (LOOKBACK_YEARS - 1)


def is_relevant(title: str, content: str) -> bool:
    lowered = f"{title} {content}".lower()
    return any(keyword in lowered for keyword in RELEVANCE_KEYWORDS)


def matches_area(title: str, content: str, area: str | None, focus: str = "") -> bool:
    if not area:
        return True
    keywords = AREA_FILTER_KEYWORDS.get(area, [])
    if not keywords:
        return True
    lowered = f"{title} {focus} {content}".lower()
    return any(keyword in lowered for keyword in keywords)


def extract_candidate_links(source: FundingSource, html: str, posts_per_source: int) -> list[str]:
    """Rank likely funding-call/application pages on a source landing page."""
    _, BeautifulSoup = load_requests_and_bs4()
    soup = BeautifulSoup(html, "html.parser")
    base = urlparse(source.url)
    base_domain = base.netloc.lower().replace("www.", "")
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    strong = ("rfp", "request for proposal", "call for", "open call", "apply", "application", "deadline", "grant", "funding", "opportunity", "fellowship")
    weak = POST_HINTS + RELEVANCE_KEYWORDS
    reject = ("privacy", "terms", "contact", "about", "press", "blog", "news", "awarded", "recipients", "past-round", "previous-round")
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        absolute = urljoin(source.url, href)
        parsed = urlparse(absolute)
        domain = parsed.netloc.lower().replace("www.", "")
        if parsed.scheme not in {"http", "https"} or domain != base_domain:
            continue
        if absolute in seen or absolute == source.url:
            continue
        seen.add(absolute)
        anchor_text = cleaned_text(anchor.get_text(" ", strip=True))
        lowered = f"{absolute} {anchor_text}".lower()
        score = sum(3 for token in strong if token in lowered) + sum(1 for token in weak if token.lower() in lowered)
        if any(token in lowered for token in reject):
            score -= 2
        if score >= 3:
            scored.append((score, absolute))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [url for _, url in scored[:posts_per_source]]


def fetch_post_document(source: FundingSource, post_url: str, allow_undated: bool = False) -> FundingDocument | None:
    _, BeautifulSoup = load_requests_and_bs4()
    html = fetch_html(post_url)
    soup = BeautifulSoup(html, "html.parser")
    title = cleaned_text((soup.find("h1") or soup.title).get_text(" ", strip=True)) if (soup.find("h1") or soup.title) else post_url
    content = cleaned_text(" ".join(node.get_text(" ", strip=True) for node in soup.find_all(["h1", "h2", "h3", "p", "li"])))
    if not content or not is_relevant(title, content):
        return None

    published_at = extract_published_date(post_url, soup)
    if published_at is None and allow_undated and is_relevant(title, content):
        published_at = datetime(current_year(), 1, 1, tzinfo=UTC)
    if not is_recent(published_at):
        return None

    return FundingDocument(
        organization=source.name,
        title=title,
        source=post_url,
        source_type="Automatic",
        content=content[:7000],
        year=published_at.year if published_at else current_year(),
    )


def extract_application_links(base_url: str, soup: Any) -> list[str]:
    """Extract actual application/submit links from HTML anchors, not just visible text."""
    found: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        absolute = urljoin(base_url, href)
        label = cleaned_text(anchor.get_text(" ", strip=True)).lower()
        haystack = f"{absolute.lower()} {label}"
        if any(token in haystack for token in ("apply", "application", "submit", "submission", "proposal")):
            if absolute not in found:
                found.append(absolute)
    return found[:8]


def extract_basic_page_text(html: str, *, url: str = "") -> tuple[str, str]:
    _, BeautifulSoup = load_requests_and_bs4()
    
    if "pdf" in html[:10].lower() or url.lower().split("?", 1)[0].endswith(".pdf"):
        title = Path(urlparse(url).path).stem.replace("_", " ").replace("-", " ").strip() or "Untitled PDF"
        return title, cleaned_text(html)[:9000]

    soup = BeautifulSoup(html, "html.parser")
    title = extract_page_title(soup, url)
    blocks = extract_text_blocks(soup)
    content = cleaned_text(" ".join(blocks))
    focused_content = build_focused_content(
        title=title,
        blocks=blocks,
        focus_keywords=FOCUSED_CONTENT_KEYWORDS + RELEVANCE_KEYWORDS,
        area_keywords=[keyword for keywords in AREA_FILTER_KEYWORDS.values() for keyword in keywords],
        min_score=3,
        max_chars=6000,
    )
    application_links = extract_application_links(url, soup) if url else []
    link_text = " ".join(f"Application URL: {u}" for u in application_links)
    return title, (cleaned_text(f"{focused_content or content} {link_text}"))[:9000]


def build_manual_url_document(url: str, organization: str | None) -> FundingDocument:
    html = fetch_html(url)
    pages = collect_recursive_pages(
        root_url=url,
        initial_html=html,
        fetch_html=fetch_html,
        extract_page_text=extract_basic_page_text,
        config=RecursiveCollectionConfig(
            max_depth=RECURSIVE_DIRECT_PAGE_CONFIG.max_depth,
            max_pages=RECURSIVE_DIRECT_PAGE_CONFIG.max_pages,
            allowed_domains=default_allowed_domains(url),
            link_keywords=RECURSIVE_DIRECT_PAGE_CONFIG.link_keywords,
            relevance_threshold=RECURSIVE_DIRECT_PAGE_CONFIG.relevance_threshold,
            char_limit_per_page=RECURSIVE_DIRECT_PAGE_CONFIG.char_limit_per_page,
        ),
    )
    title, content = combine_recursive_pages(pages, root_name=organization or urlparse(url).netloc, focus="")
    _, BeautifulSoup = load_requests_and_bs4()
    soup = BeautifulSoup(html, "html.parser")
    published_at = extract_published_date(url, soup)
    org = organization or urlparse(url).netloc
    return FundingDocument(
        organization=org,
        title=title,
        source=url,
        source_type="Manual",
        content=content,
        year=published_at.year if published_at else current_year(),
    )


def build_manual_text_document(text: str, organization: str, title: str | None, year: int | None) -> FundingDocument:
    guessed_title = title or cleaned_text(text.splitlines()[0])[:120] or f"{organization} funding input"
    manual_source = f"manual://{slugify(organization)}/{slugify(guessed_title)}"
    return FundingDocument(
        organization=organization,
        title=guessed_title,
        source=manual_source,
        source_type="Manual",
        content=cleaned_text(text),
        year=year or current_year(),
    )


def build_analysis_prompt(document: FundingDocument) -> str:
    area_text = ", ".join(FOCUS_AREAS)
    return textwrap.dedent(
        f'''\
        Task: extract funding signal. Do not invent missing information; return null/UNKNOWN if missing.

        Organization: {document.organization}
        Title: {document.title}
        Year: {document.year}

        Content:
        {document.content[:5000]}

        Return JSON only with keys:
        - funding_body (string or null)
        - program_name (string or null)
        - call_id (string or null)
        - extracted_status (string: OPEN_CALL, UPCOMING_CALL, CLOSED_CALL, GENERAL_FUNDING_PROGRAM, FUNDED_PROJECT, FUNDING_NEWS, or null)
        - opening_date (YYYY-MM-DD or null)
        - deadline (YYYY-MM-DD or null)
        - funding_amount (string or null)
        - currency (string or null)
        - duration (string or null)
        - eligibility (string or null)
        - geography (string or null)
        - trl (string or null)
        - research_priorities (list of strings)
        - required_partners (string or null)
        - deliverables (string or null)
        - evaluation_criteria (string or null)
        - eligible_costs (string or null)
        - application_url (string or null)
        - focus_area (one of {area_text})
        - keywords (up to 5 lowercase items)
        - requirements (short sentence, or "Not specified")

        Rules:
        - If dates or exact funding amounts aren't explicitly stated, return null. Do NOT guess.
        - no markdown
        '''
    )



def parse_llm_response(text: str) -> dict[str, object]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise FundingAgentError("Local LLM response was not valid JSON.")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise FundingAgentError(f"Local LLM returned invalid JSON: {exc}") from exc

    return parsed

def _normalize_date(value: str | None) -> str | None:
    parsed = parse_date_string(value or "")
    return parsed.date().isoformat() if parsed else None



def classify_sections(content: str) -> dict[str, str]:
    sections = {
        "CALL_IDENTITY": "",
        "STATUS": "",
        "FUNDING": "",
        "ELIGIBILITY": "",
        "TECHNOLOGY_FOCUS": "",
        "RESEARCH_PRIORITIES": "",
        "PROGRAM_SUPPORT": "",
        "DELIVERABLES": "",
        "EVALUATION": "",
        "COMMERCIALISATION": "",
        "APPLICATION": "",
        "OTHER": ""
    }
    
    lines = content.split("\n")
    current_section = "OTHER"
    
    for line in lines:
        lower = line.lower()
        if "who can apply" in lower or "eligibility" in lower:
            current_section = "ELIGIBILITY"
        elif "program highlights" in lower or "support" in lower or "what we offer" in lower:
            current_section = "PROGRAM_SUPPORT"
        elif "technology" in lower or "focus" in lower or "domain" in lower:
            current_section = "TECHNOLOGY_FOCUS"
        elif "funding" in lower or "financial" in lower or "grant" in lower or "₹" in lower or "$" in lower:
            current_section = "FUNDING"
        elif "commercial" in lower or "market" in lower or "scale" in lower:
            current_section = "COMMERCIALISATION"
        elif "apply" in lower or "application" in lower:
            current_section = "APPLICATION"
        elif "research" in lower or "priority" in lower or "theme" in lower:
            current_section = "RESEARCH_PRIORITIES"
        elif "deliverable" in lower or "outcome" in lower:
            current_section = "DELIVERABLES"
        elif "evaluat" in lower or "criteria" in lower:
            current_section = "EVALUATION"
        elif "about" in lower or "introduction" in lower:
            current_section = "CALL_IDENTITY"
            
        sections[current_section] += line + "\n"
        
    return {k: v.strip() for k, v in sections.items() if v.strip()}

def _parse_date(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text))
    for candidate in (text, text.split("T", 1)[0]):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            dt = dt.astimezone(UTC)
            if date_only and end_of_day:
                dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
            return dt
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def determine_status(explicit_status: str | None, opening: str | None, deadline: str | None, content: str = "", title: str = "") -> str:
    """Classify a funding page using dates first, then explicit call language.

    Positive words such as ``apply`` alone are not enough to call a page open;
    this avoids turning generic program pages into fake open opportunities.
    """
    if explicit_status:
        normalized = str(explicit_status).strip().upper()
        aliases = {"FUNDED": "FUNDED_PROJECT", "OPEN": "OPEN_CALL", "CLOSED": "CLOSED_CALL", "UPCOMING": "UPCOMING_CALL"}
        if normalized in aliases:
            normalized = aliases[normalized]
        if normalized in {"FUNDED_PROJECT", "CLOSED_CALL", "UPCOMING_CALL", "OPEN_CALL", "GENERAL_FUNDING_PROGRAM", "FUNDING_NEWS"}:
            return normalized
    lowered = re.sub(r"\s+", " ", (content or "").lower())
    if any(x in lowered for x in ("awarded", "selected projects", "fund recipients", "startups selected", "grant recipients")) and not any(x in lowered for x in ("applications are open", "now accepting applications")):
        return "FUNDED_PROJECT"

    opening_dt = _parse_date(opening)
    deadline_dt = _parse_date(deadline, end_of_day=True)
    now = datetime.now(UTC)
    if deadline_dt and deadline_dt < now:
        return "CLOSED_CALL"
    if opening_dt and opening_dt > now:
        return "UPCOMING_CALL"
    if opening_dt and opening_dt <= now and (deadline_dt is None or deadline_dt >= now):
        return "OPEN_CALL"

    if any(x in lowered for x in ("applications closed", "deadline has passed", "application period has ended", "closed for applications", "no longer accepting applications")):
        return "CLOSED_CALL"
    if any(x in lowered for x in ("will open", "coming soon", "opens on", "applications open on")):
        return "UPCOMING_CALL"

    explicit_open = any(x in lowered for x in ("applications are open", "applications are now open", "now accepting applications", "accepting applications now", "open for applications", "call is open", "rfp is open"))
    call_signal = sum(1 for x in ("request for proposals", "call for proposals", "call for applications", "rfp", "application deadline", "submission deadline", "apply now", "submit a proposal", "invite you", "invite applications", "application link", "apply for") if x in lowered)
    if explicit_open and call_signal >= 1:
        status = "OPEN_CALL"
    elif call_signal >= 2 and (explicit_open or ("application link" in lowered and any(x in lowered for x in ("apply for", "apply now", "submit")))):
        status = "OPEN_CALL"
    else:
        status = "UNKNOWN"
        
    if status == "OPEN_CALL":
        # Guard against generic titles
        t_low = title.lower().strip()
        is_generic = False
        if t_low in GENERIC_PAGE_TITLES or t_low in {"funding", "grants", "programs", "getting started", "funding opportunities"}:
            is_generic = True
        elif re.search(r"^documents\s*\|\s*page\s*\d+", t_low):
            is_generic = True
            
        if is_generic and not deadline_dt and not explicit_status:
            return "UNKNOWN"
            
    return status


def extract_call_dates(content: str) -> tuple[str | None, str | None]:
    text = cleaned_text(content)
    month = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    date_token = rf"(?:{month}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,)?\s+20\d{{2}}|\d{{1,2}}(?:st|nd|rd|th)?\s+{month}\s+20\d{{2}}|20\d{{2}}[-/]\d{{1,2}}[-/]\d{{1,2}}|\d{{1,2}}[-/]\d{{1,2}}[-/]20\d{{2}})"
    patterns = {
        "opening": rf"(?:opens?|opening|applications?\s+(?:open|opens)|open(?:ing)?\s+date|start(?:s|ing)?|launch(?:es|ing)?)[^.!?]{{0,140}}?({date_token})",
        "deadline": rf"(?:closes?|closing|deadline|due\s+date|applications?\s+(?:close|closes)|last\s+date|submission\s+deadline|applications?\s+due|final\s+date)[^.!?]{{0,160}}?({date_token})",
    }
    values: dict[str, str | None] = {"opening": None, "deadline": None}
    for key, pattern in patterns.items():
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            parsed = _normalize_date(match.group(1))
            if parsed:
                values[key] = parsed
                break
    return values["opening"], values["deadline"]


def detect_research_intent(text: str, *, focus_area: str = "General", themes: list[str] | None = None) -> tuple[str, list[str]]:
    """Conservatively classify whether a funding call actually supports research.

    Navigation/footer mentions of ``research`` do not count. Evidence must occur in
    the same sentence/line as a research-funding action or an explicit research
    objective.
    """
    raw = str(text or "")
    chunks = [cleaned_text(x) for x in re.split(r"\n+|(?<=[.!?])\s+", raw) if cleaned_text(x)]
    patterns = (
        r"\b(?:research|r&d|research and development)\b.*\b(?:grant|funding|project|proposal|award|support|study|investigat|objective|question)\b",
        r"\b(?:grant|funding|support|award)\b.*\b(?:research|r&d|scientific|investigat|study)\b",
        r"\b(?:research project|scientific research|research study|research proposal|principal investigator|research objectives|research questions)\b",
        r"\b(?:hypothesis|methodology|experimental evaluation)\b",
    )
    evidence: list[str] = []
    for chunk in chunks:
        if len(chunk.split()) < 5:
            continue
        if any(re.search(pattern, chunk, flags=re.IGNORECASE) for pattern in patterns):
            evidence.append(chunk[:500])
    if evidence:
        return "research", list(dict.fromkeys(evidence))[:4]
    return "non_research", []

def generate_research_mission(themes: list[str], support: list[str], title: str, domain: str = "General", *, research_intent: str = "unknown", research_intent_evidence: list[str] | None = None) -> dict:
    if research_intent != "research":
        return {
            "funding_call_id": "", "domain": domain or "General", "technology_themes": list(themes),
            "funding_priorities": list(support), "target_outcomes": [], "investigation_questions": [],
            "inferred_challenges": [], "evidence_refs": [], "inference_provenance": [],
            "confidence": "Unknown", "research_intent": research_intent,
            "research_intent_evidence": list(research_intent_evidence or []),
        }
    themes_str = ", ".join(themes) if themes else "relevant technologies"
    mission = f"Investigate barriers affecting the transition of {themes_str} from research/proof-of-concept toward validation, deployment, adoption and commercialization."
    
    if any(t.lower() in ("cyber-physical systems", "cps", "digital healthcare") for t in themes):
        questions = [
            "What technical barriers limit prototype-to-deployment transition?",
            "What validation/benchmark gaps exist?",
            "What interoperability barriers exist?",
            "What regulatory/privacy/safety barriers exist?",
            "What deployment and clinical workflow barriers exist?",
            "What cybersecurity limitations exist?",
            "What productisation/scalability barriers exist?",
            "Which existing solutions already address these issues?"
        ]
    else:
        questions = [
            f"What technical barriers limit the deployment of {themes_str}?",
            f"What validation and evaluation gaps limit adoption of {themes_str}?",
            f"Which existing solutions already address these problems, and what gaps remain?"
        ]
        
    identity_material = "|".join([str(domain or "General").strip().lower(), str(title).strip().lower()])
    stable_id = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()[:10].upper()
    return {
        "funding_call_id": f"CALL-{stable_id}",
        "domain": domain or "General",
        "technology_themes": themes,
        "funding_priorities": support,
        # Outcomes are only retained when they are explicitly represented by the
        # observed support priorities. The mission itself is an inference and is
        # labelled as such; no generic outcome is presented as a fact.
        "target_outcomes": list(dict.fromkeys(support)),
        "investigation_questions": questions,
        "inferred_challenges": [mission] if mission else [],
        "evidence_refs": [],
        "inference_provenance": ["INFERENCE"] if mission else [],
        "confidence": "Low" if mission else "Unknown",
        "research_intent": research_intent,
        "research_intent_evidence": list(research_intent_evidence or []),
    }

def _extract_funding_body(text: str, fallback: str) -> str:
    patterns = [
        r"Greetings from\s+(.+?)(?:\.|\n)",
        r"an initiative of\s+(.+?)(?:\s+focused on|\.|\n)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            value = cleaned_text(m.group(1))
            if value:
                return value
    return fallback


def _extract_program_name(text: str, fallback: str) -> str:
    # Prefer explicit call names in source text before falling back to page title.
    patterns = [
        r"participate in\s+(.{3,140}?)(?:,\s*an initiative|,\s*a program)",
        r"About\s+([A-Z][A-Za-z0-9 .&()\-]{2,120})(?:\n|$)",
        r"selected RFP[:\s]+([A-Z][A-Za-z0-9 .&()\-]{2,120})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            value = cleaned_text(m.group(1)).rstrip(".")
            if value and len(value) < 140 and value.lower() not in GENERIC_PAGE_TITLES:
                return value

    title = cleaned_text(fallback)
    title = re.sub(r"^apply\s+for\s+", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*[|–-]\s*(?:Ethereum Foundation|ESP|Ecosystem Support Program).*$", "", title, flags=re.IGNORECASE)
    if title and title.lower().rstrip(" .:-") not in GENERIC_PAGE_TITLES and len(title) <= 180:
        return title.rstrip(" .:-")
    return title or fallback


def _extract_funding_amount(text: str) -> str | None:
    """Extract explicit funding amounts without inventing or hard-coding values."""
    unit = r"(?:K|M|B|lakh|crore|million|billion)"
    currency = r"(?:₹|\$|€|£|USD|EUR|GBP|INR)"
    number = r"\d[\d,]*(?:\.\d+)?"
    amount_re = re.compile(
        rf"(?:{currency}\s*{number}(?:\s*{unit})?(?:\s*[–-]\s*(?:{currency}\s*)?{number}(?:\s*{unit})?)?|{number}\s*{unit}(?:\s*[–-]\s*{number}\s*{unit})?)",
        re.IGNORECASE,
    )
    labelled: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        matches = list(amount_re.finditer(sentence))
        for match in matches:
            amount = cleaned_text(match.group(0))
            before = sentence[max(0, match.start() - 80):match.start()].lower()
            after = sentence[match.end():min(len(sentence), match.end() + 80)].lower()
            label_match = re.search(r"(?:for|to)\s+(projects?|startups?|companies?|research(?:ers| teams)?|applicants?)\b", after)
            if not label_match:
                label_match = re.search(r"(?:for|to)\s+(projects?|startups?|companies?|research(?:ers| teams)?|applicants?)\b", before)
            if label_match:
                label = label_match.group(1).capitalize()
                value = f"{label}: {amount}"
                if value not in labelled:
                    labelled.append(value)
    if labelled:
        return "; ".join(labelled)
    amounts: list[str] = []
    # Require a strict semantic relationship, not just proximity
    # Matches patterns like "grant of $X", "award up to $X", "$X budget"
    strict_prefix = r"(?:grant|award|funding|budget|prize|fund|request)s?(?:\s+(?:of|up to|for|is|amount|:|max(?:imum)?|=|-))?\s*$"
    strict_suffix = r"^\s*(?:grant|award|funding|budget|prize|fund)s?"
    
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        matches = list(amount_re.finditer(sentence))
        for match in matches:
            amount = cleaned_text(match.group(0))
            before = sentence[max(0, match.start() - 60):match.start()].lower()
            after = sentence[match.end():min(len(sentence), match.end() + 60)].lower()
            
            is_valid = False
            # Check immediate proximity relationship
            if re.search(strict_prefix, before) or re.search(strict_suffix, after):
                is_valid = True
            
            # Additional heuristic: if it's explicitly stated as "X per project" etc.
            if re.search(r"^\s*(?:per project|per startup|per applicant|maximum)", after):
                is_valid = True
                
            # Reject if it's a "funding round" which is VC, not a grant
            if "funding round" in after or "raised" in before:
                is_valid = False
                
            if is_valid and amount not in amounts:
                amounts.append(amount)
                
    return "; ".join(amounts[:8]) or None


def _extract_duration(text: str) -> str | None:
    m = re.search(r"\b(\d+)\s*[–-]\s*(\d+)\s*months\b", text, flags=re.IGNORECASE)
    return f"{m.group(1)}–{m.group(2)} months" if m else None


def _extract_application_url(text: str) -> str | None:
    """Return a URL only when nearby source text identifies it as an application endpoint."""
    url_pattern = r"https?://[^\s)\]]+"
    contextual = re.compile(r"(?:application|apply|submit|submission|proposal)\s*(?:link|url|portal|here)?\s*[:\-]?\s*(" + url_pattern + r")", re.IGNORECASE)
    for match in contextual.finditer(text):
        return match.group(1).rstrip(".,")
    return None



def _evidence_excerpt(text: str, patterns: list[str], window: int = 240) -> str | None:
    chunks = [cleaned_text(c) for c in re.split(r"(?<=[.!?])\s+|\n+", text) if cleaned_text(c)]
    for pattern in patterns:
        for chunk in chunks:
            if re.search(pattern, chunk, flags=re.IGNORECASE):
                return chunk[:window]
    return None

def _field_evidence_is_specific(field: str, value: str | None, evidence: str | None) -> bool:
    """Require field evidence to contain the extracted value or a field-specific cue."""
    if not value or not evidence:
        return False
    v = cleaned_text(str(value)).lower()
    e = cleaned_text(str(evidence)).lower()
    # Compare normalized fragments rather than requiring punctuation/formatting equality.
    compact_v = re.sub(r"[^a-z0-9]+", " ", v).strip()
    compact_e = re.sub(r"[^a-z0-9]+", " ", e).strip()
    if compact_v and compact_v in compact_e:
        return True
    field_cues = {
        "deadline": ("deadline", "close", "closes", "closing", "applications close", "submit by"),
        "opening_date": ("open", "opens", "opening", "applications open"),
        "funding_amount": ("funding", "support", "grant", "award", "budget"),
        "eligibility": ("eligib", "who can apply", "applicant", "student group", "faculty"),
        "geography": ("india", "country", "countries", "region", "geograph"),
        "trl": ("trl", "technology readiness", "maturity"),
        "required_partners": ("partner", "partnership"),
        "deliverables": ("deliverable", "outcomes", "report", "required outputs"),
        "evaluation_criteria": ("evaluation", "selection", "criteria", "review"),
        "eligible_costs": ("eligible costs", "allowable costs", "expenses", "budget"),
        "application_url": ("application", "apply", "submit", "submission"),
    }
    return any(cue in e for cue in field_cues.get(field, ()))

def _application_evidence(text: str) -> str | None:
    for chunk in [cleaned_text(c) for c in re.split(r"(?<=[.!?])\s+|\n+", text) if cleaned_text(c)]:
        if not re.search(r"https?://[^\s)\]]+", chunk):
            continue
        if any(token in chunk.lower() for token in ("application", "apply", "submit", "submission", "proposal")):
            return chunk[:320]
    return None


def _extract_section_value(text: str, labels: tuple[str, ...], *, max_chars: int = 900) -> str | None:
    """Extract a bounded value from a labelled funding section without inventing text."""
    chunks = [cleaned_text(c) for c in re.split(r"\n+|(?<=[.!?])\s+", text) if cleaned_text(c)]
    label_re = re.compile(r"(?:" + "|".join(re.escape(x) for x in labels) + r")\s*[:\-]?\s*(.+)$", re.I)
    hits: list[str] = []
    for chunk in chunks:
        m = label_re.search(chunk)
        if m:
            value = cleaned_text(m.group(1))
            if value and len(value) > 3:
                hits.append(value[:max_chars])
    if hits:
        return " ".join(dict.fromkeys(hits))[:max_chars]
    return None


def analyze_document(document: FundingDocument) -> FundingOpportunity:
    lowered = document.content.lower()
    sections = classify_sections(document.content)
    domain_relevance = build_domain_relevance(document.content)
    # Never route a funding call into a canonical research area because of a few
    # incidental words on a broad landing page. Routing requires both a meaningful
    # normalized share and enough raw domain evidence; otherwise the call remains
    # General/Unscoped and the adaptive research agent investigates it.
    top_item = domain_relevance[0] if domain_relevance else {}
    top_score = float(top_item.get("score", 0.0) or 0.0)
    top_raw = float(top_item.get("raw_score", 0.0) or 0.0)
    top_matches = list(top_item.get("matched_terms", []) or [])
    focus_area = (str(top_item.get("domain")) if top_score >= 0.30 and top_raw >= 4.0 and len(top_matches) >= 2 else "General")
    top_domain = focus_area
    domain_themes = {
        "RWA": ["Real-World Assets", "Asset Tokenization"],
        "ESG": ["ESG", "Carbon and MRV"],
        "ZK-IoV": ["Zero-Knowledge", "Internet of Vehicles"],
        "DID": ["Decentralized Identity", "Verifiable Credentials"],
        "DePIN": ["Decentralized Physical Infrastructure", "IoT"],
        "MEV": ["MEV", "Transaction Ordering"],
        "Stablecoins": ["Stablecoins", "Payment and Settlement"],
        "DigitalHealthCPS": ["Cyber-Physical Systems", "Digital Healthcare"],
    }
    themes = list(domain_themes.get(focus_area, []))
    if top_domain == "DigitalHealthCPS":
        if "cyber-physical systems" not in lowered and "cps" not in lowered and "Cyber-Physical Systems" in themes:
            themes.remove("Cyber-Physical Systems")
        if "digital healthcare" not in lowered and "Digital Healthcare" in themes:
            themes.remove("Digital Healthcare")
        
    priorities = []
    if "prototype refinement" in lowered: priorities.append("Prototype refinement")
    if "validation" in lowered: priorities.append("Testing and validation")
    if "deployment" in lowered: priorities.append("Deployment")
    if "technology maturation" in lowered: priorities.append("Technology maturation")
    if "commercialisation" in lowered: priorities.append("Commercialisation")
    if "market adoption" in lowered: priorities.append("Market adoption")
        
    opening_date, deadline = extract_call_dates(document.content)
    research_intent, research_intent_evidence = detect_research_intent(document.content, focus_area=focus_area, themes=themes)
    mission_ctx = generate_research_mission(themes, priorities, document.title, focus_area, research_intent=research_intent, research_intent_evidence=research_intent_evidence)
    identity_material = "|".join([document.organization.strip().lower(), document.title.strip().lower(), canonicalize_url(document.source)])
    mission_ctx["funding_call_id"] = "CALL-" + hashlib.sha256(identity_material.encode("utf-8")).hexdigest()[:12].upper()
    # Select the primary domain plus genuinely supported secondary domains.
    # Weak incidental keyword hits should not multiply downstream collection.
    selected_domains = []
    if focus_area != "General":
        selected_domains = [
            item["domain"] for item in domain_relevance
            if float(item.get("score", 0.0) or 0.0) >= 0.10
        ][:3]
    mission_ctx["domain_relevance"] = tuple(domain_relevance)
    mission_ctx["selected_domains"] = tuple(selected_domains)

    program_name = _extract_program_name(document.content, document.title)
    status = determine_status(None, opening_date, deadline, document.content, program_name)
    opp_type = "FUNDED_PROJECT" if "awarded" in lowered or "selected projects" in lowered else "FUNDING_CALL"

    amount = _extract_funding_amount(document.content)
    application_url = _extract_application_url(document.content)
    funding_body = _extract_funding_body(document.content, document.organization)
    
    # A future deadline plus a contextual application endpoint is strong current
    # state evidence even when the publisher omits the literal phrase
    # "applications are open". This is still guarded by an identifiable call
    # title and an unexpired deadline, so generic program landing pages remain
    # UNKNOWN.
    if status == "UNKNOWN" and deadline and application_url:
        deadline_dt = _parse_date(deadline, end_of_day=True)
        generic_title = program_name.lower().rstrip(" .:-") in GENERIC_PAGE_TITLES or program_name.lower() in {"funding", "grants", "funding at nsf", "programs"}
        if deadline_dt and deadline_dt >= datetime.now(UTC) and not generic_title:
            status = "OPEN_CALL"
    duration = _extract_duration(document.content)

    eligibility = _extract_section_value(document.content, ("eligibility", "who can apply", "eligible applicants", "eligible organizations"))
    geography = _extract_section_value(document.content, ("geographic eligibility", "geography", "eligible countries", "eligible regions"))
    trl = _extract_section_value(document.content, ("technology readiness level", "trl", "maturity level"))
    required_partners = _extract_section_value(document.content, ("required partners", "partnerships required", "partner requirements"))
    deliverables = _extract_section_value(document.content, ("deliverables", "expected deliverables", "required outputs"))
    evaluation_criteria = _extract_section_value(document.content, ("evaluation criteria", "selection criteria", "review criteria"))
    eligible_costs = _extract_section_value(document.content, ("eligible costs", "allowable costs", "eligible expenses"))

    evidence = {
        "status": _evidence_excerpt(document.content, [r"applications? (?:are|now) open", r"open for applications", r"request for proposals", r"call for proposals"]),
        "opening_date": _evidence_excerpt(document.content, [r"opens?[^.\n]{0,120}20\d{2}", r"applications? open[^.\n]{0,120}20\d{2}"]),
        "deadline": _evidence_excerpt(document.content, [r"closes?[^.\n]{0,160}20\d{2}", r"deadline[^.\n]{0,160}20\d{2}", r"applications? close[^.\n]{0,160}20\d{2}"]),
        "funding_amount": _evidence_excerpt(document.content, [r"(?:funding|financial support|grant|award)[^.\n]{0,240}(?:₹|\$|€|£|USD|EUR|GBP|INR)\s*\d"]),
        "eligibility": _evidence_excerpt(document.content, [r"(?:eligibility|who can apply|eligible applicants?|eligible organizations?)[^.:\n]{0,10}[:\-]?[^.\n]{0,700}" ]),
        "geography": _evidence_excerpt(document.content, [r"(?:geographic eligibility|eligible countries|eligible regions|geography)[^.:\n]{0,10}[:\-]?[^.\n]{0,500}" ]),
        "trl": _evidence_excerpt(document.content, [r"(?:technology readiness level|TRL|maturity level)[^.:\n]{0,10}[:\-]?[^.\n]{0,300}" ]),
        "required_partners": _evidence_excerpt(document.content, [r"(?:required partners?|partnerships required|partner requirements?)[^.:\n]{0,10}[:\-]?[^.\n]{0,500}" ]),
        "deliverables": _evidence_excerpt(document.content, [r"(?:deliverables?|expected deliverables?|required outputs?)[^.:\n]{0,10}[:\-]?[^.\n]{0,700}" ]),
        "evaluation_criteria": _evidence_excerpt(document.content, [r"(?:evaluation criteria|selection criteria|review criteria)[^.:\n]{0,10}[:\-]?[^.\n]{0,700}" ]),
        "eligible_costs": _evidence_excerpt(document.content, [r"(?:eligible costs?|allowable costs?|eligible expenses?)[^.:\n]{0,10}[:\-]?[^.\n]{0,700}" ]),
        "application_url": _application_evidence(document.content),
    }
    raw_evidence = {k: v for k, v in evidence.items() if v}
    extracted_values = {
        "opening_date": opening_date, "deadline": deadline, "funding_amount": amount,
        "eligibility": eligibility, "geography": geography, "trl": trl,
        "required_partners": required_partners, "deliverables": deliverables,
        "evaluation_criteria": evaluation_criteria, "eligible_costs": eligible_costs,
        "application_url": application_url,
    }
    evidence = {}
    for key, ev in raw_evidence.items():
        if key == "status":
            evidence[key] = ev
        elif _field_evidence_is_specific(key, extracted_values.get(key), ev):
            evidence[key] = ev
        elif key == "application_url" and application_url and application_url in str(ev):
            evidence[key] = ev

    return FundingOpportunity(
        funding_body=funding_body,
        program_name=program_name,
        call_id=mission_ctx.get("funding_call_id"),
        status=status,
        opening_date=opening_date,
        deadline=deadline,
        funding_amount=amount,
        currency=None,  # Do not hardcode — extract from amount string if present
        duration=duration,
        eligibility=eligibility,
        geography=geography,
        trl=trl,
        research_priorities=priorities,
        required_partners=required_partners,
        deliverables=deliverables,
        evaluation_criteria=evaluation_criteria,
        eligible_costs=eligible_costs,
        application_url=application_url,
        opportunity_type=opp_type,
        focus_area=focus_area,
        keywords=themes,
        research_context=mission_ctx,
        source_method="deterministic_extraction",
        source_url=document.source,
        source_title=document.title,
        retrieved_at=datetime.now(UTC).isoformat(),
        published_at=None,
        last_verified_at=datetime.now(UTC).isoformat(),
        evidence=evidence,
    )

def build_domain_relevance(text: str) -> list[dict]:
    """Return the canonical 8-domain relevance ranking for a funding call.

    Funding relevance is call-level evidence: unlike downstream routing, it is allowed
    to rank multiple domains.  The same weighted scorer is used throughout RIF so
    funding, normalization, UI, and terminal reporting cannot disagree.
    """
    from core.schemas import rank_research_areas

    return rank_research_areas(text)


def build_tags(analysis: FundingOpportunity) -> list[str]:
    tags = ["#Funding"]
    if analysis.focus_area != "DigitalHealthCPS" and analysis.focus_area in {"RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins"}:
        tags.append("#Blockchain")
    focus_tag = normalize_tag(analysis.focus_area)
    if focus_tag and focus_tag not in tags:
        tags.append(focus_tag)
    for keyword in analysis.keywords:
        tag = normalize_tag(keyword)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def build_markdown(document: FundingDocument, analysis: FundingOpportunity, tags: list[str]) -> str:
    source_value = document.source if document.source_type != "Manual" or document.source.startswith("http") else "Manual"
    keyword_tags = " ".join(normalize_tag(keyword) for keyword in analysis.keywords if normalize_tag(keyword)) or "#funding"
    research_context = analysis.research_context or {}
    inferred_challenges = research_context.get("inferred_challenges") or []
    research_mission = str(inferred_challenges[0]).strip() if inferred_challenges else "Unknown"
    investigation_questions = research_context.get("investigation_questions") or []
    domain_relevance = build_domain_relevance(document.content)

    return textwrap.dedent(
        f'''\
        # Grant: {analysis.program_name}

        ## Status
        {analysis.status}
        
        ## Funding Body
        {analysis.funding_body}
        
        ## Deadlines
        Opening Date: {analysis.opening_date or "Unknown"}
        Deadline: {analysis.deadline or "Unknown"}

        ## Funding Details
        Amount: {analysis.funding_amount or "Unknown"} {analysis.currency or ""}
        Duration: {analysis.duration or "Unknown"}
        Eligible Costs: {analysis.eligible_costs or "Unknown"}

        ## Requirements
        Eligibility: {analysis.eligibility or "Unknown"}
        Geography: {analysis.geography or "Unknown"}
        TRL: {analysis.trl or "Unknown"}
        Required Partners: {analysis.required_partners or "Unknown"}

        ## Evaluation & Deliverables
        Evaluation Criteria: {analysis.evaluation_criteria or "Unknown"}
        Deliverables: {analysis.deliverables or "Unknown"}
        
        ## Problem Preview
        {document.content.strip()}

        ## Research Mission
        {research_mission}
        
        ## Investigation Questions
        {chr(10).join(f"- {q}" for q in investigation_questions) if investigation_questions else "None"}

        ## Focus Area
        {analysis.focus_area}

        ## Domain Relevance
        {chr(10).join(f"- {item['rank']}. {item['domain']}: {item['score']:.2f} ({', '.join(item['matched_terms']) or 'no direct evidence'})" for item in domain_relevance)}

        ## Keywords
        {keyword_tags}

        ## Source Details
        Source: {source_value}
        Application URL: {analysis.application_url or "Unknown"}
        Source Type: {document.source_type}
        Retrieved At: {analysis.retrieved_at}

        ## Layer
        Funding

        ## Shared Metadata
        Agent: funding
        Research Area: {analysis.focus_area}
        Source: {source_value}
        Program: {analysis.program_name}

        ## Agent-Specific Body
        Funding Body: {analysis.funding_body}
        Opportunity Type: {analysis.opportunity_type}
        Funding Amount: {analysis.funding_amount or "Unknown"}
        Evaluation Criteria: {analysis.evaluation_criteria or "Unknown"}

        ## Raw Content
        {document.content.strip()}

        ## Tags
        {' '.join(tags)}
        '''
    ).strip() + "\n"


def save_markdown_output(document: FundingDocument, analysis: FundingOpportunity, output_dir: Path) -> Path:
    org_dir = output_dir / slugify(document.organization)
    org_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{slugify(analysis.program_name)}.md"
    output_path = org_dir / filename
    tags = build_tags(analysis)
    output_path.write_text(build_markdown(document, analysis, tags), encoding="utf-8")
    LOGGER.info("Saved funding markdown output to %s", output_path)
    return output_path


def fetch_all_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT organization, title, year, problem, keywords, url, focus_area, requirements, problem_signature, source_type
        FROM funding_data
        ORDER BY year, organization, title
        """
    ).fetchall()


def write_insights(connection: sqlite3.Connection, output_dir: Path) -> None:
    rows = fetch_all_rows(connection)
    if not rows:
        return

    problem_counter = Counter(row["problem_signature"] for row in rows if row["problem_signature"])
    keyword_counter: Counter[str] = Counter()
    problems_by_year: dict[int, list[str]] = defaultdict(list)
    topics_by_year: dict[int, Counter[str]] = defaultdict(Counter)

    for row in rows:
        year = int(row["year"])
        problems_by_year[year].append(row["problem_signature"])
        keywords = json.loads(row["keywords"] or "[]")
        keyword_counter.update(keywords)
        for keyword in keywords:
            topics_by_year[year][keyword] += 1

    sorted_years = sorted(problems_by_year)
    if not sorted_years:
        return
    latest_year = sorted_years[-1]
    prior_years = sorted_years[:-1]
    emerging: list[tuple[int, str]] = []
    if prior_years:
        prior_avg: dict[str, float] = defaultdict(float)
        for year in prior_years:
            for keyword, count in topics_by_year[year].items():
                prior_avg[keyword] += count
        for keyword in list(prior_avg):
            prior_avg[keyword] /= len(prior_years)
        for keyword, latest_count in topics_by_year[latest_year].items():
            baseline = prior_avg.get(keyword, 0.0)
            delta = latest_count - baseline
            if delta > 0:
                emerging.append((int(delta * 100), keyword))
        emerging.sort(reverse=True)

    lines = ["# Funding Insights", "", "## Top Problems"]
    for problem, count in problem_counter.most_common(5):
        lines.append(f"- {problem}: {count}")

    lines.extend(["", "## Keyword Frequency"])
    for keyword, count in keyword_counter.most_common(10):
        lines.append(f"- {keyword}: {count}")

    lines.extend(["", "## Trends Over Time"])
    for year in sorted_years:
        year_counter = Counter(problems_by_year[year])
        problems_text = ", ".join(f"{problem} ({count})" for problem, count in year_counter.most_common(5))
        lines.append(f"- {year}: {problems_text}")

    lines.extend(["", "## Emerging Areas"])
    if emerging:
        for _, keyword in emerging[:5]:
            lines.append(f"- {keyword}")
    else:
        lines.append("- No clear emerging areas yet.")

    output_dir.mkdir(parents=True, exist_ok=True)
    insights_path = output_dir / "insights.md"
    insights_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("Saved funding insights to %s", insights_path)


def process_documents(documents_with_analysis: list[tuple[FundingDocument, FundingOpportunity]], output_dir: Path, db_path: Path) -> int:
    connection = db_connect(db_path)
    init_funding_db(connection)
    created = 0
    for document, analysis in documents_with_analysis:
        if funding_exists(connection, document.title, document.source):
            LOGGER.info("Skipping already stored funding item '%s'", document.title)
            continue
        output_path = save_markdown_output(document, analysis, output_dir)
        save_funding_record(
            connection,
            opportunity=analysis,
            path=str(output_path),
            source_type=document.source_type,
            year=document.year
        )
        created += 1
    connection.commit()
    write_insights(connection, output_dir)
    return created


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and structure funding opportunities for RIF.")
    parser.add_argument("--mode", choices=["auto", "url", "text"], required=True, help="Input mode")
    parser.add_argument("--url", help="Manual URL input")
    parser.add_argument("--text", help="Manual raw text input")
    parser.add_argument("--organization", help="Organization name for manual modes")
    parser.add_argument("--title", help="Optional title for manual text mode")
    parser.add_argument("--year", type=int, help="Optional year for manual text mode")
    parser.add_argument("--sources-path", default=str(DEFAULT_SOURCES_PATH), help="Path to sources/funding_sources.yaml")
    parser.add_argument("--posts-per-source", type=int, default=DEFAULT_POSTS_PER_SOURCE, help="Maximum posts to inspect per source")
    parser.add_argument("--area", help="Optional research area filter for auto mode")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for funding markdown outputs")
    parser.add_argument("--test", action="store_true", help="Run in fast deterministic test mode with artificial limits (Development only).")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path for funding memory")
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    configure_logging(args.log_level)

    try:
        if args.mode == "auto":
            response = run_agent("configured_scan", args.area, {
                "sources_path": args.sources_path,
                "posts_per_source": args.posts_per_source,
                "output_dir": args.output_dir,
                "db_path": args.db_path,
                "analysis_mode": "analyze",
                "is_test_mode": args.test
            })
            if "collection_results" in response:
                print("\nCollection Metrics:")
                header = (
                    f"{'Source':<35} | {'Method':<6} | {'Pages':<5} | {'Detail':<6} | "
                    f"{'Docs':<4} | {'Disc':<5} | {'Valid':<5} | {'Fail':<4} | "
                    f"{'PagComplete':<11} | {'Trunc':<5} | {'Status':<25}"
                )
                print(header)
                print("-" * len(header))
                for res in response["collection_results"]:
                    pag_str = str(res['pagination_complete']) if res.get('pagination_detected', True) else "N/A"
                    print(
                        f"{res['source_id'][:35]:<35} | {res['method']:<6} | "
                        f"{res['pages_fetched']:<5} | {res['detail_pages_fetched']:<6} | "
                        f"{res['documents_fetched']:<4} | {res['records_discovered']:<5} | "
                        f"{res['records_valid']:<5} | {res['records_failed']:<4} | "
                        f"{pag_str:<11} | {str(res['truncated']):<5} | "
                        f"{res['final_status']:<25}"
                    )
                    
        elif args.mode == "url":
            if not args.url:
                parser.error("--url is required for mode=url")
            run_agent("manual_url", None, {
                "url": args.url,
                "organization": args.organization,
                "output_dir": args.output_dir,
                "db_path": args.db_path,
                "analysis_mode": "analyze"
            })
        else:
            if not args.text or not args.organization:
                parser.error("--text and --organization are required for mode=text")
            run_agent("manual_text", None, {
                "text": args.text,
                "organization": args.organization,
                "title": args.title,
                "year": args.year,
                "output_dir": args.output_dir,
                "db_path": args.db_path,
                "analysis_mode": "analyze",
                "is_test_mode": args.test
            })

    except Exception as exc:
        LOGGER.error("Funding agent failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


def run_agent(mode: str, area: str | None = None, input_data: dict | None = None, funding_context: FundingCallContext | None = None, funding_contexts: list[FundingCallContext] | None = None) -> dict:
    from datetime import UTC, datetime, date
    from core.agent_interface import (
        default_intermediate_output_dir,
        infer_name_from_url,
        snapshot_markdown_files,
        validate_run_input,
    )
    from core.schemas import create_error_response
    
    agent_name = "funding"
    layer = "Funding"
    payload = input_data or {}
    analysis_mode = str(payload.get("analysis_mode", "collect_only")).strip().lower()
    started_at = datetime.now(UTC)
    configure_logging(payload.get("log_level", "INFO"))
    
    validation_errors = validate_run_input(mode, payload)
    if validation_errors:
        return create_error_response(
            agent=agent_name,
            layer=layer,
            mode=mode,
            area=area,
            errors=validation_errors,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        
    output_dir = Path(payload.get("output_dir", default_intermediate_output_dir(agent_name) if analysis_mode != "analyze" else DEFAULT_OUTPUT_DIR))
    db_path = Path(payload.get("db_path", DEFAULT_DB_PATH))
    before_snapshot = snapshot_markdown_files(output_dir)
    documents = []
    extra_warnings: list[str] = []
    
    LOGGER.info("Starting funding agent run: mode=%s area=%s", mode, area)
    
    try:
        if mode == "configured_scan":
            sources_path = Path(payload.get("sources_path", DEFAULT_SOURCES_PATH))
            
            # Honor force_refresh from orchestration payload
            from core.source_registry import current_force_source_refresh
            force = bool(payload.get("force_refresh", False))
            token = current_force_source_refresh.set(force)
            try:
                from agents.funding_collector import collect_auto_sources
                sources = load_sources(sources_path)
                collection_iterator = collect_auto_sources(
                    sources, 
                    area, 
                    warnings=extra_warnings, 
                    is_test_mode=payload.get("is_test_mode", False)
                )
                
                collection_results = []
                documents = []
                
                import pickle
                checkpoint_path = Path("scratch/funding_checkpoint.pkl")
                if checkpoint_path.exists():
                    try:
                        ckpt = pickle.loads(checkpoint_path.read_bytes())
                        completed_sources = ckpt.get("completed_sources", set())
                        collection_results = ckpt.get("collection_results", [])
                        documents = ckpt.get("documents", [])
                        LOGGER.info(f"Loaded checkpoint with {len(completed_sources)} completed sources and {len(documents)} documents.")
                        
                        # Filter sources
                        sources = [s for s in sources if s.name not in completed_sources]
                        
                        # Recreate iterator with remaining sources
                        collection_iterator = collect_auto_sources(
                            sources, 
                            area, 
                            warnings=extra_warnings, 
                            is_test_mode=payload.get("is_test_mode", False)
                        )
                    except Exception as e:
                        LOGGER.warning(f"Failed to load checkpoint: {e}")
                        completed_sources = set()
                else:
                    completed_sources = set()

                # Process and save progressively to avoid data loss on crash
                for res in collection_iterator:
                    collection_results.append(res)
                    source_docs = []
                    for doc, analysis in res.opportunities:
                        documents.append((doc, analysis))
                        source_docs.append((doc, analysis))
                    if analysis_mode == "analyze" and source_docs:
                        process_documents(source_docs, output_dir, db_path)
                        
                    # Save checkpoint after each source
                    completed_sources.add(res.source_id)
                    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                    checkpoint_path.write_bytes(pickle.dumps({
                        "completed_sources": completed_sources,
                        "collection_results": collection_results,
                        "documents": documents
                    }))
            finally:
                current_force_source_refresh.reset(token)
                
            if not collection_results:
                if area:
                    extra_warnings.append(f"No funding posts matched the configured sources for area '{area}'.")
                else:
                    extra_warnings.append("No funding posts matched the configured sources.")
        elif mode == "manual_url":
            url = payload["url"]
            organization = payload.get("organization") or infer_name_from_url(url, "Manual Organization")
            doc = build_manual_url_document(url, organization)
            analysis = analyze_document(doc)
            documents = [(doc, analysis)]
            if analysis_mode == "analyze":
                process_documents([(doc, analysis)], output_dir, db_path)
        else:
            text = payload["text"]
            organization = payload.get("organization") or "Manual Organization"
            doc = build_manual_text_document(text, organization, payload.get("title"), payload.get("year"))
            analysis = analyze_document(doc)
            documents = [(doc, analysis)]
            if analysis_mode == "analyze":
                process_documents([(doc, analysis)], output_dir, db_path)
            
        outputs = []
        docs_only = []
        for doc, analysis in documents:
            docs_only.append(doc)
            output_path = None
            try:
                output_path = save_markdown_output(doc, analysis, output_dir)
            except Exception as e:
                LOGGER.warning("Could not save funding markdown output: %s", e)

            evidence = {"source_url": analysis.source_url, "source_title": analysis.source_title, **analysis.evidence}
            domains = build_domain_relevance(doc.content)
            outputs.append({
                "funding_body": analysis.funding_body,
                "program_name": analysis.program_name,
                "call_id": analysis.call_id,
                "title": analysis.program_name,
                "status": analysis.status,
                "is_open_call": analysis.status == "OPEN_CALL",
                "opening_date": analysis.opening_date,
                "deadline": analysis.deadline,
                "funding_amount": analysis.funding_amount,
                "currency": analysis.currency,
                "duration": analysis.duration,
                "eligibility": analysis.eligibility,
                "geography": analysis.geography,
                "trl": analysis.trl,
                "eligible_costs": analysis.eligible_costs,
                "application_url": analysis.application_url,
                "opportunity_type": analysis.opportunity_type,
                "focus_area": analysis.focus_area,
                "research_area": analysis.focus_area,
                "research_priorities": analysis.research_priorities,
                "domain_relevance": domains,
                "primary_domain": analysis.focus_area if analysis.focus_area in FOCUS_AREAS else None,
                "secondary_domains": [item["domain"] for item in domains[1:3] if float(item.get("score", 0.0) or 0.0) > 0.0] if len(domains) > 1 else [],
                "required_partners": analysis.required_partners,
                "deliverables": analysis.deliverables,
                "evaluation_criteria": analysis.evaluation_criteria,
                "research_context": analysis.research_context,
                "source_url": analysis.source_url,
                "source_title": analysis.source_title,
                "published_at": analysis.published_at,
                "created_at": analysis.retrieved_at,
                "problem": doc.content.strip(),
                "problem_signal": doc.content.strip(),
                "markdown_path": str(output_path.resolve()) if output_path else None,
                "evidence": {k: v for k, v in evidence.items() if v},
                "provenance_type": "observed_source_with_deterministic_extraction",
            })
                
        response = {
            "agent": agent_name,
            "layer": layer,
            "mode": mode,
            "area": area,
            "status": "success" if documents else "partial_success",
            "outputs": outputs,
            "errors": [],
            "warnings": extra_warnings,
            "items_processed": len(documents),
            "items_saved": len(documents),
            "metadata": {
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
                "duration_seconds": (datetime.now(UTC) - started_at).total_seconds()
            }
        }
        if mode == "configured_scan":
            response["collection_results"] = [
                {
                    "source_id": res.source_id,
                    "method": res.method,
                    "pages_discovered": res.pages_discovered,
                    "pages_fetched": res.pages_fetched,
                    "pages_failed": res.pages_failed,
                    "detail_pages_discovered": res.detail_pages_discovered,
                    "detail_pages_fetched": res.detail_pages_fetched,
                    "detail_pages_failed": res.detail_pages_failed,
                    "documents_discovered": res.documents_discovered,
                    "documents_fetched": res.documents_fetched,
                    "documents_failed": res.documents_failed,
                    "records_discovered": res.records_discovered,
                    "records_valid": res.records_valid,
                    "records_failed": res.records_failed,
                    "duplicates_removed": res.duplicates_removed,
                    "pagination_detected": res.pagination_detected,
                    "pagination_complete": res.pagination_complete,
                    "discovery_exhausted": res.discovery_exhausted,
                    "truncated": res.truncated,
                    "max_depth": res.max_depth,
                    "max_depth_reached": res.max_depth_reached,
                    "errors": res.errors,
                    "final_status": res.final_status,
                }
                for res in collection_results
            ]
        LOGGER.info("Finished funding agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
        return response
        
    except FundingAgentError as exc:
        LOGGER.exception("Funding agent failed during run_agent")
        return create_error_response(
            agent=agent_name,
            layer=layer,
            mode=mode,
            area=area,
            errors=[str(exc)],
            items_processed=len(documents),
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

if __name__ == "__main__":
    raise SystemExit(main())
