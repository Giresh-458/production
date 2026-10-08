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

DEFAULT_OUTPUT_DIR = Path("outputs/companies")
DEFAULT_DB_PATH = DEFAULT_OUTPUT_DIR / "company_memory.db"
DEFAULT_SOURCES_PATH = Path("sources/company_sources.yaml")
AREA_MAP = {
    "rwa": "RWA",
    "esg_carbon": "ESG",
    "zk_iov": "ZK-IoV",
    "did": "DID",
    "depin": "DePIN",
    "mev": "MEV",
    "stablecoins": "Stablecoins",
    "cps": "DigitalHealthCPS",
    "digitalhealthcps": "DigitalHealthCPS",
}
SIGNAL_KEYWORDS = [
    "tokenization",
    "rwa",
    "real-world assets",
    "carbon credits",
    "esg",
    "mrv",
    "zero-knowledge",
    "zk",
    "decentralized identity",
    "did",
    "verifiable credentials",
    "depin",
    "decentralized physical infrastructure",
    "mev",
    "stablecoin",
    "settlement",
    "compliance",
    "oracle",
    "smart contract",
    "digital asset",
    "enterprise pilot",
    "case study",
    "deployment",
]
FOCUSED_CONTENT_KEYWORDS = [
    "use case",
    "case study",
    "customer",
    "deployment",
    "architecture",
    "product",
    "platform",
    "solution",
    "integration",
    "pilot",
    "workflow",
    "how it works",
    "enterprise",
    "infrastructure",
]
COMPONENT_VOCAB = [
    "blockchain",
    "smart contracts",
    "oracles",
    "did",
    "zk",
    "tokenization",
    "compliance layer",
    "iot / sensors",
    "stablecoin rails",
    "validator infrastructure",
    "privacy proofs",
]
SECTOR_KEYWORDS = {
    "finance": ["fund", "security", "asset", "bank", "payments", "settlement", "treasury"],
    "carbon markets": ["carbon", "climate", "mrv", "sustainability", "ecological"],
    "mobility": ["vehicle", "mobility", "v2x", "transport"],
    "identity": ["identity", "credential", "authentication", "attestation"],
    "telecom": ["wireless", "telecom", "connectivity", "network"],
    "energy": ["energy", "grid", "climate", "renewable"],
    "infrastructure": ["storage", "compute", "mapping", "infrastructure", "sequencing"],
}
LOGGER = logging.getLogger("rif.company_agent")
RECURSIVE_LINK_KEYWORDS = tuple(sorted(set(SIGNAL_KEYWORDS + [keyword for values in AREA_KEYWORDS.values() for keyword in values] + ["docs", "case study", "pilot", "product", "platform", "whitepaper"])))
RECURSIVE_CONFIG = RecursiveCollectionConfig(
    max_depth=1,
    max_pages=4,
    link_keywords=RECURSIVE_LINK_KEYWORDS,
    relevance_threshold=2,
)


@dataclass(slots=True)
class CompanySource:
    name: str
    url: str
    source_type: str
    focus: str
    area: str


@dataclass(slots=True)
class CompanyDocument:
    company_name: str
    url: str
    source_type: str
    focus: str
    area_hint: str
    content: str
    source_mode: str
    title: str
    recursive_review_id: str = ""
    recursive_page_count: int = 1
    funding_context: str = ""


@dataclass(slots=True)
class CompanyAnalysis:
    source_type: str
    evidence_type: str
    area: str
    use_case: str
    target_sector: str
    problem: str
    product_or_deployment: str
    technical_components: list[str]
    real_world_constraint: str
    research_gap: str
    prototype_idea: str
    funding_alignment: str
    problem_signature: str
    keywords: list[str]
    confidence: str
    source_method: str
    partner_fit: str = "Unknown"
    industry_need: str = "Unknown"
    potential_project_role: str = "Unknown"
    data_contribution: str = "Unknown"
    deployment_capability: str = "Unknown"
    cofunding_signal: str = "Unknown"
    geographic_eligibility: str = "Unknown"


