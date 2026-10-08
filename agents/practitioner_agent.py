from __future__ import annotations

import argparse
import json
import logging
import os
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

DEFAULT_OUTPUT_DIR = Path("outputs/practitioners")
DEFAULT_DB_PATH = DEFAULT_OUTPUT_DIR / "practitioner_memory.db"
DEFAULT_SOURCES_PATH = Path("sources/practitioner_sources.yaml")
RELEVANCE_KEYWORDS = [
    "real-world assets",
    "performance",
    "scalability",
    "latency",
    "reliability",
    "failure",
    "production failure",
    "engineering problem",
    "research problem",

    "production issue",
    "deployment",
    "integration problem",
    "developer experience",
    "tooling gap",
    "compliance",
    "custody",
    "settlement",
    "oracle",
    "data feed",
    "tokenization",
    "rwa",
    "carbon credits",
    "esg",
    "mrv",
    "zero-knowledge",
    "proving",
    "verifier",
    "privacy",
    "did",
    "verifiable credentials",
    "depin",
    "sensor",
    "wireless",
    "storage",
    "mev",
    "block builder",
    "transaction ordering",
    "stablecoin",
    "payment rails",
    "liquidity",
    "interoperability",
    "scalability",
    "security",
    "audit",
    "monitoring",
    "reliability",
    "hiring",
    "job",
]
PRACTITIONER_SIGNAL_KEYWORDS = [
    "latency", "performance", "failure", "throughput", "production",

    "pain point",
    "deployment constraint",
    "production issue",
    "missing tooling",
    "scalability bottleneck",
    "privacy",
    "compliance",
    "interoperability",
    "data quality",
    "security concern",
    "hiring",
    "operational limitation",
    "developer friction",
    "reliability",
    "integration",
]
FOCUSED_CONTENT_KEYWORDS = [
    "pain point",
    "problem",
    "constraint",
    "friction",
    "reliability",
    "incident",
    "deployment",
    "tooling",
    "integration",
    "monitoring",
    "hiring",
    "skills required",
    "operational limitation",
    "developer experience",
]
ROLE_KEYWORDS = {
    "Engineer": ["engineer", "developer", "protocol engineer", "infra engineer"],
    "Founder": ["founder", "ceo", "cofounder"],
    "Product Lead": ["product", "pm", "product lead"],
    "Researcher": ["researcher", "scientist", "research"],
    "Policy Expert": ["policy", "regulatory", "compliance"],
    "Hiring Signal": ["hiring", "job", "role", "skills required"],
}
LOGGER = logging.getLogger("rif.practitioner_agent")


@dataclass(slots=True)
class PractitionerSource:
    name: str
    type: str
    url: str
    focus: str


@dataclass(slots=True)
class PractitionerDocument:
    title: str
    source_type: str
    evidence_type: str
    practitioner_role_hint: str
    area_hint: str
    focus: str
    url: str
    content: str
    source_mode: str


@dataclass(slots=True)
class PractitionerAnalysis:
    title: str
    source_type: str
    evidence_type: str
    practitioner_role: str
    area: str
    pain_point: str
    deployment_constraint: str
    technical_problem: str
    research_opportunity: str
    prototype_idea: str
    problem_signature: str
    keywords: list[str]
    confidence: str
    source_method: str
    author: str = "Unknown"
    organization: str = "Unknown"
    publication_date: str | None = None
    experience_context: str = ""


class PractitionerAgentError(Exception):
    """Raised when the practitioner agent cannot complete a task."""


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
        raise PractitionerAgentError(
            "Automatic and URL modes require 'requests' and 'beautifulsoup4'. Install them before running those modes."
        ) from exc
    return requests, BeautifulSoup


