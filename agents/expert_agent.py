from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import textwrap
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from core.funding_selection import FundingCallContext
from core.funding_context import build_agent_specific_funding_context, current_funding_prompt_context, current_funding_queries, generate_funding_queries
from urllib.parse import urlparse

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.db import db_connect
from agents.utils import cleaned_text
from core.focused_extraction import build_focused_content, extract_text_blocks
from core.source_registry import apply_refresh_policy
from core.llm_provider import generate as llm_generate
from core.schemas import AREA_KEYWORDS
from core.funding_context import funding_context_relevance
from core.web_collection import collect_source, combine_pages

DEFAULT_OUTPUT_DIR = Path("outputs/experts")
DEFAULT_DB_PATH = DEFAULT_OUTPUT_DIR / "expert_memory.db"
DEFAULT_SOURCES_PATH = Path("sources/expert_sources.yaml")

RELEVANCE_KEYWORDS = [
    "faculty",
    "professor",
    "research",
    "lab",
    "publications",
    "recent work",
    "projects",
    "interests",
    "papers",
    "blockchain",
    "cryptography",
    "privacy",
    "security",
    "identity",
    "zero-knowledge",
    "distributed systems",
    "payments",
    "sustainability",
]
EXPERT_SIGNAL_KEYWORDS = [
    "research interests",
    "publications",
    "selected publications",
    "recent publications",
    "faculty",
    "people",
    "team",
    "projects",
    "awards",
    "talks",
    "expertise",
    "collaboration",
]
FOCUSED_CONTENT_KEYWORDS = [
    "research interests",
    "selected publications",
    "recent publications",
    "projects",
    "faculty",
    "people",
    "expertise",
    "publications",
    "collaboration",
    "workshop",
    "security",
    "privacy",
    "blockchain",
    "identity",
    "zero-knowledge",
]
ROLE_KEYWORDS = {
    "Faculty": ["professor", "associate professor", "assistant professor", "faculty"],
    "Research Scientist": ["research scientist", "principal research scientist", "scientist"],
    "Lab Lead": ["lab lead", "director", "chair", "head"],
    "Postdoc": ["postdoctoral", "postdoc"],
    "Doctoral Researcher": ["phd", "doctoral", "research scholar"],
}
SOURCE_TYPE_MAP = {
    "faculty_page": "Faculty Page",
    "lab_page": "Lab Page",
    "research_center": "Research Center",
    "project_team": "Project Team",
    "api_directory": "API Directory",
    "profile_directory_manual": "Profile Directory",
}
EVIDENCE_TYPE_MAP = {
    "faculty_page": "Expert Profile",
    "lab_page": "Lab Member Listing",
    "research_center": "Research Center People Page",
    "project_team": "Project Team Listing",
    "api_directory": "Author Directory Result",
    "profile_directory_manual": "Profile Directory",
}
API_SEARCH_TERMS = {
    "RWA": ["real world assets blockchain", "tokenization compliance"],
    "ESG": ["carbon tokenization", "MRV blockchain"],
    "ZK-IoV": ["zero knowledge vehicular networks", "privacy preserving mobility"],
    "DID": ["decentralized identity verifiable credentials"],
    "DePIN": ["decentralized physical infrastructure", "wireless storage blockchain"],
    "MEV": ["MEV block builder", "transaction ordering blockchain"],
    "Stablecoins": ["stablecoins payment settlement", "stablecoin reserves"],
}
LOGGER = logging.getLogger("rif.expert_agent")


@dataclass(slots=True)
class ExpertSource:
    name: str
    type: str
    url: str
    focus: str
    auto_collect: bool = True
    query_template: str = ""


@dataclass(slots=True)
class ExpertDocument:
    title: str
    source_type: str
    evidence_type: str
    expert_name_hint: str
    affiliation_hint: str
    role_hint: str
    area_hint: str
    focus: str
    url: str
    content: str
    source_mode: str


@dataclass(slots=True)
class ExpertAnalysis:
    title: str
    source_type: str
    evidence_type: str
    expert_name: str
    affiliation: str
    role: str
    area: str
    expertise_themes: str
    recent_work: str
    key_projects_or_papers: str
    collaboration_relevance: str
    proposal_relevance: str
    expert_signature: str
    confidence: str
    keywords: list[str]
    recent_publication_signal: str
    collaboration_fit_score: float
    relevant_papers_count: int
    source_method: str


class ExpertAgentError(Exception):
    """Raised when the expert agent cannot complete a task."""


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "item"