class CompanyAgentError(Exception):
    """Raised when the company agent cannot complete a task."""


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
        raise CompanyAgentError(
            "Automatic and URL modes require 'requests' and 'beautifulsoup4'. Install them before running those modes."
        ) from exc
    return requests, BeautifulSoup


def init_company_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS company_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            area TEXT NOT NULL,
            url TEXT NOT NULL,
            use_case TEXT NOT NULL,
            problem TEXT NOT NULL,
            research_gap TEXT NOT NULL,
            problem_signature TEXT NOT NULL,
            keywords TEXT NOT NULL,
            technical_components TEXT NOT NULL DEFAULT '[]',
            real_world_constraint TEXT NOT NULL DEFAULT '',
            funding_alignment TEXT NOT NULL DEFAULT '',
            prototype_idea TEXT NOT NULL DEFAULT '',
            confidence TEXT NOT NULL,
            path TEXT NOT NULL DEFAULT '',
            last_checked TEXT NOT NULL
        )
        """
    )
    existing = {row[1] for row in connection.execute("PRAGMA table_info(company_data)").fetchall()}
    required_columns = {
        "technical_components": "TEXT NOT NULL DEFAULT '[]'",
        "real_world_constraint": "TEXT NOT NULL DEFAULT ''",
        "funding_alignment": "TEXT NOT NULL DEFAULT ''",
        "prototype_idea": "TEXT NOT NULL DEFAULT ''",
        "path": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in required_columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE company_data ADD COLUMN {name} {definition}")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_company_url ON company_data(url)")
    connection.execute("DROP INDEX IF EXISTS idx_company_problem_signature")
    connection.commit()


def company_exists(connection: sqlite3.Connection, company_name: str, url: str, problem_signature: str | None = None) -> bool:
    if problem_signature:
        row = connection.execute(
            "SELECT 1 FROM company_data WHERE company_name = ? OR url = ? OR problem_signature = ?",
            (company_name, url, problem_signature),
        ).fetchone()
    else:
        row = connection.execute("SELECT 1 FROM company_data WHERE company_name = ? OR url = ?", (company_name, url)).fetchone()
    return row is not None


def save_company_record(connection: sqlite3.Connection, document: CompanyDocument, analysis: CompanyAnalysis, output_path: Path) -> None:
    connection.execute(
        """
        INSERT INTO company_data (
            company_name,
            source_type,
            evidence_type,
            area,
            url,
            use_case,
            problem,
            research_gap,
            problem_signature,
            keywords,
            technical_components,
            real_world_constraint,
            funding_alignment,
            prototype_idea,
            confidence,
            path,
            last_checked
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
                company_name=excluded.company_name,
                source_type=excluded.source_type,
                evidence_type=excluded.evidence_type,
                area=excluded.area,
                use_case=excluded.use_case,
                problem=excluded.problem,
                research_gap=excluded.research_gap,
                problem_signature=excluded.problem_signature,
                keywords=excluded.keywords,
                technical_components=excluded.technical_components,
                real_world_constraint=excluded.real_world_constraint,
                funding_alignment=excluded.funding_alignment,
                prototype_idea=excluded.prototype_idea,
                confidence=excluded.confidence,
                path=excluded.path,
                last_checked=excluded.last_checked
        """,
        (
            document.company_name,
            analysis.source_type,
            analysis.evidence_type,
            analysis.area,
            document.url,
            analysis.use_case,
            analysis.problem,
            analysis.research_gap,
            analysis.problem_signature,
            json.dumps(analysis.keywords),
            json.dumps(analysis.technical_components),
            analysis.real_world_constraint,
            analysis.funding_alignment,
            analysis.prototype_idea,
            analysis.confidence,
            str(output_path),
            datetime.now(UTC).isoformat(),
        ),
    )
    connection.commit()


def load_sources(sources_path: Path) -> list[CompanySource]:
    if not sources_path.exists():
        raise CompanyAgentError(f"Company sources file not found: {sources_path}")
    try:
        data = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CompanyAgentError(f"Invalid YAML in {sources_path}: {exc}") from exc

    root = data.get("companies", {})
    sources: list[CompanySource] = []
    for raw_area, entries in root.items():
        area = AREA_MAP.get(raw_area)
        if not area or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = cleaned_text(str(entry.get("name", "")))
            url = cleaned_text(str(entry.get("url", "")))
            source_type = cleaned_text(str(entry.get("source_type", ""))) or "Blockchain-native"
            focus = cleaned_text(str(entry.get("focus", "")))
            if name and url:
                sources.append(CompanySource(name=name, url=url, source_type=source_type, focus=focus, area=area))
    if not sources:
        raise CompanyAgentError("No company sources were loaded from sources/company_sources.yaml.")
    return apply_refresh_policy("company", sources)


def fetch_html(url: str) -> str:
    from core.shared_crawl_manager import SharedCrawlManager
    try:
        res = SharedCrawlManager.get().fetch(url)
        if res.status_code and res.status_code >= 400:
            LOGGER.warning("HTTP %s for %s", res.status_code, url)
            raise CompanyAgentError(f"Unable to fetch page {url}: HTTP {res.status_code}")
        LOGGER.debug("Fetched %s via %s", url, res.fetch_method)
        return res.html
    except CompanyAgentError:
        raise
    except Exception as exc:
        raise CompanyAgentError(f"Unable to fetch page {url}: {exc}") from exc

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
        focus_keywords=FOCUSED_CONTENT_KEYWORDS + SIGNAL_KEYWORDS,
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


def infer_sector(text: str) -> str:
    lowered = text.lower()
    for sector, keywords in SECTOR_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return sector
    return "infrastructure"


def detect_components(text: str) -> list[str]:
    lowered = text.lower()
    components: list[str] = []
    for component in COMPONENT_VOCAB:
        probe = component.replace(" / ", " ").replace(" ", "")
        if component in lowered or probe in lowered.replace("-", "").replace(" ", ""):
            components.append(component)
    return components[:8]


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


def discover_company_candidates(query: str, limit: int = 10) -> list[CompanyDocument]:
    """Discover organization candidates from Wikidata search, then let the LLM classify partner fit."""
    requests, _ = load_requests_and_bs4()
    try:
        from core.http_client import fetch_json
        response, _ = fetch_json(
            "https://www.wikidata.org/w/api.php",
            params={"action": "wbsearchentities", "search": query, "language": "en", "format": "json", "limit": limit},
            timeout=(8.0, 20.0),
            retries=1,
        )
        items = response.get("search", [])
    except Exception as exc:
        raise CompanyAgentError(f"Wikidata organization discovery failed: {exc}") from exc

    documents: list[CompanyDocument] = []
    for item in items:
        name = cleaned_text(str(item.get("label", "")))
        description = cleaned_text(str(item.get("description", "")))
        entity_id = cleaned_text(str(item.get("id", "")))
        if not name or not description:
            continue
        content = (
            f"Organization candidate: {name}\nDescription: {description}\n"
            f"Wikidata entity: {entity_id}\nFunding context: {query}"
        )
        documents.append(
            CompanyDocument(
                company_name=name,
                url=f"https://www.wikidata.org/wiki/{entity_id}",
                source_type="Discovery Candidate",
                focus=query,
                area_hint=infer_area(query, "Unscoped"),
                content=content,
                source_mode="Discovery",
                title=name,
                funding_context=query,
            )
        )
    return documents


def build_auto_documents(sources_path: Path, area: str | None = None) -> list[CompanyDocument]:
    import yaml
    raw_sources = yaml.safe_load(sources_path.read_text(encoding="utf-8")) if sources_path.exists() else {}
    from core.source_registry import extract_yaml_limits
    limits_by_url = extract_yaml_limits(raw_sources)
    from core.crawl_context import current_crawl_context
    documents: list[CompanyDocument] = []
    for source in load_sources(sources_path):
        if area and source.area != area:
            continue
        LOGGER.info("Collecting company intelligence from %s", source.name)
        try:
            html = fetch_html(source.url)
        except CompanyAgentError as exc:
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
        full_text = cleaned_text(f"{source.name} {source.focus} {content}")
        area_hint = area or source.area
        if not is_relevant(full_text, area_hint):
            LOGGER.info("Skipping %s because the source text is not relevant to the configured research areas.", source.name)
            update_recursive_review(
                review["review_id"],
                outcome="filtered_out_irrelevant",
                outcome_reason="document did not pass company relevance filters",
                artifact_title=title,
                selected_area=area_hint or "",
            )
            continue
        documents.append(
            CompanyDocument(
                company_name=source.name,
                url=source.url,
                source_type=source.source_type,
                focus=source.focus,
                area_hint=area_hint,
                content=full_text,
                source_mode="Automatic",
                title=title,
                recursive_review_id=review["review_id"],
                recursive_page_count=len(pages),
            )
        )
        update_recursive_review(
            review["review_id"],
            outcome="accepted_for_collection",
            outcome_reason="document passed company relevance filters",
            artifact_title=title,
            selected_area=area_hint or "",
        )
    return documents


def build_manual_url_document(url: str, company_name: str | None, source_type: str | None, area: str | None, focus: str | None, funding_context: str = "") -> CompanyDocument:
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
    title, content = combine_recursive_pages(pages, root_name=company_name or cleaned_text(urlparse(url).netloc), focus=focus or "")
    name = company_name or cleaned_text(urlparse(url).netloc)
    source_type_value = source_type or "Enterprise adopter"
    focus_value = focus or title
    area_hint = area or infer_area(cleaned_text(f"{name} {title} {content}"), "Unscoped")
    document = CompanyDocument(
        company_name=name,
        url=url,
        source_type=source_type_value,
        focus=focus_value,
        area_hint=area_hint,
        content=cleaned_text(f"{name} {focus_value} {title} {content}"),
        source_mode="Manual",
        title=title,
    )
    if not is_relevant(document.content, document.area_hint):
        raise CompanyAgentError("Manual URL content does not map to the selected research areas or use-case signals.")
    return document


def build_manual_text_document(text: str, company_name: str, source_type: str | None, area: str | None, focus: str | None, funding_context: str = "") -> CompanyDocument:
    content = cleaned_text(text)
    area_hint = area or infer_area(cleaned_text(f"{company_name} {focus or ''} {content}"), "Unscoped")
    if not is_relevant(cleaned_text(f"{area_hint} {focus or ''} {content}"), area_hint):
        raise CompanyAgentError("Manual text does not map to the selected research areas or use-case signals.")
    manual_url = f"manual://{slugify(company_name)}/{slugify(area_hint)}"
    return CompanyDocument(
        company_name=company_name,
        url=manual_url,
        source_type=source_type or "Blockchain-native",
        focus=focus or area_hint,
        area_hint=area_hint,
        content=content,
        source_mode="Manual",
        title=company_name,
        funding_context=funding_context,
    )


def build_analysis_prompt(document: CompanyDocument) -> str:
    return textwrap.dedent(
        f"""\
        Extract company intelligence.

        Company: {document.company_name}
        Source type hint: {document.source_type}
        Area hint: {document.area_hint}
        Focus: {document.focus}
        Funding context: {current_funding_prompt_context.get() or "No specific funding call supplied."}

        Return JSON only with keys:
        source_type
        evidence_type
        area
        use_case
        target_sector
        problem
        product_or_deployment
        technical_components
        real_world_constraint
        research_gap
        prototype_idea
        funding_alignment
        problem_signature
        keywords
        confidence
        partner_fit
        industry_need
        potential_project_role
        data_contribution
        deployment_capability
        cofunding_signal
        geographic_eligibility

        Rules:
        - evidence_type: Product, Case Study, Technical Documentation, Whitepaper, Blog, or Enterprise Pilot
        - area: RWA, ESG, ZK-IoV, DID, DePIN, MEV, Stablecoins, or DigitalHealthCPS
        - technical_components: list of up to 6 short items
        - keywords: list of up to 6 lowercase items
        - confidence: High, Medium, or Low
        - assess fit against the supplied funding context, not against the company in isolation
        - do not claim cofunding, data access, or geographic eligibility unless supported by the source
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
            raise CompanyAgentError("Local LLM response was not valid JSON.")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise CompanyAgentError(f"Local LLM returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CompanyAgentError("Local LLM response was not a JSON object.")
    return parsed


def sanitize_list(values: object, max_items: int = 6, lowercase: bool = False) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned_items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = cleaned_text(str(value))
        key = text.lower()
        if lowercase:
            text = key
        if text and key not in seen:
            cleaned_items.append(text)
            seen.add(key)
        if len(cleaned_items) >= max_items:
            break
    return cleaned_items


def build_problem_signature(area: str, problem: str, constraint: str) -> str:
    problem_short = cleaned_text(problem).lower()
    constraint_short = cleaned_text(constraint).lower()
    return f"{area} | {problem_short[:60]} | {constraint_short[:60]}"


def fallback_analysis(document: Any) -> Any:
    raise ValueError("LLM extraction failed and synthetic fallback is disabled")


def analyze_document(document: CompanyDocument) -> CompanyAnalysis:
    LOGGER.info("Analyzing company source '%s' with local llm_provider", document.company_name)
    try:
        parsed = parse_llm_response(llm_generate(build_analysis_prompt(document)))
    except Exception as exc:
        LOGGER.warning("Using fallback company extraction for '%s': %s", document.company_name, exc)
        return fallback_analysis(document)

    area = document.area_hint
    if area not in AREA_KEYWORDS:
        area = infer_area(json.dumps(parsed), document.area_hint)
    source_type = document.source_type
    evidence_type = "Product"
    use_case = document.focus
    target_sector = infer_sector(document.content)
    problem = coalesce_text(parsed.get("problem"), "UNKNOWN")
    product_or_deployment = document.focus, document.title
    technical_components = sanitize_list(parsed.get("technical_components"), max_items=6) or detect_components(document.content) or [
        "blockchain",
        "smart contracts",
    ]
    real_world_constraint = coalesce_text(parsed.get("real_world_constraint"), "UNKNOWN")
    
    research_gap = coalesce_text(parsed.get("research_gap"), "UNKNOWN")
    
    prototype_idea = coalesce_text(parsed.get("prototype_idea"), "UNKNOWN")
    
    funding_alignment = "industry / infrastructure"
    keywords = sanitize_list(parsed.get("keywords"), max_items=6, lowercase=True)
    confidence = "Medium".title()
    if confidence not in {"High", "Medium", "Low"}:
        confidence = "Medium"
    problem_signature = build_problem_signature(area, problem, real_world_constraint)
    return CompanyAnalysis(
        source_type=source_type,
        evidence_type=evidence_type,
        area=area,
        use_case=use_case,
        target_sector=target_sector,
        problem=problem,
        product_or_deployment=product_or_deployment,
        technical_components=technical_components,
        real_world_constraint=real_world_constraint,
        research_gap=research_gap,
        prototype_idea=prototype_idea,
        funding_alignment=funding_alignment,
        problem_signature=problem_signature,
        keywords=keywords,
        confidence=confidence,
        source_method="ollama",
        partner_fit="Unknown",
        industry_need=problem,
        potential_project_role="Industry deployment or pilot partner",
        data_contribution="Unknown",
        deployment_capability="Unknown",
        cofunding_signal="Unknown",
        geographic_eligibility="Unknown",
    )


def build_tags(analysis: CompanyAnalysis) -> list[str]:
    tags = ["#Company", "#Blockchain"]
    area_tag = normalize_tag(analysis.area)
    if area_tag and area_tag not in tags:
        tags.append(area_tag)
    for keyword in analysis.keywords:
        tag = normalize_tag(keyword)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def bullet_lines(items: list[str], empty_text: str = "None identified.") -> str:
    if not items:
        return f"- {empty_text}"
    return "\n".join(f"- {item}" for item in items)


def build_markdown(document: CompanyDocument, analysis: CompanyAnalysis) -> str:
    output_area_slug = slugify(analysis.area)
    tags = build_tags(analysis)
    sections = [
        f"# Company / Use Case: {document.company_name}",
        "",
        "## Layer",
        "Company",
        "",
        "## Source Type",
        analysis.source_type,
        "",
        "## Evidence Type",
        analysis.evidence_type,
        "",
        "## Confidence",
        analysis.confidence,
        "",
        "## Partner Fit",
        analysis.partner_fit,
        "",
        "## Industry Need",
        analysis.industry_need,
        "",
        "## Potential Project Role",
        analysis.potential_project_role,
        "",
        "## Data Contribution",
        analysis.data_contribution,
        "",
        "## Deployment Capability",
        analysis.deployment_capability,
        "",
        "## Cofunding Signal",
        analysis.cofunding_signal,
        "",
        "## Geographic Eligibility",
        analysis.geographic_eligibility,
        "",
        "## Area",
        analysis.area,
        "",
        "## Use Case",
        analysis.use_case,
        "",
        "## Target Sector",
        analysis.target_sector,
        "",
        "## Problem Being Solved",
        analysis.problem,
        "",
        "## Product or Deployment",
        analysis.product_or_deployment,
        "",
        "## Technical Components",
        bullet_lines(analysis.technical_components),
        "",
        "## Real-World Constraint",
        analysis.real_world_constraint,
        "",
        "## Research Gap",
        analysis.research_gap,
        "",
        "## Possible Prototype Idea",
        analysis.prototype_idea,
        "",
        "## Funding Alignment",
        analysis.funding_alignment,
        "",
        "## Problem Signature",
        analysis.problem_signature,
        "",
        "## Source",
        document.url if document.source_mode != "Manual" or document.url.startswith("http") else "Manual",
        "",
        "## Tags",
        " ".join(tags),
        "",
        "<!-- area_slug: " + output_area_slug + " -->",
    ]
    return "\n".join(sections) + "\n"


def save_markdown_output(document: CompanyDocument, analysis: CompanyAnalysis, output_dir: Path) -> Path:
    area_dir = output_dir / slugify(analysis.area)
    area_dir.mkdir(parents=True, exist_ok=True)
    output_path = area_dir / f"{slugify(document.company_name)}.md"
    output_path.write_text(build_markdown(document, analysis), encoding="utf-8")
    LOGGER.info("Saved company markdown output to %s", output_path)
    return output_path


def fetch_all_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT company_name, source_type, evidence_type, area, url, use_case, problem, research_gap, problem_signature, keywords, confidence
               , technical_components, real_world_constraint, funding_alignment, prototype_idea
        FROM company_data
        ORDER BY area, company_name
        """
    ).fetchall()


def write_insights(connection: sqlite3.Connection, output_dir: Path) -> None:
    rows = fetch_all_rows(connection)
    if not rows:
        return
    use_case_counter: Counter[str] = Counter()
    component_counter: Counter[str] = Counter()
    gap_counter: Counter[str] = Counter()
    constraint_counter: Counter[str] = Counter()
    prototype_counter: Counter[str] = Counter()
    funding_counter: Counter[str] = Counter()
    area_summary: dict[str, list[str]] = defaultdict(list)

    for row in rows:
        use_case_counter.update([cleaned_text(row["use_case"]).lower()])
        for keyword in json.loads(row["keywords"] or "[]"):
            component_counter.update([keyword])
        gap_counter.update([cleaned_text(row["research_gap"]).lower()])
        for component in json.loads(row["technical_components"] or "[]"):
            component_counter.update([cleaned_text(str(component)).lower()])
        constraint_counter.update([cleaned_text(row["real_world_constraint"]).lower()])
        prototype_counter.update([cleaned_text(row["prototype_idea"]).lower()])
        funding_counter.update([cleaned_text(row["funding_alignment"]).lower()])
        area_summary[row["area"]].append(cleaned_text(row["problem"]))

    lines = ["# Company Intelligence Insights", "", "## Most Common Use Cases"]
    for item, count in use_case_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Common Technical Components"])
    for item, count in component_counter.most_common(10):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Repeated Research Gaps"])
    for item, count in gap_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Real-World Constraints"])
    for item, count in constraint_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Prototype Opportunities"])
    for item, count in prototype_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Funding Alignment Patterns"])
    for item, count in funding_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Area-wise Summary"])
    for area in ["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"]:
        summary = "; ".join(area_summary.get(area, [])[:3]) or "No processed entries yet."
        lines.append(f"- {area}: {summary}")

    insights_path = output_dir / "insights.md"
    insights_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("Saved company insights to %s", insights_path)


def process_documents(documents: list[CompanyDocument], output_dir: Path, db_path: Path) -> int:
    connection = db_connect(db_path)
    init_company_db(connection)
    created = 0
    for document in documents:
        if company_exists(connection, document.company_name, document.url):
            LOGGER.info("Skipping already stored company source '%s'", document.company_name)
            continue
        analysis = analyze_document(document)
        if company_exists(connection, document.company_name, document.url, analysis.problem_signature):
            LOGGER.info("Skipping duplicate company source '%s' after signature match", document.company_name)
            continue
        output_path = save_markdown_output(document, analysis, output_dir)
        save_company_record(connection, document, analysis, output_path)
        created += 1
    write_insights(connection, output_dir)
    return created


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and structure company/use-case intelligence for RIF.")
    parser.add_argument("--mode", choices=["auto", "url", "text"], required=True, help="Input mode")
    parser.add_argument("--area", help="Optional research area filter for auto mode")
    parser.add_argument("--sources-path", default=str(DEFAULT_SOURCES_PATH), help="Path to sources/company_sources.yaml")
    parser.add_argument("--url", help="Manual URL input")
    parser.add_argument("--text", help="Manual raw text input")
    parser.add_argument("--company-name", help="Company name for manual modes")
    parser.add_argument("--source-type", help="Optional source type override")
    parser.add_argument("--area", choices=["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"], help="Optional area override")
    parser.add_argument("--focus", help="Optional focus/use-case hint")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for company markdown outputs")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path for company memory")
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
            documents = [build_manual_url_document(args.url, args.company_name, args.source_type, args.area, args.focus)]
        else:
            if not args.text or not args.company_name:
                parser.error("--text and --company-name are required for mode=text")
            documents = [build_manual_text_document(args.text, args.company_name, args.source_type, args.area, args.focus)]

        created = process_documents(documents, output_dir, db_path)
    except CompanyAgentError as exc:
        LOGGER.error("Company agent failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processed {len(documents)} company inputs. Created {created} new markdown file(s).")
    return 0


def run_agent(mode: str, area: str | None = None, input_data: dict | None = None, funding_context: FundingCallContext | None = None, funding_contexts: list[FundingCallContext] | None = None) -> dict:
    from datetime import UTC, datetime

    from core.agent_interface import (
        default_intermediate_output_dir,
        finalize_collection_agent_response,
        finalize_file_agent_response,
        infer_name_from_url,
        snapshot_markdown_files,
        validate_run_input,
    )
    from core.schemas import AREA_KEYWORDS, create_error_response

    agent_name = "company"
    layer = "Company"
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

    LOGGER.info("Starting company agent run: mode=%s area=%s", mode, area)

    try:
        if mode == "configured_scan":
            sources_path = Path(payload.get("sources_path", DEFAULT_SOURCES_PATH))
            if payload.get("discover") and payload.get("funding_context"):
                documents = discover_company_candidates(str(payload.get("funding_context")), int(payload.get("max_candidates", 10)))
            else:
                documents = build_auto_documents(sources_path, area)
        elif mode == "manual_url":
            url = payload["url"]
            company_name = payload.get("company_name") or infer_name_from_url(url, "Manual Company")
            documents = [
                build_manual_url_document(
                    url,
                    company_name,
                    payload.get("source_type", "Unknown"),
                    area or payload.get("area"),
                    payload.get("focus", area or "configured research use case"),
                    payload.get("funding_context", ""),
                )
            ]
        else:
            text = payload["text"]
            company_name = payload.get("company_name") or payload.get("title") or "Manual Company"
            documents = [
                build_manual_text_document(
                    text,
                    company_name,
                    payload.get("source_type", "Manual"),
                    area or payload.get("area"),
                    payload.get("focus", area or "configured research use case"),
                    payload.get("funding_context", ""),
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
            LOGGER.info("Finished company agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
            return response

        created = process_documents(documents, output_dir, db_path)
    except CompanyAgentError as exc:
        LOGGER.exception("Company agent failed during run_agent")
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
        problem_headings=("Problem Being Solved", "Research Gap"),
        started_at=started_at,
        default_source=payload.get("url") or payload.get("source"),
    )
    LOGGER.info("Finished company agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
    return response


if __name__ == "__main__":
    raise SystemExit(main())
