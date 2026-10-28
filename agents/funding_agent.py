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
from core.schemas import ALLOWED_RESEARCH_AREAS, normalize_area, resolve_research_area
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
    "DID": ["decentralized identity", "decentralised identity", "self-sovereign identity", "decentralized identifier", "verifiable credential", "identity wallet"],
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
FOCUS_AREAS = ["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins"]

GENERIC_PAGE_TITLES = {
    # These are navigation, directory, or browser-chrome labels, not a unique
    # funding opportunity. Keep them centralized so extraction and status
    # classification apply the same conservative rule.
    "how to apply", "apply", "home", "overview", "open rounds", "open calls",
    "request for proposals", "requests for proposals", "rpf", "rfp", "funding",
    "funding opportunities", "grants", "programs", "our programs", "topic",
    "topics", "calls for proposals", "program directory", "grant directory",
    "grants and funding", "development grants", "this listing", "yourself",
    "simpler.grants.gov", "grants.gov",
    "open call", "call documents", "programme theme", "program theme",
    "grant details", "funding allocation", "funding and prizes", "details",
    "scope", "eligibility", "who can apply", "timeline", "introduction", "background",
}

def _is_generic_funding_title(value: str | None) -> bool:
    """Recognize generic browser titles even when the site appends its brand."""
    normalized = re.sub(r"\s+", " ", str(value or "").casefold()).strip()
    # Check browser-generated pagination labels before stripping the suffix
    # after a pipe; otherwise "Documents | Page 10" becomes "documents" and
    # escapes this guard.
    if re.fullmatch(r"documents\s*\|\s*page\s*\d+", normalized):
        return True
    if re.fullmatch(r"(?:technologies|solutions)\s+to\s+be\s+improved\s+for\s+projects", normalized):
        return True
    normalized = re.split(r"\s+[|·—–]\s+|\s+-\s+", normalized, maxsplit=1)[0].strip(" .:-")
    return normalized in GENERIC_PAGE_TITLES or normalized in {"getting started", "funding at nsf"}


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
    generic_candidate: str | None = None
    for candidate in candidates:
        normalized = re.sub(r"\s+", " ", candidate).strip()
        if not normalized:
            continue
        if _is_generic_funding_title(normalized):
            generic_candidate = generic_candidate or normalized
            continue
        if len(normalized) > 180:
            normalized = normalized[:180].rsplit(" ", 1)[0]
        return normalized
    # Preserve the generic label instead of falling back to the URL itself;
    # downstream status/selector gates can then reject the directory page.
    return generic_candidate or fallback

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


def _normalize_collection_coverage_status(result: Any) -> Any:
    """Prevent source-level negative/verified claims when known coverage gaps remain."""
    gaps = getattr(result, "coverage_gaps", None) or []
    relevant_skipped = int(getattr(result, "relevant_urls_skipped", 0) or 0)
    truncated = bool(getattr(result, "truncated", False))
    pagination_detected = bool(getattr(result, "pagination_detected", False))
    pagination_complete = bool(getattr(result, "pagination_complete", False))
    discovery_exhausted = bool(getattr(result, "discovery_exhausted", False))
    status = str(getattr(result, "final_status", "") or "").upper()

    incomplete = bool(
        gaps
        or relevant_skipped
        or truncated
        or not discovery_exhausted
        or (pagination_detected and not pagination_complete)
    )
    if status in {"NO_ACTIVE_OPPORTUNITIES", "VERIFIED"} and incomplete:
        result.final_status = "PARTIAL"

    if gaps or relevant_skipped:
        result.discovery_exhausted = False
        result.pagination_complete = False
    return result


