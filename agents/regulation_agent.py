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
from core.recursive_collection import (
    RecursiveCollectionConfig,
    collect_recursive_pages,
    collect_recursive_pages_with_review,
    combine_recursive_pages,
    default_allowed_domains,
    save_recursive_review,
    update_recursive_review,
)
from core.source_registry import apply_refresh_policy
from core.llm_provider import generate as llm_generate
from core.schemas import AREA_KEYWORDS
from core.funding_context import funding_context_relevance

DEFAULT_OUTPUT_DIR = Path("outputs/regulation")
DEFAULT_DB_PATH = DEFAULT_OUTPUT_DIR / "regulation_memory.db"
DEFAULT_SOURCES_PATH = Path("sources/regulation_sources.yaml")
RELEVANCE_KEYWORDS = [
    "tokenization",
    "tokenised assets",
    "tokenized assets",
    "crypto-assets",
    "stablecoins",
    "digital assets",
    "cbdc",
    "decentralized identity",
    "did",
    "verifiable credentials",
    "kyc",
    "aml",
    "privacy",
    "compliance",
    "auditability",
    "custody",
    "settlement",
    "carbon credits",
    "esg",
    "mrv",
    "reporting",
    "registry",
    "proof of reserves",
    "disclosure",
    "market integrity",
]
FOCUSED_CONTENT_KEYWORDS = [
    "requirement",
    "requirements",
    "must",
    "shall",
    "reporting",
    "assurance",
    "disclosure",
    "compliance",
    "verification",
    "audit",
    "standard",
    "framework",
    "obligation",
    "control",
    "interoperability",
]
CONSTRAINT_VOCAB = [
    "privacy",
    "auditability",
    "identity",
    "reporting",
    "custody",
    "settlement",
    "disclosure",
    "reserve proof",
    "aml",
    "kyc",
    "interoperability",
    "assurance",
    "market integrity",
]
LOGGER = logging.getLogger("rif.regulation_agent")
RECURSIVE_LINK_KEYWORDS = tuple(sorted(set(RELEVANCE_KEYWORDS + [keyword for values in AREA_KEYWORDS.values() for keyword in values] + ["guidance", "consultation", "framework", "standard", "policy", "report", "registry"])))
RECURSIVE_CONFIG = RecursiveCollectionConfig(
    max_depth=2,
    max_pages=8,
    link_keywords=RECURSIVE_LINK_KEYWORDS,
    relevance_threshold=2,
)


@dataclass(slots=True)
class RegulationSource:
    name: str
    url: str
    focus: str
    source_type: str
    evidence_type: str


@dataclass(slots=True)
class RegulationDocument:
    name: str
    issuing_body: str
    source_type: str
    evidence_type: str
    area_hint: str
    focus: str
    url: str
    title: str
    content: str
    source_mode: str
    recursive_review_id: str = ""
    recursive_page_count: int = 1


@dataclass(slots=True)
class RegulationAnalysis:
    issuing_body: str
    regulation_name: str
    area: str
    requirement: str
    compliance_constraint: str
    technical_problem: str
    research_opportunity: str
    prototype_idea: str
    problem_signature: str
    keywords: list[str]
    confidence: str
    source_type: str
    evidence_type: str
    source_method: str
    document_type: str = "Unknown"
    jurisdiction: str = "Unknown"
    legal_force: str = "Unknown"
    effective_date: str | None = None
    expiry_date: str | None = None
    status: str = "UNKNOWN"
    supersedes: str | None = None
    superseded_by: str | None = None
    evidence_weight: float = 0.5


class RegulationAgentError(Exception):
    """Raised when the regulation agent cannot complete a task."""


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
        raise RegulationAgentError(
            "Automatic and URL modes require 'requests' and 'beautifulsoup4'. Install them before running those modes."
        ) from exc
    return requests, BeautifulSoup