def init_practitioner_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS practitioner_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            source_type TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            practitioner_role TEXT NOT NULL,
            area TEXT NOT NULL,
            url TEXT NOT NULL,
            pain_point TEXT NOT NULL,
            deployment_constraint TEXT NOT NULL,
            technical_problem TEXT NOT NULL,
            research_opportunity TEXT NOT NULL,
            prototype_idea TEXT NOT NULL,
            problem_signature TEXT NOT NULL,
            keywords TEXT NOT NULL,
            confidence TEXT NOT NULL,
            path TEXT NOT NULL DEFAULT '',
            last_checked TEXT NOT NULL
        )
        """
    )
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_practitioner_url ON practitioner_data(url)")
    connection.commit()


def practitioner_exists(
    connection: sqlite3.Connection,
    title: str,
    url: str,
    pain_point: str | None = None,
    problem_signature: str | None = None,
) -> bool:
    if pain_point and problem_signature:
        row = connection.execute(
            """
            SELECT 1 FROM practitioner_data
            WHERE url = ? OR title = ? OR (title = ? AND pain_point = ?) OR (title = ? AND problem_signature = ?)
            """,
            (url, title, title, pain_point, title, problem_signature),
        ).fetchone()
    else:
        row = connection.execute("SELECT 1 FROM practitioner_data WHERE url = ? OR title = ?", (url, title)).fetchone()
    return row is not None


def save_practitioner_record(
    connection: sqlite3.Connection,
    document: PractitionerDocument,
    analysis: PractitionerAnalysis,
    output_path: Path,
) -> None:
    connection.execute(
        """
        INSERT INTO practitioner_data (
            title,
            source_type,
            evidence_type,
            practitioner_role,
            area,
            url,
            pain_point,
            deployment_constraint,
            technical_problem,
            research_opportunity,
            prototype_idea,
            problem_signature,
            keywords,
            confidence,
            path,
            last_checked
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
                title=excluded.title,
                source_type=excluded.source_type,
                evidence_type=excluded.evidence_type,
                practitioner_role=excluded.practitioner_role,
                area=excluded.area,
                pain_point=excluded.pain_point,
                deployment_constraint=excluded.deployment_constraint,
                technical_problem=excluded.technical_problem,
                research_opportunity=excluded.research_opportunity,
                prototype_idea=excluded.prototype_idea,
                problem_signature=excluded.problem_signature,
                keywords=excluded.keywords,
                confidence=excluded.confidence,
                path=excluded.path,
                last_checked=excluded.last_checked
        """,
        (
            analysis.title,
            analysis.source_type,
            analysis.evidence_type,
            analysis.practitioner_role,
            analysis.area,
            document.url,
            analysis.pain_point,
            analysis.deployment_constraint,
            analysis.technical_problem,
            analysis.research_opportunity,
            analysis.prototype_idea,
            analysis.problem_signature,
            json.dumps(analysis.keywords),
            analysis.confidence,
            str(output_path),
            datetime.now(UTC).isoformat(),
        ),
    )
    connection.commit()


def load_sources(sources_path: Path) -> list[PractitionerSource]:
    if not sources_path.exists():
        raise PractitionerAgentError(f"Practitioner sources file not found: {sources_path}")
    try:
        data = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PractitionerAgentError(f"Invalid YAML in {sources_path}: {exc}") from exc
    root = data.get("practitioner_sources", {})
    sources: list[PractitionerSource] = []
    for _, entries in root.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = cleaned_text(str(entry.get("name", "")))
            source_type = cleaned_text(str(entry.get("type", ""))) or "article"
            url = cleaned_text(str(entry.get("url", "")))
            focus = cleaned_text(str(entry.get("focus", "")))
            if name and url:
                sources.append(PractitionerSource(name=name, type=source_type, url=url, focus=focus))
    if not sources:
        raise PractitionerAgentError("No practitioner sources were loaded from sources/practitioner_sources.yaml.")
    return apply_refresh_policy("practitioner", sources)


def fetch_html(url: str) -> str:
    from core.shared_crawl_manager import SharedCrawlManager
    try:
        res = SharedCrawlManager.get().fetch(url)
        if res.status_code and res.status_code >= 400:
            LOGGER.warning("HTTP %s for %s", res.status_code, url)
            raise PractitionerAgentError(f"Unable to fetch page {url}: HTTP {res.status_code}")
        LOGGER.debug("Fetched %s via %s", url, res.fetch_method)
        return res.html
    except PractitionerAgentError:
        raise
    except Exception as exc:
        raise PractitionerAgentError(f"Unable to fetch page {url}: {exc}") from exc

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
        focus_keywords=FOCUSED_CONTENT_KEYWORDS + PRACTITIONER_SIGNAL_KEYWORDS,
        area_keywords=[keyword for keywords in AREA_KEYWORDS.values() for keyword in keywords],
        min_score=3,
        max_chars=7000,
    )
    return title, (focused_content or content)[:8000]


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
    return "Unknown"


def infer_source_type(raw_type: str) -> str:
    mapping = {
        "forum": "Forum",
        "forum_or_docs": "Forum",
        "technical_blog": "Blog",
        "job_board": "Job Posting",
        "conference": "Talk",
        "hackathon_or_talks": "Talk",
        "industry_blog": "Article",
        "research_blog": "Article",
    }
    return mapping.get(raw_type, "Article")


def infer_evidence_type(raw_type: str) -> str:
    mapping = {
        "forum": "Technical Discussion",
        "forum_or_docs": "Technical Discussion",
        "technical_blog": "Practitioner Experience",
        "job_board": "Job Signal",
        "conference": "Deployment Report",
        "hackathon_or_talks": "Deployment Report",
        "industry_blog": "Opinion with Technical Detail",
        "research_blog": "Opinion with Technical Detail",
    }
    return mapping.get(raw_type, "Practitioner Experience")


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


def _github_repo_from_url(url: str) -> tuple[str, str] | None:
    match = re.search(r"github\.com/([^/]+)/([^/#?]+)", url)
    return (match.group(1), match.group(2).removesuffix(".git")) if match else None


def fetch_native_practitioner_source(source: PractitionerSource) -> tuple[str, str]:
    """Return structured practitioner signals from GitHub or Stack Overflow."""
    requests, _ = load_requests_and_bs4()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "RIF-Practitioner-Agent/1.0"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if source.type.startswith("github_"):
        parts = _github_repo_from_url(source.url)
        if not parts:
            raise PractitionerAgentError(f"GitHub source must point to owner/repository: {source.url}")
        owner, repo = parts
        endpoint = "issues" if source.type == "github_issues" else "discussions"
        if endpoint == "issues":
            api_url = f"https://api.github.com/repos/{owner}/{repo}/issues"
        else:
            api_url = f"https://api.github.com/repos/{owner}/{repo}/discussions"
        try:
            from core.http_client import fetch_json
            response, _ = fetch_json(api_url, headers=headers, params={"state": "open", "per_page": 20}, timeout=(8.0, 20.0), retries=1)
            items = response if isinstance(response, list) else []
        except requests.RequestException as exc:
            raise PractitionerAgentError(f"Unable to fetch GitHub {endpoint}: {exc}") from exc
        
        lines = [f"Structured GitHub practitioner source: {owner}/{repo}"]
        for item in items:
            labels = ", ".join(
                str(label.get("name", "")) for label in item.get("labels", []) if isinstance(label, dict)
            )
            lines.append(
                f"Author: {item.get('user', {}).get('login', 'Unknown')} | Date: {item.get('updated_at', '')} | "
                f"Title: {item.get('title', '')} | Labels: {labels} | URL: {item.get('html_url', '')} | "
                f"Body: {cleaned_text(str(item.get('body', '')))[:800]}"
            )
        return f"{source.name} — {source.focus}", "\n".join(lines)
    if source.type == "stack_overflow":
        try:
            from core.http_client import fetch_json
            response, _ = fetch_json(
                "https://api.stackexchange.com/2.3/search/advanced",
                params={"site": "stackoverflow", "q": source.focus, "pagesize": 20, "order": "desc", "sort": "activity"},
                timeout=(8.0, 20.0),
                retries=1,
            )
            items = response.get("items", [])
        except requests.RequestException as exc:
            raise PractitionerAgentError(f"Unable to fetch Stack Overflow data: {exc}") from exc
            
        lines = [f"Structured Stack Overflow practitioner source: {source.focus}"]
        for item in items:
            owner = item.get("owner", {}) or {}
            lines.append(
                f"Author: {owner.get('display_name', 'Unknown')} | Date: {item.get('last_activity_date', '')} | "
                f"Title: {item.get('title', '')} | Score: {item.get('score', 0)} | "
                f"Answers: {item.get('answer_count', 0)} | URL: {item.get('link', '')}"
            )
        return source.name, "\n".join(lines)
    raise PractitionerAgentError(f"Unsupported native practitioner source type: {source.type}")


def build_auto_documents(sources_path: Path, area: str | None = None) -> list[PractitionerDocument]:
    import yaml
    raw_sources = yaml.safe_load(sources_path.read_text(encoding="utf-8")) if sources_path.exists() else {}
    from core.source_registry import extract_yaml_limits
    limits_by_url = extract_yaml_limits(raw_sources)
    from core.crawl_context import current_crawl_context
    documents: list[PractitionerDocument] = []
    for source in load_sources(sources_path):
        LOGGER.info("Collecting practitioner intelligence from %s", source.name)
        try:
            if source.type in {"github_issues", "github_discussions", "stack_overflow"}:
                title, content = fetch_native_practitioner_source(source)
                full_text = cleaned_text(f"{source.name} {source.focus} {title} {content}")
            else:
                limits = limits_by_url.get(source.url, {})

                ctx = current_crawl_context.get(None)

                if ctx: ctx.begin_source()

                result = collect_source(source.url, keywords=("issue", "discussion", "deployment", "developer", "engineering", "operational", "job", "benchmark", "problem"), max_depth=limits.get('max_depth', 2), max_pages=limits.get('max_pages', 8), max_documents=limits.get('max_documents'), max_seconds=limits.get('max_seconds'))
                title = result.pages[0].title if result.pages else source.name
                full_text = cleaned_text(combine_pages(result, prefix=f"{source.name} {source.focus}"))
        except Exception as exc:
            LOGGER.warning("Skipping source %s: %s", source.name, exc)
            continue
        area_hint = area or infer_area(full_text, "Unscoped")
        if not is_relevant(full_text, area_hint):
            LOGGER.info("Skipping %s because the source text did not pass relevance filters.", source.name)
            continue
        documents.append(PractitionerDocument(title=title or source.name, source_type=infer_source_type(source.type), evidence_type=infer_evidence_type(source.type), practitioner_role_hint=infer_role(full_text), area_hint=area_hint, focus=source.focus, url=source.url, content=full_text, source_mode="Automatic"))
    return documents


def build_manual_url_document(
    url: str,
    title: str | None,
    source_type: str | None,
    evidence_type: str | None,
    practitioner_role: str | None,
    area: str | None,
    focus: str | None,
) -> PractitionerDocument:
    html = fetch_html(url)
    page_title, content = extract_page_text(html)
    resolved_title = title or page_title or cleaned_text(urlparse(url).netloc)
    full_text = cleaned_text(f"{resolved_title} {area or ''} {focus or ''} {content}")
    area_hint = area or infer_area(full_text, "Unscoped")
    if not is_relevant(full_text, area_hint):
        raise PractitionerAgentError("Manual URL content does not map to the selected research areas or practitioner signals.")
    return PractitionerDocument(
        title=resolved_title,
        source_type=source_type or "Article",
        evidence_type=evidence_type or "Practitioner Experience",
        practitioner_role_hint=practitioner_role or infer_role(full_text),
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
    practitioner_role: str | None,
    area: str | None,
    focus: str | None,
) -> PractitionerDocument:
    content = cleaned_text(text)
    area_hint = area or infer_area(cleaned_text(f"{title} {focus or ''} {content}"), "Unscoped")
    if not is_relevant(cleaned_text(f"{title} {area_hint} {focus or ''} {content}"), area_hint):
        raise PractitionerAgentError("Manual text does not map to the selected research areas or practitioner signals.")
    return PractitionerDocument(
        title=title,
        source_type=source_type or "Manual",
        evidence_type=evidence_type or "Practitioner Experience",
        practitioner_role_hint=practitioner_role or infer_role(content),
        area_hint=area_hint,
        focus=focus or area_hint,
        url=f"manual://{slugify(title)}/{slugify(area_hint)}",
        content=content,
        source_mode="Manual",
    )


def build_analysis_prompt(document: PractitionerDocument) -> str:
    return textwrap.dedent(
        f"""\
        Extract practitioner intelligence.

        Funding context: {current_funding_prompt_context.get() or "No specific funding call supplied."}

        Source type hint: {document.source_type}
        Practitioner role hint: {document.practitioner_role_hint}
        Area hint: {document.area_hint}

        Return JSON only with keys:
        source_type
        practitioner_role
        area
        pain_point
        deployment_constraint
        technical_problem
        research_opportunity
        prototype_idea
        problem_signature
        keywords
        confidence
        evidence_type

        Rules:
        - source_type: Blog, Forum, Talk, Podcast, Job Posting, Article, or Manual
        - practitioner_role: Engineer, Founder, Product Lead, Researcher, Policy Expert, Hiring Signal, Unknown
        - area: RWA, ESG, ZK-IoV, DID, DePIN, MEV, Stablecoins, or DigitalHealthCPS
        - keywords: list of up to 6 lowercase items
        - confidence: High, Medium, or Low
        - author and organization: only when explicitly present in source
        - publication_date: ISO date when explicit, otherwise null
        - experience_context: concrete deployment/role context, not marketing language
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
            raise PractitionerAgentError("Local LLM response was not valid JSON.")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise PractitionerAgentError(f"Local LLM returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PractitionerAgentError("Local LLM response was not a JSON object.")
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


def build_problem_signature(area: str, pain_point: str, technical_problem: str) -> str:
    return f"{area} | {cleaned_text(pain_point).lower()[:60]} | {cleaned_text(technical_problem).lower()[:60]}"


def fallback_analysis(document: Any) -> Any:
    raise ValueError("LLM extraction failed and synthetic fallback is disabled")


def analyze_document(document: PractitionerDocument) -> PractitionerAnalysis:
    LOGGER.info("Analyzing practitioner source '%s' with local llm_provider", document.title)
    try:
        parsed = parse_llm_response(llm_generate(build_analysis_prompt(document)))
    except Exception as exc:
        LOGGER.warning("Using fallback practitioner extraction for '%s': %s", document.title, exc)
        return fallback_analysis(document)

    source_type = document.source_type
    evidence_type = document.evidence_type
    practitioner_role = document.practitioner_role_hint, "Unknown"
    area = document.area_hint
    if area not in AREA_KEYWORDS:
        area = infer_area(json.dumps(parsed), document.area_hint or "Unscoped")
    pain_point = coalesce_text(parsed.get("pain_point"), "UNKNOWN")
    deployment_constraint = coalesce_text(parsed.get("deployment_constraint"), "UNKNOWN")
    
    technical_problem = coalesce_text(parsed.get("technical_problem"), "UNKNOWN")
    
    research_opportunity = coalesce_text(parsed.get("research_opportunity"), "UNKNOWN")
    
    prototype_idea = coalesce_text(parsed.get("prototype_idea"), "UNKNOWN")
    
    problem_signature = build_problem_signature(area, pain_point, technical_problem)
    keywords = sanitize_list(parsed.get("keywords"), max_items=6, lowercase=True)
    confidence = "Medium".title()
    if confidence not in {"High", "Medium", "Low"}:
        confidence = "Medium"
    return PractitionerAnalysis(
        title=document.title,
        source_type=source_type,
        evidence_type=evidence_type,
        practitioner_role=practitioner_role,
        area=area,
        pain_point=pain_point,
        deployment_constraint=deployment_constraint,
        technical_problem=technical_problem,
        research_opportunity=research_opportunity,
        prototype_idea=prototype_idea,
        problem_signature=problem_signature,
        keywords=keywords,
        confidence=confidence,
        source_method="ollama",
        author="Unknown",
        organization="Unknown",
        publication_date=None,
        experience_context=coalesce_text(parsed.get("experience_context"), "Explicit practitioner context not available."),
    )


def build_tags(analysis: PractitionerAnalysis) -> list[str]:
    tags = ["#Practitioner", "#Blockchain"]
    area_tag = normalize_tag(analysis.area)
    if area_tag and area_tag not in tags:
        tags.append(area_tag)
    for keyword in analysis.keywords:
        tag = normalize_tag(keyword)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def build_markdown(document: PractitionerDocument, analysis: PractitionerAnalysis) -> str:
    tags = build_tags(analysis)
    keywords_line = " ".join(normalize_tag(keyword) for keyword in analysis.keywords if normalize_tag(keyword)) or "#practitioner"
    lines = [
        f"# Practitioner Signal: {analysis.title}",
        "",
        "## Layer",
        "Practitioner",
        "",
        "## Source Type",
        analysis.source_type,
        "",
        "## Evidence Type",
        analysis.evidence_type,
        "",
        "## Practitioner Role",
        analysis.practitioner_role,
        "",
        "## Confidence",
        analysis.confidence,
        "",
        "## Area",
        analysis.area,
        "",
        "## Pain Point",
        analysis.pain_point,
        "",
        "## Deployment Constraint",
        analysis.deployment_constraint,
        "",
        "## Technical Problem",
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
        "## Keywords",
        keywords_line,
        "",
        "## Source",
        document.url if document.source_mode != "Manual" or document.url.startswith("http") else "Manual",
        "",
        "## Tags",
        " ".join(tags),
    ]
    return "\n".join(lines) + "\n"


def save_markdown_output(document: PractitionerDocument, analysis: PractitionerAnalysis, output_dir: Path) -> Path:
    area_dir = output_dir / slugify(analysis.area)
    area_dir.mkdir(parents=True, exist_ok=True)
    output_path = area_dir / f"{slugify(analysis.title)}.md"
    output_path.write_text(build_markdown(document, analysis), encoding="utf-8")
    LOGGER.info("Saved practitioner markdown output to %s", output_path)
    return output_path


def fetch_all_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT title, source_type, evidence_type, practitioner_role, area, pain_point, deployment_constraint,
               technical_problem, research_opportunity, prototype_idea, problem_signature, keywords
        FROM practitioner_data
        ORDER BY area, title
        """
    ).fetchall()


def write_insights(connection: sqlite3.Connection, output_dir: Path) -> None:
    rows = fetch_all_rows(connection)
    if not rows:
        return
    pain_counter: Counter[str] = Counter()
    constraint_counter: Counter[str] = Counter()
    technical_counter: Counter[str] = Counter()
    research_counter: Counter[str] = Counter()
    prototype_counter: Counter[str] = Counter()
    hiring_counter: Counter[str] = Counter()
    area_summary: dict[str, list[str]] = defaultdict(list)
    mappings: list[str] = []

    for row in rows:
        pain_counter.update([cleaned_text(row["pain_point"]).lower()])
        constraint_counter.update([cleaned_text(row["deployment_constraint"]).lower()])
        technical_counter.update([cleaned_text(row["technical_problem"]).lower()])
        research_counter.update([cleaned_text(row["research_opportunity"]).lower()])
        prototype_counter.update([cleaned_text(row["prototype_idea"]).lower()])
        if cleaned_text(row["practitioner_role"]) == "Hiring Signal":
            for keyword in json.loads(row["keywords"] or "[]"):
                hiring_counter.update([cleaned_text(str(keyword)).lower()])
        area_summary[row["area"]].append(cleaned_text(row["technical_problem"]))
        mappings.append(
            f"- {cleaned_text(row['pain_point'])} -> {cleaned_text(row['technical_problem'])} -> {cleaned_text(row['research_opportunity'])}"
        )

    lines = ["# Practitioner Intelligence Insights", "", "## Most Common Pain Points"]
    for item, count in pain_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Repeated Deployment Constraints"])
    for item, count in constraint_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Common Technical Problems"])
    for item, count in technical_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## High-Value Research Opportunities"])
    for item, count in research_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Prototype Opportunities"])
    for item, count in prototype_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Hiring / Skill Demand Signals"])
    if hiring_counter:
        for item, count in hiring_counter.most_common(5):
            lines.append(f"- {item}: {count}")
    else:
        lines.append("- No hiring-specific signals processed yet.")

    lines.extend(["", "## Area-wise Summary"])
    for area in ["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"]:
        summary = "; ".join(area_summary.get(area, [])[:3]) or "No processed entries yet."
        lines.append(f"- {area}: {summary}")

    lines.extend(["", "## Practitioner-to-Research Mapping"])
    lines.extend(mappings[:10] or ["- No mappings yet."])

    insights_path = output_dir / "insights.md"
    insights_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("Saved practitioner insights to %s", insights_path)


def process_documents(documents: list[PractitionerDocument], output_dir: Path, db_path: Path) -> int:
    connection = db_connect(db_path)
    init_practitioner_db(connection)
    created = 0
    for document in documents:
        if practitioner_exists(connection, document.title, document.url):
            LOGGER.info("Skipping already stored practitioner source '%s'", document.title)
            continue
        analysis = analyze_document(document)
        if practitioner_exists(connection, analysis.title, document.url, analysis.pain_point, analysis.problem_signature):
            LOGGER.info("Skipping duplicate practitioner source '%s' after signature match", analysis.title)
            continue
        output_path = save_markdown_output(document, analysis, output_dir)
        save_practitioner_record(connection, document, analysis, output_path)
        created += 1
    write_insights(connection, output_dir)
    return created


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and structure practitioner intelligence for RIF.")
    parser.add_argument("--mode", choices=["auto", "url", "text"], required=True, help="Input mode")
    parser.add_argument("--area", help="Optional research area filter for auto mode")
    parser.add_argument("--sources-path", default=str(DEFAULT_SOURCES_PATH), help="Path to sources/practitioner_sources.yaml")
    parser.add_argument("--url", help="Manual URL input")
    parser.add_argument("--text", help="Manual raw text input")
    parser.add_argument("--title", help="Source or topic title for manual modes")
    parser.add_argument("--source-type", help="Optional source type override")
    parser.add_argument("--evidence-type", help="Optional evidence type override")
    parser.add_argument("--practitioner-role", help="Optional practitioner role override")
    parser.add_argument("--area", choices=["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"], help="Optional area override")
    parser.add_argument("--focus", help="Optional focus hint")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for practitioner markdown outputs")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path for practitioner memory")
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
                    args.practitioner_role,
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
                    args.practitioner_role,
                    args.area,
                    args.focus,
                )
            ]
        created = process_documents(documents, output_dir, db_path)
    except PractitionerAgentError as exc:
        LOGGER.error("Practitioner agent failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processed {len(documents)} practitioner inputs. Created {created} new markdown file(s).")
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

    agent_name = "practitioner"
    layer = "Practitioner"
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

    LOGGER.info("Starting practitioner agent run: mode=%s area=%s", mode, area)

    try:
        if mode == "configured_scan":
            sources_path = Path(payload.get("sources_path", DEFAULT_SOURCES_PATH))
            documents = build_auto_documents(sources_path, area)
        elif mode == "manual_url":
            url = payload["url"]
            documents = [
                build_manual_url_document(
                    url,
                    payload.get("title") or infer_title_from_url(url, "Manual Practitioner Signal"),
                    payload.get("source_type", "Article"),
                    payload.get("evidence_type", "Technical Discussion"),
                    payload.get("practitioner_role", "Unknown"),
                    area or payload.get("area"),
                    payload.get("focus", area or "practitioner pain point"),
                )
            ]
        else:
            text = payload["text"]
            documents = [
                build_manual_text_document(
                    text,
                    payload.get("title") or "Manual Practitioner Signal",
                    payload.get("source_type", "Manual"),
                    payload.get("evidence_type", "Practitioner Experience"),
                    payload.get("practitioner_role", "Unknown"),
                    area or payload.get("area"),
                    payload.get("focus", area or "practitioner pain point"),
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
            LOGGER.info("Finished practitioner agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
            return response

        created = process_documents(documents, output_dir, db_path)
    except PractitionerAgentError as exc:
        LOGGER.exception("Practitioner agent failed during run_agent")
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
        problem_headings=("Technical Problem", "Pain Point"),
        started_at=started_at,
        default_source=payload.get("url") or payload.get("source"),
    )
    LOGGER.info("Finished practitioner agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
    return response


if __name__ == "__main__":
    raise SystemExit(main())