def _update_source_verification_flags(result: Any) -> Any:
    """Set source-level verification flags only when the corresponding checks ran.

    Evidence verification means every emitted record has a source URL, source
    content, and at least one extracted source-grounded evidence field. Currentness
    verification additionally requires complete source acquisition and explicit
    status/date evidence for every emitted record. A confirmed, fully exhausted
    empty-state page can verify that no active opportunities were found.
    """
    opportunities = list(getattr(result, "opportunities", None) or [])
    errors = list(getattr(result, "errors", None) or [])
    gaps = list(getattr(result, "coverage_gaps", None) or [])
    has_acquired_evidence = any(
        int(getattr(result, field, 0) or 0) > 0
        for field in ("pages_fetched", "detail_pages_fetched", "documents_fetched", "api_pages_fetched", "records_discovered")
    )
    complete = (
        has_acquired_evidence
        and str(getattr(result, "final_status", "") or "").upper()
        in {"VERIFIED", "NO_ACTIVE_OPPORTUNITIES"}
        and bool(getattr(result, "discovery_exhausted", False))
        and not bool(getattr(result, "truncated", False))
        and not gaps
        and int(getattr(result, "relevant_urls_skipped", 0) or 0) == 0
        and int(getattr(result, "pages_failed", 0) or 0) == 0
        and int(getattr(result, "detail_pages_failed", 0) or 0) == 0
        and int(getattr(result, "documents_failed", 0) or 0) == 0
        and int(getattr(result, "records_failed", 0) or 0) == 0
        and not errors
        and (
            not bool(getattr(result, "pagination_detected", False))
            or bool(getattr(result, "pagination_complete", False))
        )
    )

    if not opportunities:
        confirmed_empty = (
            complete
            and bool(getattr(result, "no_active_opportunities_confirmed", False))
            and str(getattr(result, "final_status", "") or "").upper() == "NO_ACTIVE_OPPORTUNITIES"
        )
        result.evidence_verified = confirmed_empty
        result.currentness_verified = confirmed_empty
        return result

    record_evidence_verified = True
    record_currentness_verified = True
    for item in opportunities:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            record_evidence_verified = False
            record_currentness_verified = False
            continue
        document, analysis = item[0], item[1]
        source_url = str(getattr(document, "source", "") or getattr(analysis, "source_url", "") or "")
        source_content = str(getattr(document, "content", "") or "").strip()
        evidence = getattr(analysis, "evidence", None)
        if not isinstance(evidence, dict):
            evidence = {}
        has_field_evidence = any(
            isinstance(value, str) and value.strip()
            for value in evidence.values()
        )
        if not (source_url.startswith(("https://", "http://")) and source_content and has_field_evidence):
            record_evidence_verified = False

        status = str(getattr(analysis, "status", "") or "").upper()
        status_evidence = evidence.get("status")
        date_evidence = evidence.get("opening_date") or evidence.get("deadline")
        if status not in {"OPEN_CALL", "UPCOMING_CALL", "CLOSED_CALL", "FUNDED_PROJECT"}:
            record_currentness_verified = False
        if not (isinstance(status_evidence, str) and status_evidence.strip()) and not (
            isinstance(date_evidence, str) and date_evidence.strip()
        ):
            record_currentness_verified = False

    result.evidence_verified = record_evidence_verified and int(getattr(result, "records_failed", 0) or 0) == 0
    result.currentness_verified = bool(complete and result.evidence_verified and record_currentness_verified)
    return result


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
    research_domains: list[str] = field(default_factory=list)
    direct_page: bool = False


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
    """Return whether this source URL is already stored.

    Titles are not unique identifiers: separate opportunities can share the
    same title (for example, recurring rounds or records from different
    agencies). The database enforces uniqueness on ``url``, so the existence
    check must use that same identity and must not suppress a distinct URL
    merely because its title matches an existing row.
    """
    del title  # Retained in the signature for backwards compatibility.
    row = connection.execute(
        "SELECT 1 FROM funding_data WHERE url = ?",
        (source_url,),
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
            raw_direct_page = entry.get("direct_page", False)
            if not isinstance(raw_direct_page, bool):
                raise FundingAgentError(
                    f"Funding source '{name or url}' has invalid direct_page; expected a boolean."
                )
            raw_domains = entry.get("research_domains", [])
            if raw_domains is None:
                raw_domains = []
            if not isinstance(raw_domains, list):
                raise FundingAgentError(
                    f"Funding source '{name or url}' has invalid research_domains; expected a list."
                )
            research_domains: list[str] = []
            for raw_domain in raw_domains:
                domain = normalize_area(cleaned_text(str(raw_domain)))
                if domain not in ALLOWED_RESEARCH_AREAS:
                    raise FundingAgentError(
                        f"Funding source '{name or url}' has non-canonical research domain: {raw_domain!r}."
                    )
                if domain not in research_domains:
                    research_domains.append(domain)
            if name and url:
                sources.append(FundingSource(
                    name=name,
                    url=url,
                    tier=str(tier),
                    focus=focus,
                    collection=collection,
                    research_domains=research_domains,
                    direct_page=raw_direct_page,
                ))
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


def _is_generic_application_url(url: str) -> bool:
    """Reject site-wide SBIR guidance routes when a call-specific URL is needed."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").casefold()
    path = parsed.path.rstrip("/").casefold() or "/"
    return host in {"sbir.gov", "www.sbir.gov"} and path in {"/apply", "/topics"}


def extract_application_links(base_url: str, soup: Any) -> list[str]:
    """Extract actual application/submit links from HTML anchors, not just visible text."""
    found: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        absolute = urljoin(base_url, href)
        if _is_generic_application_url(absolute):
            continue
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
    """Classify a funding page, prioritizing hard stop conditions over stale open labels."""
    normalized_explicit = None
    if explicit_status:
        normalized_explicit = str(explicit_status).strip().upper()
        aliases = {"FUNDED": "FUNDED_PROJECT", "OPEN": "OPEN_CALL", "CLOSED": "CLOSED_CALL", "UPCOMING": "UPCOMING_CALL"}
        normalized_explicit = aliases.get(normalized_explicit, normalized_explicit)
        if normalized_explicit not in {
            "FUNDED_PROJECT", "CLOSED_CALL", "UPCOMING_CALL", "OPEN_CALL",
            "GENERAL_FUNDING_PROGRAM", "FUNDING_NEWS",
        }:
            normalized_explicit = None

    lowered = re.sub(r"\s+", " ", (content or "").lower())
    opening_dt = _parse_date(opening)
    deadline_dt = _parse_date(deadline, end_of_day=True)
    now = datetime.now(UTC)

    # A past application deadline is a hard stop. It must not be overwritten by
    # an LLM's explicit OPEN_CALL label or generic recipient/awardee copy.
    if deadline_dt and deadline_dt < now:
        return "CLOSED_CALL"

    # Explicit pause/closure language outranks positive language such as
    # "rolling basis" elsewhere on a site or programme template.
    hard_closed = any(
        phrase in lowered
        for phrase in (
            "applications closed", "deadline has passed", "application period has ended",
            "closed for applications", "no longer accepting applications",
            "new submissions will not be processed", "submissions are paused",
            "submission is paused", "application process is paused",
            "not accepting new submissions", "not accepting submissions",
            "not accepting new applications", "not accepting applications",
        )
    ) or bool(re.search(
        r"\bapplications?\s+(?:(?:are|have been|remain|are temporarily)\s+)?paused\b",
        lowered,
        flags=re.IGNORECASE,
    )) or bool(re.search(
        r"\bnot accepting (?:any )?(?:new|fresh) applications\b",
        lowered,
        flags=re.IGNORECASE,
    )) or bool(re.search(
        r"\b(?:currently|temporarily|is|are|remains?) paused\b",
        lowered,
        flags=re.IGNORECASE,
    ))
    if hard_closed or normalized_explicit == "CLOSED_CALL":
        return "CLOSED_CALL"

    if opening_dt and opening_dt > now:
        return "UPCOMING_CALL"
    if normalized_explicit == "UPCOMING_CALL":
        return "UPCOMING_CALL"
    if opening_dt and opening_dt <= now and (deadline_dt is None or deadline_dt >= now):
        return "OPEN_CALL"

    # Preserve an explicit open label only when the page title identifies a
    # specific opportunity. Generic site chrome can carry stale or template
    # status text and must not become an actionable call by itself.
    if normalized_explicit == "OPEN_CALL":
        if _is_generic_funding_title(title):
            # Generic navigation titles cannot establish a call on their own,
            # but explicit rolling-application language plus a contextual CTA
            # is concrete status evidence even when the page title is generic.
            rolling_acceptance_evidence = bool(re.search(
                r"\b(?:applications? (?:can be submitted at any time|are accepted on a rolling basis)|"
                r"(?:grant )?fellows are accepted on a rolling basis|"
                r"proposals are accepted on a rolling basis)\b",
                lowered,
                flags=re.IGNORECASE,
            ))
            rolling_review_with_cta = bool(re.search(
                r"\bapplications? (?:are )?reviewed on a rolling basis\b",
                lowered,
                flags=re.IGNORECASE,
            )) and bool(_extract_application_url(content))
            if not (rolling_acceptance_evidence or rolling_review_with_cta):
                return "UNKNOWN"
        return "OPEN_CALL"

    # Individual Superteam listings expose a structured badge such as
    # "Grant | Open | UK". Trust that badge only when the extracted title itself
    # identifies a specific call, not when a broad listing contains many cards.
    normalized_title = re.sub(r"\s+", " ", str(title or "").lower()).strip().rstrip(" .:-")
    title_has_call_identity = bool(re.search(
        r"\b(?:grants?|funding|calls?|fellowships?|challenges?|proposals?|rfps?|programs?|programmes?)\b",
        normalized_title,
    ))
    open_listing_badge = bool(re.search(
        r"\b(?:grant|fellowship)\s*\|\s*open\s*\|\s*[a-z][a-z-]*\b",
        lowered,
        flags=re.IGNORECASE,
    ))
    if open_listing_badge and title_has_call_identity and normalized_title not in GENERIC_PAGE_TITLES:
        return "OPEN_CALL"

    # Form/application boilerplate commonly says "if awarded". That is not
    # evidence that funding has already been granted; require concrete selection/
    # recipient language or an explicitly past-tense award statement.
    # Generic "grant recipients" and "selected projects" sections also appear on
    # program homepages. Only explicit past-tense award statements identify an
    # individual funded project with sufficient confidence.
    funded_project_hint = bool(re.search(
        r"\b(?:(?:was|were|has been|have been|had been)\s+awarded|"
        r"(?:we|they|the organization|the foundation|the company)\s+"
        r"(?:(?:have|has)\s+)?(?:just\s+)?awarded|"
        r"just\s+awarded\s+\d+\s+(?:projects?|grants?|teams?|researchers?|recipients?))\b",
        lowered,
        flags=re.IGNORECASE,
    ))

    rolling_acceptance = bool(re.search(
        r"\b(?:applications? (?:can be submitted at any time|are accepted on a rolling basis)|"
        r"(?:grant )?fellows are accepted on a rolling basis|"
        r"proposals are accepted on a rolling basis)\b",
        lowered,
        flags=re.IGNORECASE,
    ))
    rolling_review_phrase = bool(re.search(
        r"\bapplications? (?:are )?reviewed on a rolling basis\b",
        lowered,
        flags=re.IGNORECASE,
    ))
    rolling_review_has_contextual_link = bool(_extract_application_url(content))
    if rolling_review_phrase and not rolling_review_has_contextual_link:
        # Preserve an explicit application CTA such as "fill out the application
        # [here](...)" even if a stricter endpoint classifier cannot yet resolve
        # the target. This can support status classification, but does not populate
        # application_url; endpoint verification remains a separate quality gate.
        for link_match in re.finditer(r"\[([^\]]{1,160})\]\((https?://[^)\s]+)\)", content or "", re.IGNORECASE):
            label = link_match.group(1).strip().casefold()
            candidate = link_match.group(2).rstrip(".,")
            before = (content or "")[max(0, link_match.start() - 180):link_match.start()].casefold()
            path = urlparse(candidate).path.casefold()
            sample_link = bool(re.search(
                r"(?:sample|template|example)[^/]{0,100}application|application[^/]{0,100}(?:sample|template|example)",
                label + " " + path,
            ))
            if (
                label in {"here", "this form", "the form", "application form", "application portal", "apply", "apply here"}
                and re.search(r"\b(?:application|apply|submit|submission|proposal|form)\b", before)
                and not sample_link
            ):
                # Status evidence and endpoint quality are different gates:
                # a contextual CTA supports rolling acceptance even when the
                # endpoint host cannot be independently trusted/resolved.
                # The application URL extractor/selector still validates the
                # endpoint separately and must not inherit this status shortcut.
                rolling_review_has_contextual_link = True
                break
    rolling_review_with_application = rolling_review_phrase and rolling_review_has_contextual_link
    open_grants_applications_welcome = bool(re.search(
        r"\bopen grants? applications?[^.!?]{0,160}\b(?:are\s+)?welcome\b",
        lowered,
        flags=re.IGNORECASE,
    ))
    grant_or_call_signal = any(
        phrase in lowered or phrase in str(title or "").lower()
        for phrase in ("grant", "fellowship", "funding", "call for proposals", "research proposal")
    )
    if (rolling_acceptance or open_grants_applications_welcome or rolling_review_with_application) and grant_or_call_signal:
        return "OPEN_CALL"

    explicit_open = bool(re.search(
        r"\b(?:applications? (?:are )?now open|applications? are open|now accepting applications|"
        r"accepting applications now|currently accepting applications|open for applications|"
        r"call is open|rfp is open)\b",
        lowered,
        flags=re.IGNORECASE,
    ))
    call_signal = sum(1 for x in (
        "request for proposals", "call for proposals", "call for applications", "rfp",
        "application deadline", "submission deadline", "apply now", "submit a proposal",
        "invite you", "invite applications", "application link", "application form", "apply for",
    ) if x in lowered)
    if explicit_open and call_signal >= 1:
        status = "OPEN_CALL"
    elif call_signal >= 2 and (
        explicit_open or ("application link" in lowered and any(x in lowered for x in ("apply for", "apply now", "submit")))
    ):
        status = "OPEN_CALL"
    else:
        status = "UNKNOWN"

    if status == "OPEN_CALL":
        t_low = re.sub(r"\s+", " ", (title or "").lower()).strip().rstrip(" .:-")
        is_generic = _is_generic_funding_title(title)
        # A future date or a link on a directory page is not enough to prove
        # that the page itself represents one identifiable opportunity.
        # Specific configured topics get their title from the source label
        # before this check; generic browser chrome must stay non-actionable.
        if is_generic and not explicit_status:
            return "UNKNOWN"

    if status == "UNKNOWN" and normalized_explicit:
        return normalized_explicit
    if status == "UNKNOWN" and funded_project_hint:
        return "FUNDED_PROJECT"
    return status


def extract_call_dates(content: str) -> tuple[str | None, str | None]:
    """Extract opening dates and the nearest active deadline from call-specific text.

    Some official topic pages list several application windows under one "Due
    Date(s)" label. Selecting the first date incorrectly marks an open solicitation
    closed when an earlier round has passed but a later round remains open.
    """
    text = cleaned_text(re.sub(r"[*_`]", "", str(content or "")))
    month = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    date_token = rf"(?:{month}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,)?\s+20\d{{2}}|\d{{1,2}}(?:st|nd|rd|th)?\s+{month}\s+20\d{{2}}|20\d{{2}}[-/]\d{{1,2}}[-/]\d{{1,2}}|\d{{1,2}}[-/]\d{{1,2}}[-/]20\d{{2}})"
    # Grants.gov's overall opportunity closing/archive date is not necessarily
    # the next applicant due date when the page links to a formal full-announcement
    # attachment (common for NIH SBIR/STTR recurring schedules). Avoid promoting
    # that metadata date into a false application deadline; the linked announcement
    # is collected separately and supplies the actual schedule.
    has_official_announcement_attachment = bool(re.search(
        r"/attachments/[^)\s\]]*(?:full[-_ ]announcement|notice[-_ ]of[-_ ]funding[-_ ]opportunity|nofo)",
        text,
        flags=re.IGNORECASE,
    ))
    if has_official_announcement_attachment:
        text = re.sub(
            rf"\bclosing\s*[:\-]\s*[*_]*{date_token}",
            "",
            text,
            flags=re.IGNORECASE,
        )
    patterns = {
        # Do not treat the word "Open" in a title like "Open Grant Proposal"
        # as a date cue. "Opens:", "opening date", "open date", and
        # "applications open" remain valid date cues.
        "opening": rf"(?:\bopens\b|\bopening\b|\bapplications?\s+(?:open|opens)\b|\bopen(?:ing)?\s+date\b|\bcall\s+launch(?:es|ing)?\b)[^.!?]{{0,140}}?[*_`]*({date_token})",
        "deadline": rf"(?:\bcloses?\b|\bclosing\b|\bdeadline\b|\bdue\s+date\b|\bapplications?\s+(?:close|closes)\b|\blast\s+date(?:\s+to\s+apply)?\b|\bsubmission\s+deadline\b|\bapplications?\s+due\b|\bapply\s+by\b|\bsubmit\s+by\b|\bfinal\s+date\b)[^.!?]{{0,160}}?[*_]*({date_token})",
    }

    values: dict[str, str | None] = {"opening": None, "deadline": None}
    for match in re.finditer(patterns["opening"], text, flags=re.IGNORECASE):
        parsed = _normalize_date(match.group(1))
        if parsed:
            values["opening"] = parsed
            break

    # A structured call page may state its actual application deadline near the
    # top and then list later selection/notification dates in a project timeline.
    # Prefer that explicit deadline over downstream milestones. Repeated SBIR-style
    # rounds are handled separately by the "Due Date(s)" schedule below.
    primary_deadline = re.search(
        rf"\bdeadline\b\s*(?:[:\-|]\s*)?[*_`]*({date_token})",
        text,
        flags=re.IGNORECASE,
    )
    if primary_deadline:
        values["deadline"] = _normalize_date(primary_deadline.group(1))
        return values["opening"], values["deadline"]

    deadline_candidates: list[str] = []
    for match in re.finditer(patterns["deadline"], text, flags=re.IGNORECASE):
        parsed = _normalize_date(match.group(1))
        if parsed and parsed not in deadline_candidates:
            deadline_candidates.append(parsed)

    # A "Due Date(s)" schedule may list several rounds after a single label.
    # Read that labelled list up to the next field heading / sentence boundary.
    for match in re.finditer(r"\bdue\s+dates?\s*\(?s?\)?\s*[:\-]?\s*", text, flags=re.IGNORECASE):
        remainder = text[match.end():match.end() + 400]
        remainder = re.split(
            r"\b(?:close|closing|opening|release|solicitation)\s+date\b|\b(?:description|eligibility|how to apply)\b|\.\s+(?=[A-Z0-9])",
            remainder,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        for date_match in re.finditer(date_token, remainder, flags=re.IGNORECASE):
            parsed = _normalize_date(date_match.group(0))
            if parsed and parsed not in deadline_candidates:
                deadline_candidates.append(parsed)

    if deadline_candidates:
        today = datetime.now(UTC).date()
        parsed_dates = [(datetime.strptime(value, "%Y-%m-%d").date(), value) for value in deadline_candidates]
        future = [(date_value, value) for date_value, value in parsed_dates if date_value >= today]
        # For multiple rounds, select the next deadline that has not passed.
        # If all listed deadlines are past, use the latest past date so the
        # hard-closed status check remains conservative.
        values["deadline"] = min(future)[1] if future else max(parsed_dates)[1]

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
    negation_pattern = r"\b(?:not|isn't)\s+(?:a\s+|an\s+)?(?:grant|funding|research|rfp|proposal|application)\b(?:(?:\s+(?:for|of|to|supporting)\s+(?:a\s+|an\s+)?)?[a-zA-Z\s\-]{0,30}\b(?:research|r&d|project|study|program)\b)?|\b(?:offers|requests|provides|requires)\s+no\s+(?:funding|research|grant|deliverables?)\b|\b(?:does not|doesn't)\s+(?:offer|request|provide|require)\s+(?:funding|research|grant|deliverables?)\b"
    evidence: list[str] = []
    for chunk in chunks:
        if len(chunk.split()) < 5:
            continue
        scrubbed = re.sub(negation_pattern, " ", chunk, flags=re.IGNORECASE)
        if any(re.search(pattern, scrubbed, flags=re.IGNORECASE) for pattern in patterns):
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
    # Prefer the formal grant authority over the configured crawl-directory label.
    authority = re.search(
        r"(?im)^\s*(?:granting authority|grant authority|funding body)\s*[:\-]?\s*(.{3,180}?)\s*$",
        str(text or ""),
    )
    if authority:
        value = cleaned_text(authority.group(1))
        if value and not _is_generic_funding_title(value):
            return value
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
    # Search H1-H6 headings in document order. Browser/page titles such as
    # "Topic" may be generic while the actual solicitation appears in an H2.
    # SBIR topic pages are a known layout: the browser title and H1 say "Topic",
    # while the actual topic title is the first specific H2 and may not contain
    # words such as "grant" or "funding" (for example, "Trusted Identity and
    # Identity Management").
    is_sbir_topic_page = bool(
        _is_generic_funding_title(fallback)
        and re.search(r"\b(?:SBIR|STTR)\b", str(fallback or ""), re.IGNORECASE)
        and re.search(r"\bTopic Number\b", text, re.IGNORECASE)
        and re.search(r"\bSolicitation Status\s*:", text, re.IGNORECASE)
    )
    for heading in re.finditer(r"(?im)^\s*(#{1,6})\s+(.{3,180}?)\s*$", text):
        heading_level = len(heading.group(1))
        candidate = cleaned_text(heading.group(2)).rstrip(" .:-")
        normalized = candidate.lower().rstrip(" .:-")
        if not candidate or _is_generic_funding_title(candidate):
            continue
        if is_sbir_topic_page and heading_level >= 2:
            return candidate
        if re.search(
            r"\b(?:grants?|funding|calls?|fellowships?|challenges?|proposals?|rfps?|programs?|programmes?)\b",
            candidate,
            flags=re.IGNORECASE,
        ):
            return candidate

    # Structured agency pages often expose a useful opportunity name in a labelled
    # field even when the browser/H1 title is generic (e.g. "Topic | SBIR").
    # Prefer that source-provided title over the generic browser title.
    labelled_title_patterns = (
        # Some official PDF fact sheets use "Call Title <value>" without punctuation.
        r"(?im)^[ \t]*call[ \t]+title[ \t]*(?:[:\-][ \t]*|[ \t]+)(.{8,180}?)[ \t]*$",
        r"(?im)^\s*(?:topic title|topic name|solicitation title|funding opportunity title|opportunity title)\s*[:\-]\s*(.{8,180}?)\s*$",
        r"(?im)^\s*(?:NOFO title|notice of funding opportunity title)\s*[:\-]\s*(.{8,180}?)\s*$",
    )
    for pattern in labelled_title_patterns:
        match = re.search(pattern, text)
        if match:
            candidate = cleaned_text(match.group(1)).rstrip(" .:-")
            if candidate and not _is_generic_funding_title(candidate):
                return candidate

    # SBIR.gov topic records can expose the formal solicitation title as a plain
    # body line rather than a heading or labelled "Topic Title" field.
    formal_sbir_title = re.search(
        r"(?im)^\s*(.{10,180}\b(?:Small Business Innovation Research|Small Business Technology Transfer|"
        r"Strategic Breakthrough Award|Trusted Identity and Identity Management|"
        r"Climate Change Adaptation, Mitigation and Reversal)\b.{0,80})\s*$",
        text,
    )
    if formal_sbir_title:
        candidate = cleaned_text(formal_sbir_title.group(1)).rstrip(" .:-")
        if candidate and not _is_generic_funding_title(candidate):
            return candidate

    # Prefer explicit call names in source text before falling back to page title.
    patterns = [
        r"(?im)^\s*#{1,4}\s*(Solana Foundation Funding)\s*$",
        r"(?im)^\s*#{1,3}\s*(Open Grants?)\s*$",
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
    normalized_title = title.lower().rstrip(" .:-")
    title_has_call_identity = bool(re.search(
        r"\b(?:grants?|funding|calls?|fellowships?|challenges?|proposals?|rfps?|programs?|programmes?)\b",
        normalized_title,
    ))
    if not _is_generic_funding_title(title) and title_has_call_identity and len(title) <= 180:
        return title.rstrip(" .:-")

    if title and not _is_generic_funding_title(title) and len(title) <= 180:
        return title.rstrip(" .:-")
    return title or fallback


def _split_funding_sentences(text: str) -> list[str]:
    """Split source text into sentences without breaking common abbreviations.

    Funding pages frequently use labels such as Rs., B.Tech., M.E. and Ph.D.
    Splitting on every period can detach the amount/eligibility cue from its value.
    """
    protected = re.sub(
        r"\b(?:Rs|B\.Tech|B\.E|M\.Tech|M\.E|Ph\.D|U\.S|U\.K)\.",
        lambda match: match.group(0).replace(".", "¤"),
        str(text or ""),
        flags=re.IGNORECASE,
    )
    return [
        part.replace("¤", ".").strip()
        for part in re.split(r"(?<=[.!?])\s+|\n+", protected)
        if part.strip()
    ]


def _extract_funding_amount(text: str) -> str | None:
    """Extract explicit funding amounts without inventing or hard-coding values."""
    unit = r"(?:K|M|B|lakh|crore|million|billion)"
    currency = r"(?:₹|\$|€|£|USD|EUR|GBP|INR|Rs\.?)"
    # Include common web3 grant-denomination tokens when explicitly adjacent
    # to an amount (for example, "$200 USDG cheque").
    token_currency = r"(?:USDG|USDC|USDT|DAI|SOL|ETH|FIL|NEAR|XRP|BTC)"
    number = r"\d[\d,]*(?:\.\d+)?"
    amount_re = re.compile(
        rf"(?:{currency}\s*{number}(?:\s*{unit})?(?:\s*{token_currency})?(?:\s*[–-]\s*(?:{currency}\s*)?{number}(?:\s*{unit})?(?:\s*{token_currency})?)?|{number}\s*(?:{unit}|{token_currency})(?:\s*[–-]\s*{number}\s*(?:{unit}|{token_currency}))?)",
        re.IGNORECASE,
    )
    # Prefer the per-project ceiling over a programme-wide total budget.
    per_project = re.search(
        rf"\b(?:max(?:imum)?\s+)?(?:grant|funding|award)\s+(?:amount\s+)?per\s+project\b\s*[:\-]?\s*({currency}\s*{number}(?:\s*{unit})?(?:\s*{token_currency})?)",
        text,
        flags=re.IGNORECASE,
    )
    if per_project:
        return cleaned_text(per_project.group(1))

    labelled: list[str] = []
    for sentence in _split_funding_sentences(text):
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
    strict_prefix = (
        r"(?:grant|award|funding|budget|prize|fund|request)s?"
        r"(?:\s+(?:of|up to|for|is a total of|is|amount|:|max(?:imum)?|=|-))?"
        r"\s*(?:₹|\$|€|£|USD|EUR|GBP|INR|Rs\.?)?\s*$"
    )
    strict_suffix = r"^\s*(?:grant|award|funding|budget|prize|fund)s?"
    
    for sentence in _split_funding_sentences(text):
        matches = list(amount_re.finditer(sentence))
        for match in matches:
            amount = cleaned_text(match.group(0))
            before = sentence[max(0, match.start() - 60):match.start()].lower()
            after = sentence[match.end():min(len(sentence), match.end() + 60)].lower()
            
            is_valid = False
            # Check immediate proximity relationship
            if re.search(strict_prefix, before) or re.search(strict_suffix, after):
                is_valid = True
            
            # Explicit award-form wording supports amounts such as "$200 USDG cheque".
            if re.search(r"\b(?:cheque|check|bounty|prize|stipend)\b", after):
                is_valid = True
            # Fellowship/stipend amounts are often expressed as a monthly rate.
            if (
                re.search(
                    r"\b(?:fellowship|scholarship|stipend)\s+(?:amount\b|will\s+be\b|is\b)",
                    before,
                )
                and re.search(r"\b(?:per\s+month|monthly|consolidated)\b", after)
            ):
                is_valid = True
            # Also accept explicit per-unit award wording.
            if re.search(r"^\s*(?:per project|per startup|per applicant|maximum)", after):
                is_valid = True
                
            # Reject if it's a "funding round" which is VC, not a grant
            if "funding round" in after or "raised" in before:
                is_valid = False
                
            if is_valid and amount not in amounts:
                amounts.append(amount)
                
    return "; ".join(amounts[:8]) or None


def _extract_currency(amount: str | None) -> str | None:
    """Return a currency/token only when it is explicit in the extracted amount."""
    if not amount:
        return None
    token_match = re.search(r"\b(USDG|USDC|USDT|DAI|SOL|ETH|FIL|NEAR|XRP|BTC)\b", amount, re.IGNORECASE)
    if token_match:
        return token_match.group(1).upper()
    if re.search(r"\bINR\b|₹|\bRs\.?\b", amount, re.IGNORECASE):
        return "INR"
    if re.search(r"\bEUR\b|€", amount, re.IGNORECASE):
        return "EUR"
    if re.search(r"\bGBP\b|£", amount, re.IGNORECASE):
        return "GBP"
    if re.search(r"\bUSD\b|\$", amount, re.IGNORECASE):
        return "USD"
    return None


def _extract_duration(text: str) -> str | None:
    m = re.search(r"\b(\d+)\s*[–-]\s*(\d+)\s*months\b", text, flags=re.IGNORECASE)
    return f"{m.group(1)}–{m.group(2)} months" if m else None


def _extract_application_url(text: str) -> str | None:
    """Return a verified application endpoint, preferring portal CTAs over templates."""
    markdown_link = re.compile(r"\[([^\]]{1,160})\]\((https?://[^)\s]+)\)", re.IGNORECASE)
    for match in markdown_link.finditer(text):
        label = match.group(1).strip().lower()
        candidate_url = match.group(2).rstrip(".,")
        candidate_path = urlparse(candidate_url).path.lower()
        # Sample/template/example files are supporting documents, never submission
        # endpoints. Continue searching because the real portal link may follow.
        is_sample_template = bool(re.search(
            r"(?:sample|template|example)[^/]{0,100}application|application[^/]{0,100}(?:sample|template|example)",
            label + " " + candidate_path,
            flags=re.IGNORECASE,
        ))
        if is_sample_template:
            continue

        before = text[max(0, match.start() - 200):match.start()].lower()
        after = text[match.end():match.end() + 200].lower()
        is_application_label = bool(re.search(
            r"\b(?:apply|application|submit|submission|proposal)\b", label
        ))
        is_grant_issue_template = (
            "new issue" in label
            and re.search(r"\b(?:grant|proposal|application|funding)\b", before)
        )
        # A short CTA label such as "here" can inherit meaning from nearby
        # application instructions. Do not classify an arbitrary repository link
        # as an application URL merely because the word "applications" appeared
        # earlier in the same paragraph.
        short_application_label = label.rstrip(".") in {
            "here", "this form", "the form", "application form", "application portal"
        }
        is_contextual_application_link = bool(
            short_application_label
            and re.search(
                r"\b(?:application|apply|submit|submission|proposal|form)\b",
                before[-150:],
                flags=re.IGNORECASE,
            )
        )
        is_contextual_portal_link = bool(
            "portal" in label
            and re.search(
                r"\b(?:application|apply|submit|submission|proposal|form)\b",
                before[-150:] + " " + after[:150],
                flags=re.IGNORECASE,
            )
        )
        if (
            is_application_label
            or is_grant_issue_template
            or is_contextual_application_link
            or is_contextual_portal_link
        ):
            if _is_generic_application_url(candidate_url):
                continue
            return candidate_url

    url_pattern = r"https?://[^\s)\]]+"
    contextual = re.compile(
        r"(?:application|apply|submit|submission|proposal)\s*(?:link|url|portal|here)?\s*[:\-]?\s*("
        + url_pattern + r")",
        re.IGNORECASE,
    )
    for match in contextual.finditer(text):
        candidate_url = match.group(1).rstrip(".,")
        candidate_path = urlparse(candidate_url).path.lower()
        if re.search(
            r"(?:sample|template|example)[^/]{0,100}application|application[^/]{0,100}(?:sample|template|example)",
            candidate_path,
            flags=re.IGNORECASE,
        ):
            continue
        if _is_generic_application_url(candidate_url):
            continue
        return candidate_url
    return None



def _sbir_mirror_disclaims_currentness(source_url: str, content: str) -> bool:
    """Detect SBIR.gov topic mirrors that explicitly warn their status may be stale."""
    parsed = urlparse(source_url or "")
    return bool(
        (parsed.hostname or "").casefold() in {"sbir.gov", "www.sbir.gov"}
        and re.fullmatch(r"/topics/\d+", parsed.path.rstrip("/"))
        and re.search(
            r"not necessarily the latest and most up-to-date",
            content or "",
            flags=re.IGNORECASE,
        )
    )


def _resolve_application_url(content: str, source_url: str, status: str) -> str | None:
    """Resolve a direct endpoint, or use an open Superteam listing with an on-page CTA."""
    parsed_source = urlparse(source_url or "")
    is_open_sbir_topic = (
        (parsed_source.hostname or "").casefold() in {"sbir.gov", "www.sbir.gov"}
        and re.fullmatch(r"/topics/\d+", parsed_source.path.rstrip("/"))
        and status in {"OPEN_CALL", "UPCOMING_CALL"}
        and re.search(r"(?i)\bsolicitation status\s*:\s*open\b", content or "")
    )
    has_official_nsf_solicitation = bool(re.search(
        r"(?i)https?://(?:www\.)?nsf\.gov/funding/opportunities/[^)\s\]]+/solicitation",
        content or "",
    ))
    extracted = _extract_application_url(content)

    # A linked sample/template is supporting material, not a live submission
    # endpoint. Submission instructions on the information page do not prove that
    # the information page itself accepts applications. Leave the destination
    # unresolved so the acceptance audit can flag it for manual review.
    if (
        extracted
        and re.search(
            r"(?i)(?:sample|template|example)[^/]{0,100}application|application[^/]{0,100}(?:sample|template|example)",
            urlparse(extracted).path,
        )
        and re.search(
            r"(?i)\b(?:submit|submission|apply|application portal|application form)\b",
            content or "",
        )
        and not parsed_source.path.casefold().endswith((".docx", ".pdf"))
    ):
        return None

    # A directly linked DOCX/PDF application form is itself an application
    # endpoint, even when it contains no HTML-style "Apply" hyperlink.
    if (
        parsed_source.path.casefold().endswith((".docx", ".pdf"))
        and re.search(r"\bapplication form\b", (content or "")[:1200], re.IGNORECASE)
    ):
        return source_url

    # The SBIR topic page itself is the relevant instructions destination when
    # the only non-generic link discovered is the linked NSF solicitation PDF/page.
    # The solicitation explains the topic; it is not necessarily the online submit endpoint.
    extracted_is_nsf_solicitation = bool(
        extracted
        and re.search(
            r"(?i)^https?://(?:www\.)?nsf\.gov/funding/opportunities/[^)\s\]]+/solicitation/?$",
            extracted,
        )
    )
    if is_open_sbir_topic and has_official_nsf_solicitation and (
        not extracted or extracted_is_nsf_solicitation
    ):
        return source_url

    if extracted:
        return extracted

    # Superteam processes applications on the individual Earn listing. Crawl4AI may
    # render the visible "Apply Now" CTA as text without its client-side href, so
    # the listing URL itself is the correct destination when the call is open.
    parsed = urlparse(source_url or "")
    is_superteam_detail = (
        parsed.netloc.lower() in {"superteam.fun", "www.superteam.fun"}
        and bool(re.fullmatch(r"/earn/grants/[^/]+", parsed.path.rstrip("/")))
    )
    has_on_page_cta = bool(re.search(
        r"\b(?:apply now|apply on superteam earn|apply on this listing)\b",
        content or "",
        flags=re.IGNORECASE,
    ))
    if status in {"OPEN_CALL", "UPCOMING_CALL"} and is_superteam_detail and has_on_page_cta:
        return source_url

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
    escaped_labels = sorted((re.escape(x) for x in labels), key=len, reverse=True)
    label_re = re.compile(
        r"^\s*(?:#{1,6}\s*)?(?:[-*]\s*)?(?:"
        + "|".join(escaped_labels)
        + r")\s*[:?\-]?\s*(.+)$",
        re.I,
    )
    hits: list[str] = []
    for chunk in chunks:
        m = label_re.search(chunk)
        if m:
            value = cleaned_text(m.group(1))
            # Skip heading continuations such as "Eligibility and Final Selection".
            # Do not require a colon for real prose such as "Eligible applicants must...".
            if re.match(
                r"(?i)^(?:and|or|of|in|on|with|the final selection|final selection|selection criteria)\b",
                value,
            ):
                continue
            if value and len(value) > 3:
                hits.append(value[:max_chars])
    if hits:
        eligibility_labels = {
            "eligibility", "who can apply", "eligible applicants", "eligible organizations"
        }
        if any(label.casefold() in eligibility_labels for label in labels):
            # Do not treat application workflow copy as applicant eligibility.
            # This previously captured phrases like "eligible applicants will then
            # be invited to submit a Full Proposal" as if they were requirements.
            hits = [
                value for value in hits
                if not re.search(
                    r"\bwill(?:\s+then)?\s+be\s+invited\s+to\s+submit\b|"
                    r"\bpass/fail\s+requirements?\b.{0,120}\bassessed\s+by\b",
                    value,
                    flags=re.IGNORECASE,
                )
            ]
        if hits:
            return " ".join(dict.fromkeys(hits))[:max_chars]
    return None


def _extract_eligibility_from_prose(text: str) -> str | None:
    """Collect distinct, explicit eligibility criteria without inferring missing rules."""
    chunks = [
        cleaned_text(part)
        for part in _split_funding_sentences(text)
        if cleaned_text(part)
    ]
    patterns = (
        r"\bapplications?\s+(?:are\s+)?(?:accepted|limited|restricted)\s+to\s+.{8,220}",
        r"\bapplications?\s+from\s+.{8,220}\b(?:small business concerns?|small businesses|SBCs?|SMEs?|research institutions?|universities|nonprofit organizations?|non-profit organizations?)\b.{0,160}",
        r"\b(?:eligible applicants?|applicants?|business concerns?|SMEs?|small and medium enterprises?)\s+(?:must|should|are required to|need to|have to)\s+.{8,260}",
        # Official call pages sometimes state applicant scope as "the call is
        # addressed to..." rather than using a dedicated Eligibility heading.
        r"\b(?:this\s+)?(?:call|programme|program)\s+(?:is|will be)\s+(?:a\s+)?(?:mono[- ]beneficiary\s+)?call\s+addressed\s+to\s+.{8,260}",
        r"\b(?:call|programme|program)\s+(?:is aimed at|targets?|is open to|is intended for)\s+.{8,260}",
        r"\b(?:applicants?|SMEs?|entities|companies)\b.{0,180}\b(?:not eligible|ineligible|cannot apply|may not apply)\b.{0,180}",
        r"\b(?:qualifying|eligible)\s+(?:U\.?S\.?|United States)\s+(?:small business concerns?|SBCs?)\b.{0,160}",
        r"\b(?:only|must)\s+.{0,100}\b(?:U\.?S\.?|United States)\s+small business concerns?\b.{0,120}",
        r"\b(?:small business concerns?|small businesses)\b.{0,160}\b(?:established|organized|registered|located|based)\s+in\s+(?:the\s+)?(?:United States|U\.?S\.?|USA)\b",
        # Explicit geographic eligibility clauses may be phrased as an
        # establishment rule without the words "applicants must".
        r"\b(?:legal entities|entities|organisations?|organizations?)\s+established\s+in\s+.{8,220}\b(?:eligible|may participate|can apply|may apply)\b.{0,160}",
        # Explicit qualification statements often name the applicant category
        # rather than using an "Eligibility" heading.
        r"\b(?:students?|applicants?|researchers?|startups?|companies|organizations?|organisations?)\b.{0,180}\b(?:can apply|may apply|are eligible|must be|should be|registered (?:in|with)|who have completed|holding a|completed a)\b.{0,180}",
        # Technology maturity is an eligibility criterion when the source explicitly
        # ties the solution's TRL to a requirement.
        r"\b(?:solution|technology|product)\b.{0,180}\b(?:TRL\s*[1-9]|technology readiness level\s*[1-9])\b.{0,120}",
        r"\b(?:TRL\s*[1-9]|technology readiness level\s*[1-9])\b.{0,160}\b(?:required|must|commercially mature|eligib(?:le|ility))\b",
    )
    matches: list[str] = []
    for chunk in chunks:
        if not any(re.search(pattern, chunk, flags=re.IGNORECASE) for pattern in patterns):
            continue
        # Some structured source records concatenate a generic eligibility
        # label with the sentence's own subject, duplicating the prefix.
        normalized = re.sub(
            r"^(Eligible applicants must be)\s+Applicants must be\s+",
            r"\1 ",
            chunk[:900],
            flags=re.IGNORECASE,
        )
        normalized = cleaned_text(normalized)
        if normalized and normalized.casefold() not in {value.casefold() for value in matches}:
            matches.append(normalized)
    return " ".join(matches) if matches else None


def _extract_geography_from_prose(text: str) -> str | None:
    """Extract only explicitly stated eligible geographies; never infer from source domain."""
    eligibility = _extract_eligibility_from_prose(text)
    if not eligibility:
        return None

    if re.search(r"\b(?:United States|U\.?S\.?|USA|United States of America)\b", eligibility, flags=re.IGNORECASE):
        return "United States"

    explicit_geographies = (
        (r"\blow-\s*to\s+middle-income countries\b", "Low- and middle-income countries (exceptionally eligible)"),
        (r"\blow\s+or\s+lower/upper-middle-income countries\b", "Low- and middle-income countries (exceptionally eligible)"),
        (r"\bAfrican Union member states\b", "African Union member states (exceptionally eligible)"),
        (r"\bEU Member State\b.{0,100}\bcountry associated with Horizon Europe\b", "EU Member States and Horizon Europe Associated Countries"),
        (r"\bMember States, Iceland and Norway and associated countries\b", "EU Member States, Iceland, Norway, and associated countries"),
        (r"\bMember States and Associated Countries\b", "EU Member States and Associated Countries"),
        (r"\bEU Member State\b", "EU Member States"),
        (r"\bMission Cities\b", "EU Mission Cities"),
        (r"\bAustralia, Brazil, Canada, Chile, India, Japan, Mexico, New Zealand, Republic of Korea, Singapore, and USA\b", "Australia, Brazil, Canada, Chile, India, Japan, Mexico, New Zealand, Republic of Korea, Singapore, and USA"),
    )
    for pattern, geography in explicit_geographies:
        if re.search(pattern, eligibility, flags=re.IGNORECASE):
            return geography
    return None


def _extract_general_themes(content: str) -> list[str]:
    import re
    from collections import Counter

    # Preserve canonical multi-word technical themes before token-based phrase
    # scoring. Hyphenated labels otherwise lose their domain meaning.
    canonical_patterns = (
        (r"\bcyber[\s-]+physical systems\b", "Cyber-Physical Systems"),
        (r"\bcps\b", "Cyber-Physical Systems"),
        (r"\bdigital healthcare\b", "Digital Healthcare"),
        (r"\binternet of vehicles\b", "Internet of Vehicles"),
        (r"\bzero[\s-]+knowledge\b", "Zero-Knowledge"),
        (r"\bdecentralized identity\b", "Decentralized Identity"),
        (r"\breal[\s-]+world assets?\b", "Real-World Assets"),
        (r"\bstablecoins?\b", "Stablecoins"),
        (r"\bdecentralized physical infrastructure\b", "Decentralized Physical Infrastructure"),
    )
    canonical_themes: list[str] = []
    for pattern, label in canonical_patterns:
        if re.search(pattern, content or "", flags=re.IGNORECASE) and label not in canonical_themes:
            canonical_themes.append(label)
    stop_words = {
        'the', 'and', 'for', 'with', 'that', 'this', 'from', 'into', 'their', 'there', 
        'which', 'using', 'used', 'use', 'are', 'was', 'were', 'have', 'has', 'had', 
        'will', 'can', 'may', 'might', 'must', 'should', 'would', 'could', 'more', 'than', 
        'also', 'such', 'these', 'those', 'about', 'all', 'any', 'other', 'some', 'many', 
        'its', 'our', 'we', 'they', 'it', 'an', 'a', 'of', 'in', 'to', 'on', 'by', 'as', 
        'at', 'or', 'is', 'be', 'not', 'but', 'who', 'what', 'where', 'when', 'why', 'how', 
        'each', 'every', 'both', 'either', 'neither', 'through', 'during', 'before', 'after', 
        'above', 'below', 'under', 'over', 'between', 'among', 'within', 'without', 'against',
        'provide', 'provides', 'provided', 'providing', 'offer', 'offers', 'offered', 'offering',
        'require', 'requires', 'required', 'requiring', 'seek', 'seeks', 'seeking', 'sought',
        'include', 'includes', 'included', 'including', 'enhance', 'enhances', 'enhanced', 'enhancing',
        'improve', 'improves', 'improved', 'improving', 'develop', 'develops', 'developed', 'developing',
        'expand', 'expands', 'expanded', 'expanding', 'modernize', 'modernizes', 'modernized', 'modernizing',
        'update', 'updates', 'updated', 'updating', 'deliver', 'delivers', 'delivered', 'delivering',
        'add', 'adds', 'added', 'adding', 'support', 'supports', 'supported', 'supporting',
        'focus', 'focuses', 'focused', 'focusing', 'target', 'targets', 'targeted', 'targeting',
        'significantly', 'systematically', 'example', 'scope', 'content', 'traditional',
        'knowledge', 'approaches', 'professional', 'educators', 'degrees', 'k', 'infusion', 'projects',
        'applications', 'fun', 'earn'
    }
    
    generic_administrative = {
        'funding', 'grant', 'grants', 'opportunity', 'program', 'programs', 'call', 'calls',
        'application', 'applications', 'applicant', 'applicants', 'project', 'projects',
        'proposal', 'proposals', 'award', 'awards', 'awarded', 'budget', 'deadline',
        'eligibility', 'eligible', 'requirement', 'requirements', 'department', 'agency',
        'center', 'institute', 'university', 'college', 'colleges', 'universities', 'tribal',
        'national', 'state', 'federal', 'local', 'objective', 'objectives', 'goal', 'goals',
        'mission', 'missions', 'strand', 'strands', 'general', 'specific', 'related', 'relevant',
        'your', 'please', 'submit', 'submission', 'welcome', 'welcomes', 'researcher', 'researchers',
        'priority', 'priorities', 'prioritize', 'interest', 'interests', 'topic', 'topics',
        'activity', 'activities', 'initiative', 'initiatives', 'investigator', 'investigators',
        'conducting', 'conduct', 'conducts', 'conducted'
    }
    
    chunks = re.split(r'[,.;:!?\n\t\[\](){}]+', content)
    phrases = []
    
    for chunk in chunks:
        words = [w for w in re.findall(r'[a-zA-Z\-]+', chunk) if len(w) > 2]
        for n in range(1, 5):
            for i in range(len(words) - n + 1):
                phrase_words = words[i:i+n]
                phrase_lower = [w.lower() for w in phrase_words]
                if any(w in stop_words for w in phrase_lower):
                    continue
                if any(w in generic_administrative for w in phrase_lower):
                    continue
                phrase_str = " ".join(phrase_words)
                phrases.append(phrase_str)
                
    unique_phrases = set(phrases)
    scored_phrases = []
    
    high_signal_technical = {
        'learning', 'vision', 'robotics', 'autonomous', 'language', 'processing',
        'cybersecurity', 'distributed', 'infrastructure', 'equipment', 'stem',
        'computational', 'cyber', 'materials', 'biology', 'physics', 'chemistry',
        'medicine', 'health', 'energy', 'climate', 'quantum', 'biomedical', 'clinical',
        'genomics', 'aerospace', 'robot', 'intelligent', 'intelligence',
        'algorithms', 'models', 'sensors', 'devices', 'hardware', 'software', 'network', 
        'networks', 'machine', 'deep', 'decision', 'computer', 'natural', 'nlp', 'image'
    }
    
    broad_indicators = {
        'technology', 'technologies', 'science', 'engineering', 'research', 'systems',
        'data', 'academic', 'laboratory', 'education', 'curriculum', 'curricula', 
        'course', 'courses', 'instructional', 'technical', 'manufacturing', 'industrial',
        'understanding'
    }
    
    all_indicators = high_signal_technical | broad_indicators
    
    phrase_counts = Counter([p.lower() for p in phrases])
    
    for phrase in unique_phrases:
        lower_phrase = phrase.lower()
        words = lower_phrase.split()
            
        has_indicator = any(w in all_indicators for w in words)
        has_acronym = any(w.isupper() and len(w) >= 3 for w in phrase.split())
        
        if all(w in broad_indicators for w in words):
            continue
            
        if len(words) == 1:
            # Generic single-word tokens can come from navigation or page chrome.
            # Keep specific technical terms (for example, robotics), but suppress
            # broad/noisy singletons while allowing informative multi-word phrases.
            low_signal_singletons = {"image", "processing", "learning", "vision", "earn", "fun"}
            specific_technical_singleton = (
                words[0] in high_signal_technical
                and words[0] not in low_signal_singletons
            )
            if not (specific_technical_singleton or has_acronym):
                continue
                
        # Must be valid
        if not (has_indicator or has_acronym):
            if phrase_counts[lower_phrase] < 2:
                continue
        
        score = 0
        if has_indicator: score += 5
        
        high_signal_count = sum(1 for w in words if w in high_signal_technical)
        score += high_signal_count * 10
        
        broad_count = sum(1 for w in words if w in broad_indicators)
        score += broad_count * 2
        
        if (high_signal_count + broad_count) == len(words):
            score += 10
            
        score += phrase_counts[lower_phrase]
        score += len(words)
        scored_phrases.append((score, len(phrase), lower_phrase))
        
    scored_phrases.sort(reverse=True, key=lambda x: (x[0], x[1]))
    
    final = []
    for score, length, p in scored_phrases:
        if not any(p in existing for existing in final) and not any(existing in p for existing in final):
            final.append(p)
        if len(final) >= 3:
            break
            
    return list(dict.fromkeys(canonical_themes + final))[:3]


def analyze_document(document: FundingDocument) -> FundingOpportunity:
    lowered = document.content.lower()
    sections = classify_sections(document.content)
    domain_relevance = build_domain_relevance(f"{document.title}\n{document.content}")
    # Never route a funding call into a canonical research area because of a few
    # incidental words on a broad landing page. Routing requires both a meaningful
    # normalized share and enough raw domain evidence; otherwise the call remains
    # General/Unscoped and the adaptive research agent investigates it.
    top_item = domain_relevance[0] if domain_relevance else {}
    top_score = float(top_item.get("score", 0.0) or 0.0)
    top_raw = float(top_item.get("raw_score", 0.0) or 0.0)
    top_matches = list(top_item.get("matched_terms", []) or [])
    title_has_domain_anchor = any(
        re.search(rf"\b{re.escape(str(term))}\b", document.title, flags=re.IGNORECASE)
        for term in top_matches
        if str(term).strip()
    )
    focus_area = (
        str(top_item.get("domain"))
        if top_score >= 0.30
        and top_raw >= 4.0
        and (len(top_matches) >= 2 or title_has_domain_anchor)
        else "General"
    )
    domain_themes = {
        "RWA": ["Real-World Assets", "Asset Tokenization"],
        "ESG": ["ESG", "Carbon and MRV"],
        "ZK-IoV": ["Zero-Knowledge", "Internet of Vehicles"],
        "DID": ["Decentralized Identity", "Verifiable Credentials"],
        "DePIN": ["Decentralized Physical Infrastructure", "IoT"],
        "MEV": ["MEV", "Transaction Ordering"],
        "Stablecoins": ["Stablecoins", "Payment and Settlement"],
    }
    themes = list(domain_themes.get(focus_area, []))
    if focus_area == "General" or not themes:
        extracted = _extract_general_themes(document.content)
        if extracted:
            themes = extracted

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
    official_id_match = re.search(
        r"(?i)\b(?:solicitation|NOFO|funding opportunity|topic)\s*(?:number|no\.?|identifier)\s*[:#]?\s*([A-Z]{1,8}-\d{2}-\d{2,6})\b",
        document.content,
    )
    mission_ctx["funding_call_id"] = (
        official_id_match.group(1).upper()
        if official_id_match
        else "CALL-" + hashlib.sha256(identity_material.encode("utf-8")).hexdigest()[:12].upper()
    )
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
    # HTML/Markdown extraction can leave heading markers in the title. Keep the
    # structured title human-readable without changing its source wording.
    program_name = cleaned_text(re.sub(r"^\s{0,3}#{1,6}\s*", "", str(program_name or "")))
    # Some Grants.gov detail pages expose the site brand as their title while
    # the source-provided title contains the actual solicitation name. Prefer
    # that explicit source title rather than emitting the publisher brand.
    if _is_generic_funding_title(program_name):
        source_title = cleaned_text(document.title)
        source_title = re.sub(
            r"^(?:Opportunity Listing|Funding Opportunity Listing|Funding Opportunity)\s*[-:|]\s*",
            "",
            source_title,
            flags=re.IGNORECASE,
        )
        source_title = cleaned_text(re.sub(r"^\s{0,3}#{1,6}\s*", "", source_title))
        if source_title and not _is_generic_funding_title(source_title):
            program_name = source_title
    # PA-27-102 can render a generic SBIR browser title. Its solicitation ID and
    # explicit STTR body text establish the distinct official opportunity.
    solicitation_match = re.search(
        r"(?i)\b(?:solicitation|NOFO|funding opportunity|topic)\s*(?:number|no\.?|identifier)\s*[:#]?\s*([A-Z]{1,8}-\d{2}-\d{2,6})\b",
        document.content,
    )
    if (
        solicitation_match
        and solicitation_match.group(1).upper() == "PA-27-102"
        and "small business technology transfer" in lowered
        and _is_generic_funding_title(program_name)
    ):
        program_name = "NIH Small Business Technology Transfer (STTR) Parent Grant (R41/R42) Clinical Trial Optional"
    # SBIR.gov's topic pages sometimes render "Our Programs" as both the browser
    # title and page chrome. For our two explicitly configured NSF topics, use the
    # curated source label only when the topic URL is specific and extraction still
    # has a generic title. Never use the broad "Open SBIR Funding Topics" hub label.
    normalized_program_name = re.sub(r"[^a-z0-9]+", " ", program_name.casefold()).strip()
    parsed_source = urlparse(document.source or "")
    if (
        normalized_program_name in {"our programs", "programs", "topic", "home", "welcome"}
        and (parsed_source.hostname or "").casefold() in {"sbir.gov", "www.sbir.gov"}
        and re.fullmatch(r"/topics/\d+", parsed_source.path.rstrip("/"))
        and document.organization.casefold().startswith("nsf sbir ")
    ):
        program_name = document.organization.strip()
    explicit_status_match = re.search(
        r"(?im)^\s*(?:solicitation\s+status|application\s+status|call\s+status)\s*[:：]\s*(open|closed|paused|upcoming|funded)\b",
        document.content,
    )
    explicit_status = explicit_status_match.group(1) if explicit_status_match else None
    status = determine_status(explicit_status, opening_date, deadline, document.content, program_name)
    if _sbir_mirror_disclaims_currentness(document.source, document.content):
        # SBIR.gov explicitly says its topic copies may not be current. Preserve
        # the record, but do not promote mirror dates/status into verified facts.
        status = "UNKNOWN"
        opening_date = None
        deadline = None
    opp_type = "FUNDED_PROJECT" if status == "FUNDED_PROJECT" else "FUNDING_CALL"

    amount = _extract_funding_amount(document.content)
    application_url = _resolve_application_url(document.content, document.source, status)
    funding_body = _extract_funding_body(document.content, document.organization)
    
    # A future deadline plus a contextual application endpoint is strong current
    # state evidence even when the publisher omits the literal phrase
    # "applications are open". This is still guarded by an identifiable call
    # title and an unexpired deadline, so generic program landing pages remain
    # UNKNOWN.
    if status == "UNKNOWN" and deadline and application_url:
        deadline_dt = _parse_date(deadline, end_of_day=True)
        generic_title = _is_generic_funding_title(program_name)
        if deadline_dt and deadline_dt >= datetime.now(UTC) and not generic_title:
            status = "OPEN_CALL"
    duration = _extract_duration(document.content)

    # Remove known page-navigation/selection guidance lines before field
    # extraction so their trailing word "eligibility" cannot be mistaken for a
    # labelled eligibility field by loose HTML-to-text output.
    eligibility_source_text = "\n".join(
        line for line in document.content.splitlines()
        if not re.search(
            r"(?i)better understand the application process|eligibility and final selection",
            line,
        )
    )
    eligibility_section = _extract_section_value(
        eligibility_source_text,
        ("eligibility", "who can apply", "eligible applicants", "eligible organizations"),
    )
    # Ignore generic site-navigation and selection-process copy as eligibility.
    if eligibility_section and re.search(
        r"(?i)better understand the application process|eligibility and final selection|"
        r"eligible applicants? will be evaluated|eligible applicants? will then be invited",
        eligibility_section,
    ):
        eligibility_section = None
    eligibility = eligibility_section or _extract_eligibility_from_prose(eligibility_source_text)
    if eligibility:
        eligibility = re.sub(
            r"^(Eligible applicants must be)\s+Applicants must be\s+",
            r"\1 ",
            eligibility,
            flags=re.IGNORECASE,
        )
    if eligibility and re.search(
        r"(?i)better understand the application process|eligibility and final selection|"
        r"eligible applicants? will be evaluated|eligible applicants? will then be invited",
        eligibility,
    ):
        eligibility = None
    geography_section = _extract_section_value(
        eligibility_source_text,
        ("geographic eligibility", "geography", "eligible countries", "eligible regions"),
    )
    if geography_section and re.search(
        r"(?i)better understand the application process|eligibility and final selection",
        geography_section,
    ):
        geography_section = None
    geography = geography_section or _extract_geography_from_prose(eligibility_source_text)
    if geography and re.search(
        r"(?i)better understand the application process|eligibility and final selection",
        geography,
    ):
        geography = None
    trl = _extract_section_value(document.content, ("technology readiness level", "trl", "maturity level"))
    required_partners = _extract_section_value(document.content, ("required partners", "partnerships required", "partner requirements"))
    deliverables = _extract_section_value(document.content, ("deliverables", "expected deliverables", "required outputs"))
    evaluation_criteria = _extract_section_value(document.content, ("evaluation criteria", "selection criteria", "review criteria"))
    eligible_costs = _extract_section_value(document.content, ("eligible costs", "allowable costs", "eligible expenses"))

    evidence = {
        "status": _evidence_excerpt(document.content, [
            # These are source excerpts, not automatic acceptance decisions.
            # Cover explicit status fields, directory badges and application windows.
            r"(?:call|application|solicitation|grant|opportunity)\s+status\s*[:：-]\s*(?:open|closed|paused|upcoming|funded)",
            r"applications? (?:are|now) open",
            r"open for applications",
            r"open for proposals",
            r"open for submissions",
            r"currently accepting (?:applications|proposals|submissions)",
            r"accepting (?:new )?(?:applications|proposals|submissions)",
            r"applications? (?:are )?reviewed on a rolling basis",
            r"applications? can be submitted at any time",
            r"applications? (?:are )?accepted on a rolling basis",
            r"(?:grant )?fellows are accepted on a rolling basis",
            r"proposals are accepted on a rolling basis",
            r"open grants? applications?[^.\n]{0,160}\b(?:are\s+)?welcome\b",
            r"\b(?:grant|fellowship|bounty)\s*\|\s*open\s*\|",
            r"request for proposals",
            r"call for proposals",
            r"(?:currently\s+)?not accepting (?:any fresh |new )?(?:applications|submissions)",
            r"no longer accepting (?:applications|submissions)",
            r"applications? (?:are )?(?:closed|paused|suspended)",
            r"deadline[^.\n]{0,180}20\d{2}",
            r"(?:due date|proposal deadline|closing date|applications? due|submit(?:ted)? by|apply by|deadline date)[^.!?\n]{0,180}",
            r"(?:was|has been|have been) awarded[^.\n]{0,160}",
        ]),
        "opening_date": _evidence_excerpt(document.content, [
            r"opens?[^.\n]{0,160}20\d{2}",
            r"applications? open[^.\n]{0,160}20\d{2}",
            r"application window[^.\n]{0,160}20\d{2}",
        ]),
        "deadline": _evidence_excerpt(document.content, [
            r"closes?[^.\n]{0,180}20\d{2}",
            r"deadline[^.\n]{0,180}20\d{2}",
            r"applications? close[^.\n]{0,180}20\d{2}",
            r"(?:due date|proposal deadline|closing date|applications? due|submit(?:ted)? by|apply by|deadline date)[^.!?\n]{0,180}",
        ]),
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

    analysis = FundingOpportunity(
        funding_body=funding_body,
        program_name=program_name,
        call_id=mission_ctx.get("funding_call_id"),
        status=status,
        opening_date=opening_date,
        deadline=deadline,
        funding_amount=amount,
        currency=_extract_currency(amount),
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
    # Reuse a source-grounded problem sentence in the research context. Do not
    # invent a generic challenge merely to fill a missing field.
    problem_intel = extract_funding_problem_intelligence(document, analysis)
    source_problem = problem_intel.get("selected_problem")
    if source_problem:
        mission_ctx["inferred_challenges"] = [source_problem]
        mission_ctx["evidence_refs"] = [document.source]
        mission_ctx["inference_provenance"] = ["source_grounded_problem_extraction"]
        mission_ctx["confidence"] = problem_intel.get("problem_confidence") or "Medium"
        analysis.research_context = mission_ctx
    return analysis

def build_domain_relevance(text: str) -> list[dict]:
    """Return the canonical seven-domain relevance ranking for a funding call.

    Funding relevance is call-level evidence: unlike downstream routing, it is allowed
    to rank multiple domains.  The same weighted scorer is used throughout RIF so
    funding, normalization, UI, and terminal reporting cannot disagree.
    """
    from core.schemas import rank_research_areas

    return rank_research_areas(text)


def extract_funding_problem_intelligence(document: FundingDocument, analysis: FundingOpportunity) -> dict:
    """Extract a concise, source-grounded problem signal instead of copying the page.

    Funding pages often contain navigation, eligibility rules and application
    instructions rather than a concrete technical problem. In that case the
    signal stays unresolved; the source text is not promoted as if it were a
    problem statement. Every selected signal must retain its supporting passage.
    """
    from core.problem_extraction import extract_problem_intelligence

    evidence_snippets = [str(value) for value in (analysis.evidence or {}).values() if str(value).strip()]
    return extract_problem_intelligence(
        content=document.content,
        title=analysis.program_name,
        evidence_bundle={"evidence_snippets": evidence_snippets},
        allow_llm_refinement=False,
        research_area=analysis.focus_area,
        agent_name="funding",
    )


def build_tags(analysis: FundingOpportunity) -> list[str]:
    tags = ["#Funding"]
    if analysis.focus_area in {"RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins"}:
        tags.append("#Blockchain")
    focus_tag = normalize_tag(analysis.focus_area)
    if focus_tag and focus_tag not in tags:
        tags.append(focus_tag)
    for keyword in analysis.keywords:
        tag = normalize_tag(keyword)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def build_markdown(document: FundingDocument, analysis: FundingOpportunity, tags: list[str], run_id: str | None = None) -> str:
    source_value = document.source if document.source_type != "Manual" or document.source.startswith("http") else "Manual"
    keyword_tags = " ".join(normalize_tag(keyword) for keyword in analysis.keywords if normalize_tag(keyword)) or "#funding"
    research_context = analysis.research_context or {}
    inferred_challenges = research_context.get("inferred_challenges") or []
    research_mission = str(inferred_challenges[0]).strip() if inferred_challenges else "Unknown"
    investigation_questions = research_context.get("investigation_questions") or []
    domain_relevance = build_domain_relevance(document.content)
    
    evidence_bundle = ""
    if analysis.evidence:
        evidence_lines = []
        for key, snippet in analysis.evidence.items():
            clean_snippet = snippet.replace('\n', ' ').strip()
            evidence_lines.append(f"        - **{key}**: {clean_snippet}")
        evidence_bundle = "\n".join(evidence_lines)
    else:
        evidence_bundle = "        - No explicit evidence snippets provided."

    run_id_line = f"\n        - Run Id: {run_id}" if run_id else ""

    intel = extract_funding_problem_intelligence(document, analysis)
    selected_problem = intel.get("selected_problem") or "Insufficient problem signal"
    problem_confidence = intel.get("problem_confidence") or "Low"

    return textwrap.dedent(
        f'''\
        # Grant: {analysis.program_name}

        ## Problem Intelligence
        **Selected Problem**: {selected_problem}
        **Confidence**: {problem_confidence}

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
        {chr(10).join(f"        - {q}" for q in investigation_questions) if investigation_questions else "        None"}

        ## Focus Area
        {analysis.focus_area}

        ## Domain Relevance
        {chr(10).join(f"        - {item['rank']}. {item['domain']}: {item['score']:.2f} ({', '.join(item['matched_terms']) or 'no direct evidence'})" for item in domain_relevance)}

        ## Keywords
        {keyword_tags}

        ## Source Details
        Source: {source_value}
        Application URL: {analysis.application_url or "Unknown"}
        Source Type: {document.source_type}
        Retrieved At: {analysis.retrieved_at}

        ## Layer
        Funding

        ## Provenance
        - Source URL: {analysis.source_url}
        - Retrieved At: {analysis.retrieved_at}

        ## Evidence Bundle
        ### Evidence Snippets
{evidence_bundle}

        ## Shared Metadata
        - Agent: funding
        - Title: {analysis.program_name}
        - Source: {source_value}
        - Funding Call Id: {analysis.call_id}
        - Layer: Funding
        - Research Area: {analysis.focus_area}{run_id_line}

        ## Agent-Specific Body
        - Funding Body: {analysis.funding_body}
        - Opportunity Type: {analysis.opportunity_type}
        - Funding Amount: {analysis.funding_amount or "Unknown"}
        - Evaluation Criteria: {analysis.evaluation_criteria or "Unknown"}

        ## Raw Content
        {document.content.strip()}

        ## Tags
        {' '.join(tags)}
        '''
    ).strip() + "\n"


def save_markdown_output(document: FundingDocument, analysis: FundingOpportunity, output_dir: Path, run_id: str | None = None) -> Path:
    import hashlib
    org_dir = output_dir / slugify(document.organization)
    org_dir.mkdir(parents=True, exist_ok=True)
    
    base_slug = slugify(analysis.program_name)
    if analysis.call_id:
        unique_suffix = slugify(analysis.call_id)
    else:
        url_hash = hashlib.sha256(document.source.encode('utf-8')).hexdigest()[:8]
        unique_suffix = url_hash
        
    filename = f"{base_slug}-{unique_suffix}.md"
    output_path = org_dir / filename
    tags = build_tags(analysis)
    output_path.write_text(build_markdown(document, analysis, tags, run_id=run_id), encoding="utf-8")
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


def process_documents(documents_with_analysis: list[tuple[FundingDocument, FundingOpportunity]], output_dir: Path, db_path: Path, run_id: str | None = None) -> int:
    connection = db_connect(db_path)
    init_funding_db(connection)
    created = 0
    refreshed = 0
    for document, analysis in documents_with_analysis:
        already_stored = funding_exists(connection, document.title, document.source)
        if already_stored:
            LOGGER.info("Refreshing stored funding item '%s' from %s", document.title, document.source)
        output_path = save_markdown_output(document, analysis, output_dir, run_id=run_id)
        # save_funding_record uses ON CONFLICT(url) DO UPDATE. Always call it for
        # an observed source URL so status, deadline and eligibility cannot remain
        # stale merely because the URL was seen in an earlier scan.
        save_funding_record(
            connection,
            opportunity=analysis,
            path=str(output_path),
            source_type=document.source_type,
            year=document.year
        )
        if already_stored:
            refreshed += 1
        else:
            created += 1
    connection.commit()
    write_insights(connection, output_dir)
    LOGGER.info("Funding persistence: %d new records, %d refreshed records", created, refreshed)
    return created


def _format_collection_error(error: Any) -> str:
    """Format one source-collection error for actionable CLI diagnostics."""
    if isinstance(error, dict):
        operation = str(error.get("operation") or "collection")
        url = str(error.get("url") or "")
        reason = str(error.get("error") or error.get("reason") or "Unknown error")
        parts = [operation]
        if url:
            parts.append(url)
        if reason:
            parts.append(reason)
        message = " | ".join(parts)
    else:
        message = str(error)
    return message if len(message) <= 600 else message[:597] + "..."


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
                    f"{'Source':<35} | {'Method':<6} | {'Pages':<5} | {'PgFail':<6} | "
                    f"{'Detail':<6} | {'Docs':<4} | {'DocFail':<7} | {'Disc':<5} | "
                    f"{'Valid':<5} | {'RecFail':<7} | {'Filt':<5} | {'PagComplete':<11} | "
                    f"{'Trunc':<5} | {'Errs':<4} | {'Status':<25}"
                )
                print(header)
                print("-" * len(header))
                collection_results = response["collection_results"]
                for res in collection_results:
                    pag_str = str(res['pagination_complete']) if res.get('pagination_detected', True) else "N/A"
                    errors = res.get("errors") or []
                    print(
                        f"{res['source_id'][:35]:<35} | {res['method']:<6} | "
                        f"{res.get('pages_fetched', 0):<5} | {res.get('pages_failed', 0):<6} | "
                        f"{res.get('detail_pages_fetched', 0):<6} | "
                        f"{res.get('documents_fetched', 0):<4} | {res.get('documents_failed', 0):<7} | "
                        f"{res.get('records_discovered', 0):<5} | {res.get('records_valid', 0):<5} | "
                        f"{res.get('records_failed', 0):<7} | {res.get('records_filtered', 0):<5} | {pag_str:<11} | "
                        f"{str(res.get('truncated', False)):<5} | {len(errors):<4} | "
                        f"{res.get('final_status', 'UNKNOWN'):<25}"
                    )
                for res in collection_results:
                    errors = res.get("errors") or []
                    if not errors:
                        continue
                    print(f"\nCollection errors — {res['source_id']} ({len(errors)}):")
                    for index, error in enumerate(errors[:5], start=1):
                        print(f"  {index}. {_format_collection_error(error)}")
                    if len(errors) > 5:
                        print(f"  ... {len(errors) - 5} additional error(s) omitted")

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


def _funding_sources_fingerprint(
    sources: list[FundingSource],
    *,
    area: str | None = None,
    is_test_mode: bool = False,
) -> str:
    """Fingerprint the source configuration and collection mode for checkpoint safety."""
    manifest = {
        "area": area,
        "is_test_mode": bool(is_test_mode),
        "sources": [
            {
                "name": source.name,
                "url": source.url,
                "tier": source.tier,
                "focus": source.focus,
                "collection": source.collection,
                "research_domains": source.research_domains,
                "direct_page": source.direct_page,
            }
            for source in sources
        ],
    }
    serialized = json.dumps(
        manifest,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _funding_url_key(value: str | None) -> str:
    """Normalize a source URL for matching explicitly configured companion pages."""
    parsed = urlparse(str(value or ""))
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{parsed.path.rstrip('/')}"


def _merge_related_eligibility_context(
    documents: list[tuple[FundingDocument, FundingOpportunity]],
    sources: list[Any],
) -> tuple[set[str], list[str]]:
    """Merge explicitly configured official companion-page eligibility into a call.

    Relationships are declared in source configuration rather than inferred from
    title similarity. Supplemental evidence is retained with its source URL, and a
    missing companion page is marked unresolved for the audit instead of guessed.
    """
    documents_by_url = {
        _funding_url_key(document.source): (document, analysis)
        for document, analysis in documents
    }
    supplemental_context_urls: set[str] = set()
    warnings: list[str] = []

    for source in sources:
        collection = getattr(source, "collection", {}) or {}
        relations = collection.get("related_context_pages", []) or []
        for relation in relations:
            fields = relation.get("fields", ["eligibility"]) or ["eligibility"]
            if "eligibility" not in fields:
                continue

            primary_key = _funding_url_key(relation.get("primary_url"))
            context_key = _funding_url_key(relation.get("context_url"))
            primary_entry = documents_by_url.get(primary_key)
            context_entry = documents_by_url.get(context_key)
            if not primary_entry:
                continue

            primary_document, primary_analysis = primary_entry
            primary_analysis.evidence = dict(primary_analysis.evidence or {})
            if not context_entry:
                primary_analysis.evidence["eligibility_supplemental_context_status"] = "missing"
                warnings.append(
                    "Official eligibility companion page was not fetched for "
                    f"{primary_document.source}: {relation.get('context_url')}"
                )
                continue

            context_document, _context_analysis = context_entry
            supplemental_context_urls.add(context_key)
            supplemental_eligibility = _extract_eligibility_from_prose(context_document.content)
            if not supplemental_eligibility:
                primary_analysis.evidence["eligibility_supplemental_context_status"] = "unresolved"
                primary_analysis.evidence["eligibility_supplemental_source_url"] = context_document.source
                warnings.append(
                    "Official eligibility companion page was fetched but no explicit eligibility "
                    f"criteria were extracted: {context_document.source}"
                )
                continue

            eligibility_values = [
                cleaned_text(value)
                for value in (primary_analysis.eligibility, supplemental_eligibility)
                if cleaned_text(value)
            ]
            merged_values: list[str] = []
            for value in eligibility_values:
                if value.casefold() not in {existing.casefold() for existing in merged_values}:
                    merged_values.append(value)

            primary_analysis.eligibility = " ".join(merged_values) or None
            primary_analysis.evidence["eligibility_supplemental_context_status"] = "verified"
            primary_analysis.evidence["eligibility_supplemental_source_url"] = context_document.source
            primary_analysis.evidence["eligibility_supplemental_text"] = supplemental_eligibility

    return supplemental_context_urls, warnings


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
    started_at = datetime.now(UTC)
    # Every independent invocation gets a distinct checkpoint identity. A
    # supplied run_id stays stable across retries of that same pipeline run.
    run_id = str(payload.get("run_id") or started_at.strftime("%Y%m%dT%H%M%S%fZ"))
    analysis_mode = str(payload.get("analysis_mode", "collect_only")).strip().lower()
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
    source_domain_map: dict[str, list[str]] = {}
    
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
                source_domain_map = {
                    source.name: list(source.research_domains)
                    for source in sources
                }
                if payload.get("require_domain_mappings", False):
                    unmapped_sources = sorted(
                        source_name for source_name, domains in source_domain_map.items()
                        if not domains
                    )
                    if unmapped_sources:
                        raise FundingAgentError(
                            "Funding sources missing canonical research-domain mappings: "
                            + ", ".join(unmapped_sources)
                        )
                is_test_mode = bool(payload.get("is_test_mode", False))
                source_fingerprint = _funding_sources_fingerprint(
                    sources,
                    area=area,
                    is_test_mode=is_test_mode,
                )
                collection_iterator = collect_auto_sources(
                    sources,
                    area,
                    warnings=extra_warnings,
                    is_test_mode=payload.get("is_test_mode", False),
                )

                collection_results = []
                documents = []

                import pickle
                checkpoint_override = payload.get("checkpoint_path")
                checkpoint_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", run_id).strip("._") or "run"
                checkpoint_path = (
                    Path(checkpoint_override)
                    if checkpoint_override
                    else Path("scratch") / f"funding_checkpoint_{checkpoint_key}.pkl"
                )
                completed_sources = set()
                if checkpoint_path.exists():
                    try:
                        ckpt = pickle.loads(checkpoint_path.read_bytes())
                        checkpoint_matches = (
                            isinstance(ckpt, dict)
                            and ckpt.get("run_id") == run_id
                            and ckpt.get("source_fingerprint") == source_fingerprint
                        )
                        if not checkpoint_matches:
                            LOGGER.info(
                                "Ignoring stale or incompatible funding checkpoint: %s",
                                checkpoint_path,
                            )
                        else:
                            completed_sources = set(ckpt.get("completed_sources", set()))
                            collection_results = list(ckpt.get("collection_results", []))
                            documents = list(ckpt.get("documents", []))
                            LOGGER.info(
                                "Resuming funding checkpoint with %d completed sources and %d documents.",
                                len(completed_sources),
                                len(documents),
                            )

                            # A checkpoint is resumable only for the same run
                            # and exactly the same source configuration.
                            sources = [s for s in sources if s.name not in completed_sources]
                            collection_iterator = collect_auto_sources(
                                sources,
                                area,
                                warnings=extra_warnings,
                                is_test_mode=payload.get("is_test_mode", False),
                            )
                    except Exception as e:
                        LOGGER.warning("Failed to load funding checkpoint %s: %s", checkpoint_path, e)
                        completed_sources = set()

                # Process and save progressively to avoid data loss on crash
                for res in collection_iterator:
                    _normalize_collection_coverage_status(res)
                    _update_source_verification_flags(res)
                    collection_results.append(res)
                    source_docs = []
                    for doc, analysis in res.opportunities:
                        documents.append((doc, analysis))
                        source_docs.append((doc, analysis))
                    if analysis_mode == "analyze" and source_docs:
                        process_documents(source_docs, output_dir, db_path, run_id=run_id)
                        
                    # Save checkpoint after each source
                    completed_sources.add(res.source_id)
                    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                    checkpoint_path.write_bytes(pickle.dumps({
                        "run_id": run_id,
                        "source_fingerprint": source_fingerprint,
                        "completed_sources": completed_sources,
                        "collection_results": collection_results,
                        "documents": documents,
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
                process_documents([(doc, analysis)], output_dir, db_path, run_id=run_id)
        else:
            text = payload["text"]
            organization = payload.get("organization") or "Manual Organization"
            doc = build_manual_text_document(text, organization, payload.get("title"), payload.get("year"))
            analysis = analyze_document(doc)
            documents = [(doc, analysis)]
            if analysis_mode == "analyze":
                process_documents([(doc, analysis)], output_dir, db_path, run_id=run_id)
            
        supplemental_context_urls: set[str] = set()
        if mode == "configured_scan":
            configured_sources = load_sources(Path(payload.get("sources_path", DEFAULT_SOURCES_PATH)))
            supplemental_context_urls, context_warnings = _merge_related_eligibility_context(
                documents,
                configured_sources,
            )
            extra_warnings.extend(context_warnings)

        outputs = []
        docs_only = []
        companion_context_skipped_count = 0
        generic_title_skipped_count = 0
        for doc, analysis in documents:
            # Companion pages are merged into their configured primary call above;
            # do not emit them as duplicate independent opportunities.
            if _funding_url_key(doc.source) in supplemental_context_urls:
                companion_context_skipped_count += 1
                continue
            # Do not emit navigation/portal chrome as a funding opportunity.
            # Keep the record out of structured output as well as downstream
            # qualification; generic labels such as "Topic | SBIR" are not calls.
            if _is_generic_funding_title(analysis.program_name or doc.title):
                generic_title_skipped_count += 1
                extra_warnings.append(
                    f"Skipped generic funding-page title: {analysis.program_name or doc.title!r} ({doc.source})"
                )
                continue
            docs_only.append(doc)
            output_path = None
            try:
                output_path = save_markdown_output(doc, analysis, output_dir, run_id=run_id)
            except Exception as e:
                LOGGER.warning("Could not save funding markdown output: %s", e)

            evidence = {"source_url": analysis.source_url, "source_title": analysis.source_title, **analysis.evidence}
            domains = build_domain_relevance(f"{doc.title}\n{doc.content}")
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
                "currency": _extract_currency(analysis.funding_amount),
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
                **(
                    {"source_research_domains": list(source_domain_map[doc.organization])}
                    if doc.organization in source_domain_map else {}
                ),
                "primary_domain": analysis.focus_area if analysis.focus_area in FOCUS_AREAS else None,
                "secondary_domains": [item["domain"] for item in domains[1:3] if float(item.get("score", 0.0) or 0.0) > 0.0] if len(domains) > 1 else [],
                "required_partners": analysis.required_partners,
                "deliverables": analysis.deliverables,
                "evaluation_criteria": analysis.evaluation_criteria,
                "research_context": analysis.research_context,
                "source_url": analysis.source_url,
                "source_title": analysis.source_title,
                "published_at": analysis.published_at,
                # Preserve provenance timestamps in the structured response.
                # created_at remains as a backwards-compatible alias for retrieved_at.
                "retrieved_at": analysis.retrieved_at,
                "last_verified_at": analysis.last_verified_at,
                "created_at": analysis.retrieved_at,
                "problem": (problem_intel := extract_funding_problem_intelligence(doc, analysis)).get("selected_problem"),
                "problem_signal": problem_intel.get("selected_problem"),
                "problem_type": problem_intel.get("problem_type"),
                "problem_signal_status": "resolved" if problem_intel.get("selected_problem") else "unresolved",
                "problem_confidence": problem_intel.get("problem_confidence"),
                "problem_evidence": problem_intel.get("problem_evidence") or [],
                "markdown_path": str(output_path.resolve()) if output_path else None,
                "evidence": {k: v for k, v in evidence.items() if v},
                "provenance_type": "observed_source_with_deterministic_extraction",
            })
                
        response = {
            "agent": agent_name,
            "layer": layer,
            "mode": mode,
            "area": area,
            "status": "success" if outputs else "partial_success",
            "outputs": outputs,
            "errors": [],
            "warnings": extra_warnings,
            "items_processed": len(documents),
            "items_saved": len(outputs),
            "metadata": {
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
                "duration_seconds": (datetime.now(UTC) - started_at).total_seconds()
            }
        }
        if mode == "configured_scan":
            # Recompute flags for resumed checkpoint entries as well as sources
            # collected in this invocation; defaults are never audit evidence.
            for source_result in collection_results:
                _normalize_collection_coverage_status(source_result)
                _update_source_verification_flags(source_result)

            source_records_discovered_total = sum(int(getattr(item, "records_discovered", 0) or 0) for item in collection_results)
            source_records_valid_total = sum(int(getattr(item, "records_valid", 0) or 0) for item in collection_results)
            source_records_failed_total = sum(int(getattr(item, "records_failed", 0) or 0) for item in collection_results)
            source_records_filtered_total = sum(int(getattr(item, "records_filtered", 0) or 0) for item in collection_results)
            source_duplicates_removed_total = sum(int(getattr(item, "duplicates_removed", 0) or 0) for item in collection_results)
            source_documents_processed = len(documents)
            records_emitted = len(outputs)
            unaccounted_source_to_documents_delta = source_records_valid_total - source_documents_processed
            unaccounted_documents_to_outputs_delta = (
                source_documents_processed
                - companion_context_skipped_count
                - generic_title_skipped_count
                - records_emitted
            )
            response["record_accounting"] = {
                "source_records_discovered_total": source_records_discovered_total,
                "source_records_valid_total": source_records_valid_total,
                "source_records_failed_total": source_records_failed_total,
                "source_records_filtered_total": source_records_filtered_total,
                "source_duplicates_removed_total": source_duplicates_removed_total,
                "agent_documents_processed": source_documents_processed,
                "companion_context_documents_merged": companion_context_skipped_count,
                "generic_title_records_skipped": generic_title_skipped_count,
                "raw_records_emitted": records_emitted,
                "unaccounted_source_to_documents_delta": unaccounted_source_to_documents_delta,
                "unaccounted_documents_to_outputs_delta": unaccounted_documents_to_outputs_delta,
                "reconciled": (
                    unaccounted_source_to_documents_delta == 0
                    and unaccounted_documents_to_outputs_delta == 0
                ),
            }
            response["collection_results"] = [
                {
                    "source_id": res.source_id,
                    "method": res.method,
                    "api_pages_fetched": res.api_pages_fetched,
                    "api_records_received": res.api_records_received,
                    "api_expected_records": res.api_expected_records,
                    "pages_discovered": res.pages_discovered,
                    "pages_fetched": res.pages_fetched,
                    "pages_failed": res.pages_failed,
                    "detail_pages_discovered": res.detail_pages_discovered,
                    "detail_pages_fetched": res.detail_pages_fetched,
                    "detail_pages_failed": res.detail_pages_failed,
                    "documents_discovered": res.documents_discovered,
                    "documents_fetched": res.documents_fetched,
                    "documents_failed": res.documents_failed,
                    "api_pages_fetched": res.api_pages_fetched,
                    "api_records_received": res.api_records_received,
                    "api_expected_records": res.api_expected_records,
                    "records_discovered": res.records_discovered,
                    "records_valid": res.records_valid,
                    "records_failed": res.records_failed,
                    "records_filtered": res.records_filtered,
                    "records_filtered_by_reason": res.records_filtered_by_reason,
                    "duplicates_removed": res.duplicates_removed,
                    "urls_skipped": res.urls_skipped,
                    "relevant_urls_skipped": res.relevant_urls_skipped,
                    "coverage_gaps": res.coverage_gaps,
                    "failed_urls": res.failed_urls,
                    "evidence_verified": res.evidence_verified,
                    "currentness_verified": res.currentness_verified,
                    "no_active_opportunities_confirmed": res.no_active_opportunities_confirmed,
                    "pagination_detected": res.pagination_detected,
                    "pagination_complete": res.pagination_complete,
                    "discovery_exhausted": res.discovery_exhausted,
                    "truncated": res.truncated,
                    "truncation_details": res.truncation_details,
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