def init_regulation_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS regulation_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            issuing_body TEXT NOT NULL,
            source_type TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            area TEXT NOT NULL,
            url TEXT NOT NULL,
            requirement TEXT NOT NULL,
            compliance_constraint TEXT NOT NULL,
            technical_problem TEXT NOT NULL,
            research_opportunity TEXT NOT NULL,
            problem_signature TEXT NOT NULL,
            keywords TEXT NOT NULL,
            confidence TEXT NOT NULL,
            prototype_idea TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL DEFAULT '',
            last_checked TEXT NOT NULL
        )
        """
    )
    existing = {row[1] for row in connection.execute("PRAGMA table_info(regulation_data)").fetchall()}
    required_columns = {
        "prototype_idea": "TEXT NOT NULL DEFAULT ''",
        "path": "TEXT NOT NULL DEFAULT ''",
        "document_type": "TEXT NOT NULL DEFAULT 'Other'",
        "jurisdiction": "TEXT NOT NULL DEFAULT 'Unknown'",
        "legal_force": "TEXT NOT NULL DEFAULT 'Unknown'",
        "effective_date": "TEXT",
        "expiry_date": "TEXT",
        "status": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "supersedes": "TEXT",
        "superseded_by": "TEXT",
        "evidence_weight": "REAL NOT NULL DEFAULT 0.3",
    }
    for name, definition in required_columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE regulation_data ADD COLUMN {name} {definition}")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_regulation_url ON regulation_data(url)")
    connection.commit()


def regulation_exists(
    connection: sqlite3.Connection,
    name: str,
    url: str,
    issuing_body: str,
    problem_signature: str | None = None,
) -> bool:
    if problem_signature:
        row = connection.execute(
            """
            SELECT 1 FROM regulation_data
            WHERE url = ? OR (name = ? AND issuing_body = ?) OR (name = ? AND issuing_body = ? AND problem_signature = ?)
            """,
            (url, name, issuing_body, name, issuing_body, problem_signature),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT 1 FROM regulation_data WHERE url = ? OR (name = ? AND issuing_body = ?)",
            (url, name, issuing_body),
        ).fetchone()
    return row is not None


def save_regulation_record(
    connection: sqlite3.Connection,
    document: RegulationDocument,
    analysis: RegulationAnalysis,
    output_path: Path,
) -> None:
    connection.execute(
        """
        INSERT INTO regulation_data (
            name,
            issuing_body,
            source_type,
            evidence_type,
            area,
            url,
            requirement,
            compliance_constraint,
            technical_problem,
            research_opportunity,
            problem_signature,
            keywords,
            confidence,
            prototype_idea,
            path,
            last_checked
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
                name=excluded.name,
                issuing_body=excluded.issuing_body,
                source_type=excluded.source_type,
                evidence_type=excluded.evidence_type,
                area=excluded.area,
                requirement=excluded.requirement,
                compliance_constraint=excluded.compliance_constraint,
                technical_problem=excluded.technical_problem,
                research_opportunity=excluded.research_opportunity,
                problem_signature=excluded.problem_signature,
                keywords=excluded.keywords,
                confidence=excluded.confidence,
                prototype_idea=excluded.prototype_idea,
                path=excluded.path,
                last_checked=excluded.last_checked
        """,
        (
            analysis.regulation_name,
            analysis.issuing_body,
            analysis.source_type,
            analysis.evidence_type,
            analysis.area,
            document.url,
            analysis.requirement,
            analysis.compliance_constraint,
            analysis.technical_problem,
            analysis.research_opportunity,
            analysis.problem_signature,
            json.dumps(analysis.keywords),
            analysis.confidence,
            analysis.prototype_idea,
            str(output_path),
            datetime.now(UTC).isoformat(),
        ),
    )
    connection.commit()


def load_sources(sources_path: Path) -> list[RegulationSource]:
    if not sources_path.exists():
        raise RegulationAgentError(f"Regulation sources file not found: {sources_path}")
    try:
        data = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RegulationAgentError(f"Invalid YAML in {sources_path}: {exc}") from exc

    root = data.get("regulation_sources", {})
    sources: list[RegulationSource] = []
    for _, entries in root.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = cleaned_text(str(entry.get("name", "")))
            url = cleaned_text(str(entry.get("url", "")))
            focus = cleaned_text(str(entry.get("focus", "")))
            source_type = cleaned_text(str(entry.get("source_type", ""))) or "Regulation"
            evidence_type = cleaned_text(str(entry.get("evidence_type", ""))) or "Guidance"
            if name and url:
                sources.append(
                    RegulationSource(
                        name=name,
                        url=url,
                        focus=focus,
                        source_type=source_type,
                        evidence_type=evidence_type,
                    )
                )
    if not sources:
        raise RegulationAgentError("No regulation sources were loaded from sources/regulation_sources.yaml.")
    return apply_refresh_policy("regulation", sources)


def fetch_html(url: str) -> str:
    from core.shared_crawl_manager import SharedCrawlManager
    try:
        res = SharedCrawlManager.get().fetch(url)
        if res.status_code and res.status_code >= 400:
            LOGGER.warning("HTTP %s for %s", res.status_code, url)
            raise RegulationAgentError(f"Unable to fetch page {url}: HTTP {res.status_code}")
        LOGGER.debug("Fetched %s via %s", url, res.fetch_method)
        return res.html
    except RegulationAgentError:
        raise
    except Exception as exc:
        raise RegulationAgentError(f"Unable to fetch page {url}: {exc}") from exc

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
        focus_keywords=FOCUSED_CONTENT_KEYWORDS + RELEVANCE_KEYWORDS,
        area_keywords=[keyword for keywords in AREA_KEYWORDS.values() for keyword in keywords],
        min_score=3,
        max_chars=6500,
    )
    return title, (focused_content or content)[:7000]


def infer_area(text: str, default: str = "Unscoped") -> str:
    lowered = text.lower()
    if default in AREA_KEYWORDS and any(keyword in lowered for keyword in AREA_KEYWORDS[default]):
        return default
    for area, keywords in AREA_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return area
    return default


def infer_constraint(text: str) -> str:
    lowered = text.lower()
    for item in CONSTRAINT_VOCAB:
        if item in lowered:
            return item
    return "compliance"


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


def build_auto_documents(sources_path: Path, area: str | None = None) -> list[RegulationDocument]:
    import yaml
    raw_sources = yaml.safe_load(sources_path.read_text(encoding="utf-8")) if sources_path.exists() else {}
    from core.source_registry import extract_yaml_limits
    limits_by_url = extract_yaml_limits(raw_sources)
    from core.crawl_context import current_crawl_context
    documents: list[RegulationDocument] = []
    for source in load_sources(sources_path):
        LOGGER.info("Collecting regulation intelligence from %s", source.name)
        structured = collect_structured_source(source.url, source.source_type, area)
        if structured:
            full_text = cleaned_text(f"{source.name} {source.focus} {structured.text}")
            area_hint = area or infer_area(full_text, "Unscoped")
            if is_relevant(full_text, area_hint):
                documents.append(RegulationDocument(name=source.name, issuing_body=source.name, source_type=source.source_type, evidence_type=source.evidence_type, area_hint=area_hint, focus=source.focus, url=structured.url, title=structured.title, content=full_text, source_mode="Automatic"))
            continue
        try:
            html = fetch_html(source.url)
        except RegulationAgentError as exc:
            LOGGER.warning("Skipping source %s: %s", source.name, exc)
            continue
        from core.http_client import canonicalize_url
        limits = limits_by_url.get(source.url)
        if not limits:
            limits = limits_by_url.get(canonicalize_url(source.url), {})

        source_limit_configured = bool(limits)
        ctx = current_crawl_context.get(None)

        if ctx:
            ctx.source_limits_telemetry.append({
                "source_name": source.name,
                "url": source.url,
                "source_limit_configured": source_limit_configured,
                "source_limit_matched": source_limit_configured,
                "max_pages": limits.get("max_pages"),
                "max_documents": limits.get("max_documents"),
                "max_depth": limits.get("max_depth"),
                "max_seconds": limits.get("max_seconds"),
                "global_fallback_used": not source_limit_configured
            })
            ctx.begin_source()

        pages, review = collect_recursive_pages_with_review(
            root_url=source.url,
            root_name=source.name,
            initial_html=html,
            fetch_html=fetch_html,
            extract_page_text=extract_page_text,
            config=RecursiveCollectionConfig(
                max_depth=limits.get('max_depth', RECURSIVE_CONFIG.max_depth),
                max_pages=limits.get('max_pages', RECURSIVE_CONFIG.max_pages),
                max_documents=limits.get('max_documents', getattr(RECURSIVE_CONFIG, 'max_documents', None)),
                max_seconds=limits.get('max_seconds', getattr(RECURSIVE_CONFIG, 'max_seconds', None)),
                allowed_domains=default_allowed_domains(source.url),
                link_keywords=RECURSIVE_CONFIG.link_keywords,
                relevance_threshold=RECURSIVE_CONFIG.relevance_threshold,
                char_limit_per_page=RECURSIVE_CONFIG.char_limit_per_page,
            ),
        )
        save_recursive_review(review)
        title, content = combine_recursive_pages(pages, root_name=source.name, focus=source.focus)
        area_hint = area or infer_area(cleaned_text(f"{source.focus} {title} {content}"), "Unscoped")
        full_text = cleaned_text(f"{source.name} {source.focus} {title} {content}")
        if not is_relevant(full_text, area_hint):
            LOGGER.info("Skipping %s because the source text is not technically relevant.", source.name)
            update_recursive_review(
                review["review_id"],
                outcome="filtered_out_irrelevant",
                outcome_reason="document did not pass regulation relevance filters",
                artifact_title=title,
                selected_area=area_hint or "",
            )
            continue
        documents.append(
            RegulationDocument(
                name=source.name,
                issuing_body=source.name,
                source_type=source.source_type,
                evidence_type=source.evidence_type,
                area_hint=area_hint,
                focus=source.focus,
                url=source.url,
                title=title,
                content=full_text,
                source_mode="Automatic",
                recursive_review_id=review["review_id"],
                recursive_page_count=len(pages),
            )
        )
        update_recursive_review(
            review["review_id"],
            outcome="accepted_for_collection",
            outcome_reason="document passed regulation relevance filters",
            artifact_title=title,
            selected_area=area_hint or "",
        )
    return documents


def build_manual_url_document(
    url: str,
    name: str | None,
    issuing_body: str | None,
    source_type: str | None,
    evidence_type: str | None,
    area: str | None,
    focus: str | None,
) -> RegulationDocument:
    html = fetch_html(url)
    pages = collect_recursive_pages(
        root_url=url,
        initial_html=html,
        fetch_html=fetch_html,
        extract_page_text=extract_page_text,
        config=RecursiveCollectionConfig(
            max_depth=limits.get('max_depth', RECURSIVE_CONFIG.max_depth),
            max_pages=limits.get('max_pages', RECURSIVE_CONFIG.max_pages),
            allowed_domains=default_allowed_domains(url),
            link_keywords=RECURSIVE_CONFIG.link_keywords,
            relevance_threshold=RECURSIVE_CONFIG.relevance_threshold,
            char_limit_per_page=RECURSIVE_CONFIG.char_limit_per_page,
        ),
    )
    title, content = combine_recursive_pages(pages, root_name=name or cleaned_text(urlparse(url).netloc), focus=focus or "")
    resolved_name = name or title or urlparse(url).netloc
    resolved_body = issuing_body or cleaned_text(urlparse(url).netloc)
    area_hint = area or infer_area(cleaned_text(f"{resolved_name} {focus or ''} {title} {content}"), "Unscoped")
    full_text = cleaned_text(f"{resolved_name} {resolved_body} {focus or ''} {title} {content}")
    if not is_relevant(full_text, area_hint):
        raise RegulationAgentError("Manual URL content does not map to the selected research areas or technical policy signals.")
    return RegulationDocument(
        name=resolved_name,
        issuing_body=resolved_body,
        source_type=source_type or "Regulation",
        evidence_type=evidence_type or "Guidance",
        area_hint=area_hint,
        focus=focus or title,
        url=url,
        title=title,
        content=full_text,
        source_mode="Manual",
    )


def build_manual_text_document(
    text: str,
    name: str,
    issuing_body: str | None,
    source_type: str | None,
    evidence_type: str | None,
    area: str | None,
    focus: str | None,
) -> RegulationDocument:
    content = cleaned_text(text)
    area_hint = area or infer_area(cleaned_text(f"{name} {focus or ''} {content}"), "Unscoped")
    if not is_relevant(cleaned_text(f"{area_hint} {focus or ''} {content}"), area_hint):
        raise RegulationAgentError("Manual text does not map to the selected research areas or technical policy signals.")
    return RegulationDocument(
        name=name,
        issuing_body=issuing_body or name,
        source_type=source_type or "Regulation",
        evidence_type=evidence_type or "Guidance",
        area_hint=area_hint,
        focus=focus or area_hint,
        url=f"manual://{slugify(name)}/{slugify(area_hint)}",
        title=name,
        content=content,
        source_mode="Manual",
    )


def build_analysis_prompt(document: RegulationDocument) -> str:
    return textwrap.dedent(
        f"""\
        Extract regulation intelligence.

        Funding context: {current_funding_prompt_context.get() or "No specific funding call supplied."}

        Issuing body: {document.issuing_body}
        Name hint: {document.name}
        Area hint: {document.area_hint}
        Source type hint: {document.source_type}
        Evidence type hint: {document.evidence_type}

        Return JSON only with keys:
        issuing_body
        regulation_name
        area
        requirement
        compliance_constraint
        technical_problem
        research_opportunity
        prototype_idea
        problem_signature
        keywords
        confidence
        source_type
        evidence_type

        Rules:
        - area: RWA, ESG, ZK-IoV, DID, DePIN, MEV, Stablecoins, or DigitalHealthCPS
        - keywords: list of up to 6 lowercase items
        - confidence: High, Medium, or Low
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
            raise RegulationAgentError("Local LLM response was not valid JSON.")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise RegulationAgentError(f"Local LLM returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RegulationAgentError("Local LLM response was not a JSON object.")
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


def build_problem_signature(area: str, constraint: str, technical_problem: str) -> str:
    return f"{area} | {cleaned_text(constraint).lower()[:60]} | {cleaned_text(technical_problem).lower()[:60]}"


EVIDENCE_WEIGHTS = {
    "Binding Regulation": 1.00,
    "Regulatory Guidance": 0.85,
    "Consultation": 0.65,
    "Technical Standard": 0.60,
    "Policy Report": 0.45,
    "Framework": 0.50,
    "Registry Standard": 0.55,
    "Other": 0.30,
}


def normalize_document_type(source_type: str, evidence_type: str) -> str:
    text = f"{source_type} {evidence_type}".lower()
    if "consult" in text:
        return "Consultation"
    if "guidance" in text:
        return "Regulatory Guidance"
    if "standard" in text or "iso" in text or "w3c" in text:
        return "Technical Standard"
    if "framework" in text:
        return "Framework"
    if "registry" in text:
        return "Registry Standard"
    if "policy" in text or "report" in text:
        return "Policy Report"
    if "regulation" in text or "official" in text or "binding" in text:
        return "Binding Regulation"
    return "Other"


def infer_jurisdiction(text: str) -> str:
    lowered = text.lower()
    mapping = {
        "India": ("india", "rbi", "sebi", "meity"),
        "European Union": ("european union", "eu ", "mica", "csrd", "esrs", "efrag"),
        "United States": ("united states", "sec ", "us ", "federal"),
        "International": ("fatf", "bis", "iosco", "world bank", "international"),
    }
    for jurisdiction, terms in mapping.items():
        if any(term in lowered for term in terms):
            return jurisdiction
    return "Unknown"


def infer_legal_force(document_type: str) -> str:
    return {
        "Binding Regulation": "Binding",
        "Regulatory Guidance": "Advisory",
        "Consultation": "Consultative",
        "Technical Standard": "Voluntary",
        "Policy Report": "None",
        "Framework": "Voluntary",
        "Registry Standard": "Voluntary",
        "Other": "None",
    }.get(document_type, "Unknown")


def _status_from_dates(effective_date: str | None, expiry_date: str | None) -> str:
    today = datetime.now(UTC).date()
    def parse(value: str | None):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    effective = parse(effective_date)
    expiry = parse(expiry_date)
    if effective and today < effective:
        return "UPCOMING"
    if expiry and today > expiry:
        return "EXPIRED"
    return "ACTIVE" if effective or expiry else "UNKNOWN"


def fallback_analysis(document: Any) -> Any:
    raise ValueError("LLM extraction failed and synthetic fallback is disabled")


def analyze_document(document: RegulationDocument) -> RegulationAnalysis:
    LOGGER.info("Analyzing regulation source '%s' with local llm_provider", document.name)
    try:
        parsed = parse_llm_response(llm_generate(build_analysis_prompt(document)))
    except Exception as exc:
        LOGGER.warning("Using fallback regulation extraction for '%s': %s", document.name, exc)
        return fallback_analysis(document)

    area = document.area_hint
    if area not in AREA_KEYWORDS:
        area = infer_area(json.dumps(parsed), document.area_hint or "Unscoped")
    issuing_body = document.issuing_body
    regulation_name = document.name
    requirement = coalesce_text(parsed.get("requirement"), "UNKNOWN")
    
    constraint = infer_constraint(document.content)
    technical_problem = coalesce_text(parsed.get("technical_problem"), "UNKNOWN")
    
    research_opportunity = coalesce_text(parsed.get("research_opportunity"), "UNKNOWN")
    
    prototype_idea = coalesce_text(parsed.get("prototype_idea"), "UNKNOWN")
    
    problem_signature = build_problem_signature(area, constraint, technical_problem)
    keywords = sanitize_list(parsed.get("keywords"), max_items=6, lowercase=True)
    confidence = "Medium".title()
    if confidence not in {"High", "Medium", "Low"}:
        confidence = "Medium"
    source_type = document.source_type
    evidence_type = document.evidence_type
    return RegulationAnalysis(
        issuing_body=issuing_body,
        regulation_name=regulation_name,
        area=area,
        requirement=requirement,
        compliance_constraint=constraint,
        technical_problem=technical_problem,
        research_opportunity=research_opportunity,
        prototype_idea=prototype_idea,
        problem_signature=problem_signature,
        keywords=keywords,
        confidence=confidence,
        source_type=source_type,
        evidence_type=evidence_type,
        source_method="ollama",
        document_type=cleaned_text(str(parsed.get("document_type", ""))) or normalize_document_type(document.source_type, document.evidence_type),
        jurisdiction=cleaned_text(str(parsed.get("jurisdiction", ""))) or infer_jurisdiction(f"{issuing_body} {document.content}"),
        legal_force=cleaned_text(str(parsed.get("legal_force", ""))) or infer_legal_force(cleaned_text(str(parsed.get("document_type", ""))) or normalize_document_type(document.source_type, document.evidence_type)),
        effective_date=None,
        expiry_date=coalesce_text(parsed.get("expiry_date")) or None,
        status=cleaned_text(str(parsed.get("status", ""))).upper() or _status_from_dates(None, coalesce_text(parsed.get("expiry_date")) or None),
        supersedes=None,
        superseded_by=coalesce_text(parsed.get("superseded_by")) or None,
        evidence_weight=float(parsed.get("evidence_weight") or EVIDENCE_WEIGHTS.get(cleaned_text(str(parsed.get("document_type", ""))) or normalize_document_type(document.source_type, document.evidence_type), 0.30)),
    )


def build_tags(analysis: RegulationAnalysis) -> list[str]:
    tags = ["#Regulation", "#Blockchain", "#Compliance"]
    area_tag = normalize_tag(analysis.area)
    if area_tag and area_tag not in tags:
        tags.append(area_tag)
    for keyword in analysis.keywords:
        tag = normalize_tag(keyword)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def build_markdown(document: RegulationDocument, analysis: RegulationAnalysis) -> str:
    tags = build_tags(analysis)
    lines = [
        f"# Regulation / Standard: {analysis.regulation_name}",
        "",
        "## Layer",
        "Regulation",
        "",
        "## Source Type",
        analysis.source_type,
        "",
        "## Evidence Type",
        analysis.evidence_type,
        "",
        "## Issuing Body",
        analysis.issuing_body,
        "",
        "## Confidence",
        analysis.confidence,
        "",
        "## Document Type",
        analysis.document_type,
        "",
        "## Jurisdiction",
        analysis.jurisdiction,
        "",
        "## Legal Force",
        analysis.legal_force,
        "",
        "## Effective Date",
        analysis.effective_date or "Unknown",
        "",
        "## Expiry Date",
        analysis.expiry_date or "Unknown",
        "",
        "## Status",
        analysis.status,
        "",
        "## Supersedes",
        analysis.supersedes or "None identified",
        "",
        "## Superseded By",
        analysis.superseded_by or "None identified",
        "",
        "## Evidence Weight",
        str(analysis.evidence_weight),
        "",
        "## Area",
        analysis.area,
        "",
        "## Requirement",
        analysis.requirement,
        "",
        "## Compliance Constraint",
        analysis.compliance_constraint,
        "",
        "## Technical Problem Created",
        analysis.technical_problem,
        "",
        "## Research Opportunity",
        analysis.research_opportunity,
        "",
        "## Prototype Idea",
        analysis.prototype_idea,
        "",
        "## Problem Signature",
        analysis.problem_signature,
        "",
        "## Source",
        document.url if document.source_mode != "Manual" or document.url.startswith("http") else "Manual",
        "",
        "## Tags",
        " ".join(tags),
    ]
    return "\n".join(lines) + "\n"


def save_markdown_output(document: RegulationDocument, analysis: RegulationAnalysis, output_dir: Path) -> Path:
    area_dir = output_dir / slugify(analysis.area)
    area_dir.mkdir(parents=True, exist_ok=True)
    output_path = area_dir / f"{slugify(analysis.regulation_name)}.md"
    output_path.write_text(build_markdown(document, analysis), encoding="utf-8")
    LOGGER.info("Saved regulation markdown output to %s", output_path)
    return output_path


def fetch_all_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT name, issuing_body, source_type, evidence_type, area, requirement, compliance_constraint,
               technical_problem, research_opportunity, problem_signature, keywords, prototype_idea
        FROM regulation_data
        ORDER BY area, name
        """
    ).fetchall()


def write_insights(connection: sqlite3.Connection, output_dir: Path) -> None:
    rows = fetch_all_rows(connection)
    if not rows:
        return
    constraint_counter: Counter[str] = Counter()
    technical_problem_counter: Counter[str] = Counter()
    research_counter: Counter[str] = Counter()
    prototype_counter: Counter[str] = Counter()
    area_summary: dict[str, list[str]] = defaultdict(list)
    mappings: list[str] = []

    for row in rows:
        constraint_counter.update([cleaned_text(row["compliance_constraint"]).lower()])
        technical_problem_counter.update([cleaned_text(row["technical_problem"]).lower()])
        research_counter.update([cleaned_text(row["research_opportunity"]).lower()])
        prototype_counter.update([cleaned_text(row["prototype_idea"]).lower()])
        area_summary[row["area"]].append(cleaned_text(row["technical_problem"]))
        mappings.append(
            f"- {cleaned_text(row['requirement'])} -> {cleaned_text(row['technical_problem'])} -> {cleaned_text(row['research_opportunity'])}"
        )

    lines = ["# Regulation & Standards Intelligence Insights", "", "## Most Common Compliance Constraints"]
    for item, count in constraint_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Repeated Technical Problems"])
    for item, count in technical_problem_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Research Opportunities"])
    for item, count in research_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Prototype Opportunities"])
    for item, count in prototype_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Area-wise Summary"])
    for area in ["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"]:
        summary = "; ".join(area_summary.get(area, [])[:3]) or "No processed entries yet."
        lines.append(f"- {area}: {summary}")

    lines.extend(["", "## Regulation-to-Research Mapping"])
    lines.extend(mappings[:10] or ["- No mappings yet."])

    insights_path = output_dir / "insights.md"
    insights_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("Saved regulation insights to %s", insights_path)


def process_documents(documents: list[RegulationDocument], output_dir: Path, db_path: Path) -> int:
    connection = db_connect(db_path)
    init_regulation_db(connection)
    created = 0
    for document in documents:
        if regulation_exists(connection, document.name, document.url, document.issuing_body):
            LOGGER.info("Skipping already stored regulation source '%s'", document.name)
            continue
        analysis = analyze_document(document)
        if regulation_exists(connection, analysis.regulation_name, document.url, analysis.issuing_body, analysis.problem_signature):
            LOGGER.info("Skipping duplicate regulation source '%s' after signature match", analysis.regulation_name)
            continue
        output_path = save_markdown_output(document, analysis, output_dir)
        save_regulation_record(connection, document, analysis, output_path)
        created += 1
    write_insights(connection, output_dir)
    return created


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and structure regulation intelligence for RIF.")
    parser.add_argument("--mode", choices=["auto", "url", "text"], required=True, help="Input mode")
    parser.add_argument("--area", help="Optional research area filter for auto mode")
    parser.add_argument("--sources-path", default=str(DEFAULT_SOURCES_PATH), help="Path to sources/regulation_sources.yaml")
    parser.add_argument("--url", help="Manual URL input")
    parser.add_argument("--text", help="Manual raw text input")
    parser.add_argument("--name", help="Regulation or standard name for manual modes")
    parser.add_argument("--issuing-body", help="Issuing body for manual modes")
    parser.add_argument("--source-type", help="Optional source type override")
    parser.add_argument("--evidence-type", help="Optional evidence type override")
    parser.add_argument("--area", choices=["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"], help="Optional area override")
    parser.add_argument("--focus", help="Optional focus hint")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for regulation markdown outputs")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path for regulation memory")
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
                    args.name,
                    args.issuing_body,
                    args.source_type,
                    args.evidence_type,
                    args.area,
                    args.focus,
                )
            ]
        else:
            if not args.text or not args.name:
                parser.error("--text and --name are required for mode=text")
            documents = [
                build_manual_text_document(
                    args.text,
                    args.name,
                    args.issuing_body,
                    args.source_type,
                    args.evidence_type,
                    args.area,
                    args.focus,
                )
            ]
        created = process_documents(documents, output_dir, db_path)
    except RegulationAgentError as exc:
        LOGGER.error("Regulation agent failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processed {len(documents)} regulation inputs. Created {created} new markdown file(s).")
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

    agent_name = "regulation"
    layer = "Regulation"
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

    LOGGER.info("Starting regulation agent run: mode=%s area=%s", mode, area)

    try:
        if mode == "configured_scan":
            sources_path = Path(payload.get("sources_path", DEFAULT_SOURCES_PATH))
            documents = build_auto_documents(sources_path, area)
        elif mode == "manual_url":
            url = payload["url"]
            documents = [
                build_manual_url_document(
                    url,
                    payload.get("name") or infer_title_from_url(url, "Manual Regulation"),
                    payload.get("issuing_body", "Unknown"),
                    payload.get("source_type", "Regulation"),
                    payload.get("evidence_type", "Official Regulation"),
                    area or payload.get("area"),
                    payload.get("focus", area or "configured compliance"),
                )
            ]
        else:
            text = payload["text"]
            documents = [
                build_manual_text_document(
                    text,
                    payload.get("name") or payload.get("title") or "Manual Regulation",
                    payload.get("issuing_body", "Unknown"),
                    payload.get("source_type", "Manual"),
                    payload.get("evidence_type", "Guidance"),
                    area or payload.get("area"),
                    payload.get("focus", area or "configured compliance"),
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
            LOGGER.info("Finished regulation agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
            return response

        created = process_documents(documents, output_dir, db_path)
    except RegulationAgentError as exc:
        LOGGER.exception("Regulation agent failed during run_agent")
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
        problem_headings=("Technical Problem Created", "Research Opportunity"),
        started_at=started_at,
        default_source=payload.get("url") or payload.get("source"),
    )
    LOGGER.info("Finished regulation agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
    return response


if __name__ == "__main__":
    raise SystemExit(main())
from core.structured_sources import collect_structured_source