def normalize_tag(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return f"#{text}" if text else ""


def load_requests_and_bs4() -> tuple[Any, Any]:
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise ExpertAgentError(
            "Automatic and URL modes require 'requests' and 'beautifulsoup4'. Install them before running those modes."
        ) from exc
    return requests, BeautifulSoup


def init_expert_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS expert_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expert_name TEXT NOT NULL,
            affiliation TEXT NOT NULL,
            role TEXT NOT NULL,
            area TEXT NOT NULL,
            url TEXT NOT NULL,
            expertise_themes TEXT NOT NULL,
            recent_work TEXT NOT NULL,
            key_projects_or_papers TEXT NOT NULL,
            collaboration_relevance TEXT NOT NULL,
            proposal_relevance TEXT NOT NULL,
            expert_signature TEXT NOT NULL,
            confidence TEXT NOT NULL,
            keywords TEXT NOT NULL,
            recent_publication_signal TEXT NOT NULL DEFAULT '',
            collaboration_fit_score REAL NOT NULL DEFAULT 0,
            relevant_papers_count INTEGER NOT NULL DEFAULT 0,
            path TEXT NOT NULL DEFAULT '',
            last_checked TEXT NOT NULL
        )
        """
    )
    for column, definition in (("recent_publication_signal", "TEXT NOT NULL DEFAULT ''"), ("collaboration_fit_score", "REAL NOT NULL DEFAULT 0"), ("relevant_papers_count", "INTEGER NOT NULL DEFAULT 0")):
        try:
            connection.execute(f"ALTER TABLE expert_data ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            pass
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_expert_url ON expert_data(url)")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_expert_identity ON expert_data(expert_name, affiliation, expert_signature)"
    )
    connection.commit()


def expert_exists(connection: sqlite3.Connection, expert_name: str, affiliation: str, url: str, expert_signature: str | None = None) -> bool:
    if expert_signature:
        row = connection.execute(
            """
            SELECT 1 FROM expert_data
            WHERE url = ? OR (expert_name = ? AND affiliation = ?) OR (expert_name = ? AND expert_signature = ?)
            """,
            (url, expert_name, affiliation, expert_name, expert_signature),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT 1 FROM expert_data WHERE url = ? OR (expert_name = ? AND affiliation = ?)",
            (url, expert_name, affiliation),
        ).fetchone()
    return row is not None


def save_expert_record(connection: sqlite3.Connection, document: ExpertDocument, analysis: ExpertAnalysis, output_path: Path) -> None:
    connection.execute(
        """
        INSERT INTO expert_data (
            expert_name,
            affiliation,
            role,
            area,
            url,
            expertise_themes,
            recent_work,
            key_projects_or_papers,
            collaboration_relevance,
            proposal_relevance,
            expert_signature,
            confidence,
            keywords,
            recent_publication_signal,
            collaboration_fit_score,
            relevant_papers_count,
            path,
            last_checked
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
                expert_name=excluded.expert_name,
                affiliation=excluded.affiliation,
                role=excluded.role,
                area=excluded.area,
                expertise_themes=excluded.expertise_themes,
                recent_work=excluded.recent_work,
                key_projects_or_papers=excluded.key_projects_or_papers,
                collaboration_relevance=excluded.collaboration_relevance,
                proposal_relevance=excluded.proposal_relevance,
                expert_signature=excluded.expert_signature,
                confidence=excluded.confidence,
                keywords=excluded.keywords,
                recent_publication_signal=excluded.recent_publication_signal,
                collaboration_fit_score=excluded.collaboration_fit_score,
                relevant_papers_count=excluded.relevant_papers_count,
                path=excluded.path,
                last_checked=excluded.last_checked
        """,
        (
            analysis.expert_name,
            analysis.affiliation,
            analysis.role,
            analysis.area,
            document.url,
            analysis.expertise_themes,
            analysis.recent_work,
            analysis.key_projects_or_papers,
            analysis.collaboration_relevance,
            analysis.proposal_relevance,
            analysis.expert_signature,
            analysis.confidence,
            json.dumps(analysis.keywords),
            analysis.recent_publication_signal,
            analysis.collaboration_fit_score,
            analysis.relevant_papers_count,
            str(output_path),
            datetime.now(UTC).isoformat(),
        ),
    )
    connection.commit()


def load_sources(sources_path: Path) -> list[ExpertSource]:
    if not sources_path.exists():
        raise ExpertAgentError(f"Expert sources file not found: {sources_path}")
    try:
        data = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ExpertAgentError(f"Invalid YAML in {sources_path}: {exc}") from exc
    root = data.get("expert_sources", {})
    sources: list[ExpertSource] = []
    for _, entries in root.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = cleaned_text(str(entry.get("name", "")))
            source_type = cleaned_text(str(entry.get("type", ""))) or "faculty_page"
            url = cleaned_text(str(entry.get("url", "")))
            focus = cleaned_text(str(entry.get("focus", "")))
            auto_collect = bool(entry.get("auto_collect", True))
            query_template = cleaned_text(str(entry.get("query_template", "")))
            if name and url:
                sources.append(
                    ExpertSource(
                        name=name,
                        type=source_type,
                        url=url,
                        focus=focus,
                        auto_collect=auto_collect,
                        query_template=query_template,
                    )
                )
    if not sources:
        raise ExpertAgentError("No expert sources were loaded from sources/expert_sources.yaml.")
    return apply_refresh_policy("expert", sources)


def fetch_html(url: str) -> str:
    from core.shared_crawl_manager import SharedCrawlManager
    try:
        res = SharedCrawlManager.get().fetch(url)
        if res.status_code and res.status_code >= 400:
            LOGGER.warning("HTTP %s for %s", res.status_code, url)
            raise ExpertAgentError(f"Unable to fetch page {url}: HTTP {res.status_code}")
        LOGGER.debug("Fetched %s via %s", url, res.fetch_method)
        return res.html
    except ExpertAgentError:
        raise
    except Exception as exc:
        raise ExpertAgentError(f"Unable to fetch page {url}: {exc}") from exc

def extract_page_text(html: str) -> tuple[str, str]:
    _, BeautifulSoup = load_requests_and_bs4()
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "footer", "nav", "header"]):
        tag.decompose()
    title_node = soup.find("h1") or soup.title
    title = cleaned_text(title_node.get_text(" ", strip=True)) if title_node else "Untitled"
    blocks = extract_text_blocks(soup)
    content = cleaned_text(" ".join(blocks))
    focused_content = build_focused_content(
        title=title,
        blocks=blocks,
        focus_keywords=FOCUSED_CONTENT_KEYWORDS + EXPERT_SIGNAL_KEYWORDS,
        area_keywords=[keyword for keywords in AREA_KEYWORDS.values() for keyword in keywords],
        min_score=3,
        max_chars=8000,
    )
    return title, (focused_content or content)[:9000]


def fetch_json_text(url: str) -> str:
    requests, _ = load_requests_and_bs4()
    headers = {"User-Agent": "RIF-Expert-Agent/1.0", "Accept": "application/json"}
    try:
        from core.http_client import get_response
        response = get_response(url, headers=headers, timeout=(8.0, 20.0), retries=1)
    except Exception as exc:
        raise ExpertAgentError(f"Unable to fetch API source {url}: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        return cleaned_text(response.text)[:9000]
    return cleaned_text(json.dumps(payload, ensure_ascii=False))[:9000]


def infer_area(text: str, default: str = "Unscoped") -> str:
    lowered = text.lower()
    if default in AREA_KEYWORDS and any(keyword in lowered for keyword in AREA_KEYWORDS[default]):
        return default
    for area, keywords in AREA_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return area
    return default


def infer_role(text: str) -> str:
    lowered = text.lower()
    for role, keywords in ROLE_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return role
    return "Expert"


def infer_source_type(raw_type: str) -> str:
    return SOURCE_TYPE_MAP.get(raw_type, "Expert Profile")


def infer_evidence_type(raw_type: str) -> str:
    return EVIDENCE_TYPE_MAP.get(raw_type, "Expert Profile")


def infer_affiliation(text: str, url: str) -> str:
    lowered = text.lower()
    known_affiliations = [
        "indian institute of science",
        "iisc",
        "iit bombay",
        "iit madras",
        "iit kanpur",
        "iit hyderabad",
        "iit delhi",
        "iit kharagpur",
        "iit roorkee",
        "iit guwahati",
    ]
    for item in known_affiliations:
        if item in lowered:
            return item.upper() if item == "iisc" else item.title()
    host = urlparse(url).netloc.replace("www.", "")
    return host or "Unknown"


def infer_expert_name(title: str, text: str) -> str:
    candidate = cleaned_text(title)
    if candidate and len(candidate.split()) <= 8 and not any(word in candidate.lower() for word in ("faculty", "people", "team", "lab", "center", "centre")):
        return candidate
    match = re.search(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b", text)
    if match:
        return cleaned_text(match.group(1))
    return candidate or "Unknown Expert"


def is_relevant(text: str, area_hint: str) -> bool:
    lowered = text.lower()
    normalized_area = area_hint if area_hint in AREA_KEYWORDS else None
    if not normalized_area:
        # In funding-driven unscoped mode, use the selected call's live mission
        # rather than forcing the source into one of the legacy static domains.
        return funding_context_relevance(text)
    # A configured collection must be relevant to the requested domain. Do not
    # accept a match from some other static RIF domain.
    return any(keyword in lowered for keyword in AREA_KEYWORDS[normalized_area])


def build_api_documents(source: ExpertSource, area: str | None) -> list[ExpertDocument]:
    if not source.query_template:
        return []
    terms = API_SEARCH_TERMS.get(area, []) if area else []
    documents: list[ExpertDocument] = []
    for term in terms[:2]:
        query_url = source.query_template.replace("{query}", term.replace(" ", "%20"))
        LOGGER.info("Collecting expert intelligence from API %s with query '%s'", source.name, term)
        try:
            content = fetch_json_text(query_url)
        except ExpertAgentError as exc:
            LOGGER.warning("Skipping API source %s query '%s': %s", source.name, term, exc)
            continue
        title = f"{source.name}: {term}"
        full_text = cleaned_text(f"{source.name} {source.focus} {title} {content}")
        area_hint = area or infer_area(full_text, "Unscoped")
        if not is_relevant(full_text, area_hint):
            continue
        documents.append(
            ExpertDocument(
                title=title,
                source_type=infer_source_type(source.type),
                evidence_type=infer_evidence_type(source.type),
                expert_name_hint=infer_expert_name(title, full_text),
                affiliation_hint=infer_affiliation(full_text, query_url),
                role_hint="Expert",
                area_hint=area_hint,
                focus=source.focus,
                url=query_url,
                content=full_text,
                source_mode="Automatic",
            )
        )
    return documents


def build_auto_documents(sources_path: Path, area: str | None = None) -> list[ExpertDocument]:
    import yaml
    raw_sources = yaml.safe_load(sources_path.read_text(encoding="utf-8")) if sources_path.exists() else {}
    from core.source_registry import extract_yaml_limits
    limits_by_url = extract_yaml_limits(raw_sources)
    from core.crawl_context import current_crawl_context
    documents: list[ExpertDocument] = []
    for source in load_sources(sources_path):
        if not source.auto_collect:
            continue
        if source.type == "api_directory":
            documents.extend(build_api_documents(source, area))
            continue
        LOGGER.info("Collecting expert intelligence from %s", source.name)
        try:
            limits = limits_by_url.get(source.url, {})

            ctx = current_crawl_context.get(None)

            if ctx: ctx.begin_source()

            result = collect_source(source.url, keywords=("researcher", "faculty", "professor", "lab", "publication", "profile", "papers"), max_depth=limits.get('max_depth', 2), max_pages=limits.get('max_pages', 8), max_documents=limits.get('max_documents'), max_seconds=limits.get('max_seconds'))
        except Exception as exc:
            LOGGER.warning("Skipping source %s: %s", source.name, exc)
            continue
        content = combine_pages(result, prefix=f"{source.name} {source.focus}")
        title = result.pages[0].title if result.pages else source.name
        full_text = cleaned_text(content)
        area_hint = area or infer_area(full_text, "Unscoped")
        if not is_relevant(full_text, area_hint):
            continue
        documents.append(ExpertDocument(title=title or source.name, source_type=infer_source_type(source.type), evidence_type=infer_evidence_type(source.type), expert_name_hint=infer_expert_name(title or source.name, full_text), affiliation_hint=infer_affiliation(full_text, source.url), role_hint=infer_role(full_text), area_hint=area_hint, focus=source.focus, url=source.url, content=full_text, source_mode="Automatic"))
    return documents


def build_manual_url_document(
    url: str,
    title: str | None,
    source_type: str | None,
    evidence_type: str | None,
    role: str | None,
    area: str | None,
    focus: str | None,
) -> ExpertDocument:
    html = fetch_html(url)
    page_title, content = extract_page_text(html)
    resolved_title = title or page_title or cleaned_text(urlparse(url).netloc)
    full_text = cleaned_text(f"{resolved_title} {focus or ''} {content}")
    area_hint = area or infer_area(full_text, "Unscoped")
    if not is_relevant(full_text, area_hint):
        raise ExpertAgentError("Manual URL content does not map to the selected research areas or expert signals.")
    return ExpertDocument(
        title=resolved_title,
        source_type=source_type or "Expert Profile",
        evidence_type=evidence_type or "Expert Profile",
        expert_name_hint=infer_expert_name(resolved_title, full_text),
        affiliation_hint=infer_affiliation(full_text, url),
        role_hint=role or infer_role(full_text),
        area_hint=area_hint,
        focus=focus or page_title,
        url=url,
        content=full_text,
        source_mode="Manual",
    )


def build_manual_text_document(
    text: str,
    title: str,
    source_type: str | None,
    evidence_type: str | None,
    role: str | None,
    area: str | None,
    focus: str | None,
    source: str | None = None,
) -> ExpertDocument:
    content = cleaned_text(text)
    area_hint = area or infer_area(cleaned_text(f"{title} {focus or ''} {content}"), "Unscoped")
    if not is_relevant(cleaned_text(f"{area_hint} {focus or ''} {content}"), area_hint):
        raise ExpertAgentError("Manual text does not map to the selected research areas or expert signals.")
    source_url = source or f"manual://{slugify(title)}/{slugify(area_hint)}"
    return ExpertDocument(
        title=title,
        source_type=source_type or "Manual",
        evidence_type=evidence_type or "Expert Profile",
        expert_name_hint=infer_expert_name(title, content),
        affiliation_hint=infer_affiliation(content, source_url),
        role_hint=role or infer_role(content),
        area_hint=area_hint,
        focus=focus or area_hint,
        url=source_url,
        content=content,
        source_mode="Manual",
    )


def build_analysis_prompt(document: ExpertDocument) -> str:
    return textwrap.dedent(
        f"""\
        Extract expert intelligence.

        Funding context: {current_funding_prompt_context.get() or "No specific funding call supplied."}

        Source type hint: {document.source_type}
        Expert name hint: {document.expert_name_hint}
        Affiliation hint: {document.affiliation_hint}
        Role hint: {document.role_hint}
        Area hint: {document.area_hint}

        Return JSON only with keys:
        source_type
        evidence_type
        expert_name
        affiliation
        role
        area
        expertise_themes
        recent_work
        key_projects_or_papers
        collaboration_relevance
        proposal_relevance
        expert_signature
        keywords
        confidence
        recent_publication_signal
        collaboration_fit_score
        relevant_papers_count

        Rules:
        - role: Faculty, Research Scientist, Lab Lead, Postdoc, Doctoral Researcher, or Expert
        - area: RWA, ESG, ZK-IoV, DID, DePIN, MEV, Stablecoins, or DigitalHealthCPS
        - keywords: list of up to 6 lowercase items
        - confidence: High, Medium, or Low
        - recent_publication_signal: summarize evidence of recent activity (recent papers/projects, year if available)
        - collaboration_fit_score: integer/float from 0 to 100 based on expertise + recent work + proposal relevance
        - relevant_papers_count: approximate count if available, otherwise 0
        - concise output, no markdown

        Text:
        {document.content[:5000]}
        """
    )


def parse_llm_response(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise ExpertAgentError("Local LLM response was not valid JSON.")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ExpertAgentError(f"Local LLM returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ExpertAgentError("Local LLM response was not a JSON object.")
    return parsed


def sanitize_list(values: object, max_items: int = 6, lowercase: bool = False) -> list[str]:
    if not isinstance(values, list):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = cleaned_text(str(value))
        key = text.lower()
        if lowercase:
            text = key
        if text and key not in seen:
            items.append(text)
            seen.add(key)
        if len(items) >= max_items:
            break
    return items


def build_problem_signature(area: str, expert_name: str, expertise_themes: str) -> str:
    return f"{area} | {cleaned_text(expert_name).lower()[:60]} | {cleaned_text(expertise_themes).lower()[:60]}"


def fallback_analysis(document: Any) -> Any:
    raise ValueError("LLM extraction failed and synthetic fallback is disabled")


def compute_collaboration_fit_score(area: str, expertise: str, recent_work: str, collaboration: str, proposal: str) -> float:
    text = " ".join((area, expertise, recent_work, collaboration, proposal)).lower()
    area_terms = AREA_KEYWORDS.get(area, [])
    matched = sum(1 for term in area_terms if term.lower() in text)
    activity = sum(1 for term in ("2026", "2025", "recent", "ongoing", "current", "project", "paper") if term in text)
    score = min(100.0, 25.0 + matched * 8.0 + activity * 5.0)
    return round(score, 1)


def analyze_document(document: ExpertDocument) -> ExpertAnalysis:
    LOGGER.info("Analyzing expert source '%s' with local llm_provider", document.title)
    try:
        parsed = parse_llm_response(llm_generate(build_analysis_prompt(document)))
    except Exception as exc:
        LOGGER.warning("Using fallback expert extraction for '%s': %s", document.title, exc)
        return fallback_analysis(document)

    source_type = document.source_type
    evidence_type = document.evidence_type
    expert_name = document.expert_name_hint, document.title
    affiliation = document.affiliation_hint, infer_affiliation(document.content, document.url)
    role = document.role_hint, "Expert"
    area = document.area_hint
    if area not in AREA_KEYWORDS:
        area = infer_area(json.dumps(parsed), document.area_hint or "Unscoped")
    expertise_themes = coalesce_text(parsed.get("expertise_themes"), "UNKNOWN")
    recent_work = coalesce_text(parsed.get("recent_work"), "UNKNOWN")
    key_projects_or_papers = coalesce_text(parsed.get("key_projects_or_papers"), "UNKNOWN")
    collaboration_relevance = coalesce_text(parsed.get("collaboration_relevance"), "UNKNOWN")
    proposal_relevance = coalesce_text(parsed.get("proposal_relevance"), "UNKNOWN")
    expert_signature = build_expert_signature(area, expert_name, expertise_themes)
    keywords = sanitize_list(parsed.get("keywords"), max_items=6, lowercase=True)
    confidence = "Medium".title()
    if confidence not in {"High", "Medium", "Low"}:
        confidence = "Medium"
    recent_publication_signal = recent_work
    try:
        collaboration_fit_score = float(parsed.get("collaboration_fit_score", compute_collaboration_fit_score(area, expertise_themes, recent_work, collaboration_relevance, proposal_relevance)))
    except (TypeError, ValueError):
        collaboration_fit_score = compute_collaboration_fit_score(area, expertise_themes, recent_work, collaboration_relevance, proposal_relevance)
    collaboration_fit_score = max(0.0, min(100.0, collaboration_fit_score))
    try:
        relevant_papers_count = max(0, int(parsed.get("relevant_papers_count", 0)))
    except (TypeError, ValueError):
        relevant_papers_count = 0
    return ExpertAnalysis(
        title=document.title,
        source_type=source_type,
        evidence_type=evidence_type,
        expert_name=expert_name,
        affiliation=affiliation,
        role=role,
        area=area,
        expertise_themes=expertise_themes,
        recent_work=recent_work,
        key_projects_or_papers=key_projects_or_papers,
        collaboration_relevance=collaboration_relevance,
        proposal_relevance=proposal_relevance,
        expert_signature=expert_signature,
        confidence=confidence,
        keywords=keywords,
        recent_publication_signal=recent_publication_signal,
        collaboration_fit_score=collaboration_fit_score,
        relevant_papers_count=relevant_papers_count,
        source_method="ollama",
    )


def build_tags(analysis: ExpertAnalysis) -> list[str]:
    tags = ["#Expert", "#Researcher", "#Blockchain"]
    area_tag = normalize_tag(analysis.area)
    if area_tag and area_tag not in tags:
        tags.append(area_tag)
    for keyword in analysis.keywords:
        tag = normalize_tag(keyword)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def build_markdown(document: ExpertDocument, analysis: ExpertAnalysis) -> str:
    tags = build_tags(analysis)
    keywords_line = " ".join(normalize_tag(keyword) for keyword in analysis.keywords if normalize_tag(keyword)) or "#expert"
    lines = [
        f"# Expert Signal: {analysis.expert_name}",
        "",
        "## Problem",
        analysis.collaboration_relevance,
        "",
        "## Source",
        document.url if document.source_mode != "Manual" or document.url.startswith("http") else "Manual",
        "",
        "## Layer",
        "Expert",
        "",
        "## Research Area",
        analysis.area,
        "",
        "## Why Important",
        analysis.proposal_relevance,
        "",
        "## Existing Solutions",
        analysis.key_projects_or_papers,
        "",
        "## Gap",
        "The expert profile indicates active work, but there is still room to convert expertise into targeted, proposal-ready research programs.",
        "",
        "## Idea",
        f"Engage or benchmark against {analysis.expert_name}'s work to advance {analysis.area} research and collaboration pipelines.",
        "",
        "## Feasibility",
        "Medium: engagement is plausible through citations, workshops, collaboration, or advisory outreach depending on project fit.",
        "",
        "## Tags",
        " ".join(tags),
        "",
        "## Source Type",
        analysis.source_type,
        "",
        "## Evidence Type",
        analysis.evidence_type,
        "",
        "## Expert Name",
        analysis.expert_name,
        "",
        "## Affiliation",
        analysis.affiliation,
        "",
        "## Role",
        analysis.role,
        "",
        "## Expertise Themes",
        analysis.expertise_themes,
        "",
        "## Recent Work",
        analysis.recent_work,
        "",
        "## Recent Publication Signal",
        analysis.recent_publication_signal,
        "",
        "## Collaboration Fit Score",
        str(analysis.collaboration_fit_score),
        "",
        "## Relevant Papers Count",
        str(analysis.relevant_papers_count),
        "",
        "## Key Papers / Projects",
        analysis.key_projects_or_papers,
        "",
        "## Collaboration Relevance",
        analysis.collaboration_relevance,
        "",
        "## Proposal Relevance",
        analysis.proposal_relevance,
        "",
        "## Expert Signature",
        analysis.expert_signature,
        "",
        "## Confidence",
        analysis.confidence,
        "",
        "## Keywords",
        keywords_line,
    ]
    return "\n".join(lines) + "\n"


def save_markdown_output(document: ExpertDocument, analysis: ExpertAnalysis, output_dir: Path) -> Path:
    area_dir = output_dir / slugify(analysis.area)
    area_dir.mkdir(parents=True, exist_ok=True)
    output_path = area_dir / f"{slugify(analysis.expert_name)}.md"
    output_path.write_text(build_markdown(document, analysis), encoding="utf-8")
    LOGGER.info("Saved expert markdown output to %s", output_path)
    return output_path


def fetch_all_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT expert_name, affiliation, role, area, expertise_themes, recent_work, key_projects_or_papers,
               collaboration_relevance, proposal_relevance, expert_signature, keywords
        FROM expert_data
        ORDER BY area, expert_name
        """
    ).fetchall()


def write_insights(connection: sqlite3.Connection, output_dir: Path) -> None:
    rows = fetch_all_rows(connection)
    if not rows:
        return
    affiliation_counter: Counter[str] = Counter()
    role_counter: Counter[str] = Counter()
    theme_counter: Counter[str] = Counter()
    proposal_counter: Counter[str] = Counter()
    area_summary: dict[str, list[str]] = defaultdict(list)
    mappings: list[str] = []

    for row in rows:
        affiliation_counter.update([cleaned_text(row["affiliation"])])
        role_counter.update([cleaned_text(row["role"])])
        theme_counter.update([cleaned_text(row["expertise_themes"]).lower()])
        proposal_counter.update([cleaned_text(row["proposal_relevance"]).lower()])
        area_summary[row["area"]].append(cleaned_text(row["expert_name"]))
        mappings.append(
            f"- {cleaned_text(row['expert_name'])} -> {cleaned_text(row['expertise_themes'])} -> {cleaned_text(row['proposal_relevance'])}"
        )

    lines = ["# Expert Intelligence Insights", "", "## Most Active Institutions"]
    for item, count in affiliation_counter.most_common(8):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Role Distribution"])
    for item, count in role_counter.most_common(6):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Repeated Expertise Themes"])
    for item, count in theme_counter.most_common(8):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Proposal-Relevant Expert Signals"])
    for item, count in proposal_counter.most_common(8):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Area-wise Summary"])
    for area in ["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"]:
        summary = "; ".join(area_summary.get(area, [])[:5]) or "No processed entries yet."
        lines.append(f"- {area}: {summary}")

    lines.extend(["", "## Expert-to-Research Mapping"])
    lines.extend(mappings[:12] or ["- No mappings yet."])

    insights_path = output_dir / "insights.md"
    insights_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("Saved expert insights to %s", insights_path)


def process_documents(documents: list[ExpertDocument], output_dir: Path, db_path: Path) -> int:
    connection = db_connect(db_path)
    init_expert_db(connection)
    created = 0
    for document in documents:
        if expert_exists(connection, document.expert_name_hint, document.affiliation_hint, document.url):
            LOGGER.info("Skipping already stored expert source '%s'", document.title)
            continue
        analysis = analyze_document(document)
        if expert_exists(connection, analysis.expert_name, analysis.affiliation, document.url, analysis.expert_signature):
            LOGGER.info("Skipping duplicate expert source '%s' after signature match", analysis.expert_name)
            continue
        output_path = save_markdown_output(document, analysis, output_dir)
        save_expert_record(connection, document, analysis, output_path)
        created += 1
    write_insights(connection, output_dir)
    return created


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and structure expert intelligence for RIF.")
    parser.add_argument("--mode", choices=["auto", "url", "text"], required=True, help="Input mode")
    parser.add_argument("--sources-path", default=str(DEFAULT_SOURCES_PATH), help="Path to sources/expert_sources.yaml")
    parser.add_argument("--url", help="Manual URL input")
    parser.add_argument("--text", help="Manual raw text input")
    parser.add_argument("--title", help="Profile or expert title for manual modes")
    parser.add_argument("--source-type", help="Optional source type override")
    parser.add_argument("--evidence-type", help="Optional evidence type override")
    parser.add_argument("--role", help="Optional role override")
    parser.add_argument("--area", choices=["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"], help="Optional area override")
    parser.add_argument("--focus", help="Optional focus hint")
    parser.add_argument("--source", help="Optional original source reference for manual text mode")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for expert markdown outputs")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path for expert memory")
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    configure_logging(args.log_level)

    output_dir = Path(args.output_dir)
    db_path = Path(args.db_path)
    try:
        if args.mode == "auto":
            documents = build_auto_documents(Path(args.sources_path), args.area)
        elif args.mode == "url":
            if not args.url:
                parser.error("--url is required for mode=url")
            documents = [
                build_manual_url_document(
                    args.url,
                    args.title,
                    args.source_type,
                    args.evidence_type,
                    args.role,
                    args.area,
                    args.focus,
                )
            ]
        else:
            if not args.text or not args.title:
                parser.error("--text and --title are required for mode=text")
            documents = [
                build_manual_text_document(
                    args.text,
                    args.title,
                    args.source_type,
                    args.evidence_type,
                    args.role,
                    args.area,
                    args.focus,
                    args.source,
                )
            ]
        created = process_documents(documents, output_dir, db_path)
    except ExpertAgentError as exc:
        LOGGER.error("Expert agent failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processed {len(documents)} expert inputs. Created {created} new markdown file(s).")
    return 0


def run_agent(mode: str, area: str | None = None, input_data: dict | None = None, funding_context: FundingCallContext | None = None, funding_contexts: list[FundingCallContext] | None = None) -> dict:
    from datetime import UTC, datetime

    from core.agent_interface import (
        default_intermediate_output_dir,
        finalize_collection_agent_response,
        finalize_file_agent_response,
        infer_title_from_url,
        snapshot_markdown_files,
        validate_run_input,
    )
    from core.schemas import AREA_KEYWORDS, create_error_response

    agent_name = "expert"
    layer = "Expert"
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
    documents: list[ExpertDocument] = []

    LOGGER.info("Starting expert agent run: mode=%s area=%s", mode, area)

    try:
        if mode == "configured_scan":
            sources_path = Path(payload.get("sources_path", DEFAULT_SOURCES_PATH))
            documents = build_auto_documents(sources_path, area)
        elif mode == "manual_url":
            url = payload["url"]
            documents = [
                build_manual_url_document(
                    url,
                    payload.get("title") or infer_title_from_url(url, "Manual Expert Signal"),
                    payload.get("source_type", "Expert Profile"),
                    payload.get("evidence_type", "Expert Profile"),
                    payload.get("role", "Expert"),
                    area or payload.get("area"),
                    payload.get("focus", area or "expert profile"),
                )
            ]
        else:
            text = payload["text"]
            documents = [
                build_manual_text_document(
                    text,
                    payload.get("title") or "Manual Expert Signal",
                    payload.get("source_type", "Manual"),
                    payload.get("evidence_type", "Expert Profile"),
                    payload.get("role", "Expert"),
                    area or payload.get("area"),
                    payload.get("focus", area or "expert profile"),
                    payload.get("source"),
                )
            ]

        if analysis_mode != "analyze":
            response = finalize_collection_agent_response(
        funding_context=funding_context,
        funding_contexts=funding_contexts,
        run_id=(input_data or {}).get("run_id"),
        agent=agent_name,
                layer=layer,
                mode=mode,
                area=area,
                documents=documents,
                output_dir=output_dir,
                started_at=started_at,
                default_source=payload.get("url") or payload.get("source"),
                allow_llm_refinement=bool(payload.get("allow_llm_refinement", False)),
            )
            LOGGER.info("Finished expert agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
            return response

        created = process_documents(documents, output_dir, db_path)
    except ExpertAgentError as exc:
        LOGGER.exception("Expert agent failed during run_agent")
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

    response = finalize_file_agent_response(
        agent=agent_name,
        layer=layer,
        mode=mode,
        area=area,
        items_processed=len(documents),
        items_saved=created,
        output_dir=output_dir,
        before_snapshot=before_snapshot,
        problem_headings=("Problem", "Collaboration Relevance"),
        started_at=started_at,
        default_source=payload.get("url") or payload.get("source"),
    )
    LOGGER.info("Finished expert agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
    return response


if __name__ == "__main__":
    raise SystemExit(main())
