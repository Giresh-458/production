from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
from core.source_registry import apply_refresh_policy
from core.llm_provider import generate as llm_generate
from core.schemas import AREA_KEYWORDS

DEFAULT_OUTPUT_DIR = Path("outputs/labs")
DEFAULT_DB_PATH = DEFAULT_OUTPUT_DIR / "labs_memory.db"
DEFAULT_SOURCES_PATH = Path("sources/labs_sources.yaml")
FOCUS_AREAS = ["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS", "General"]
THEME_KEYWORDS = AREA_KEYWORDS
LOGGER = logging.getLogger("rif.lab_agent")


@dataclass(slots=True)
class LabSource:
    name: str
    url: str
    focus_pages: list[str]


@dataclass(slots=True)
class LabPage:
    url: str
    title: str
    content: str


@dataclass(slots=True)
class LabDocument:
    name: str
    institution: str
    base_url: str
    source_type: str
    pages: list[LabPage]
    funding_context: str = ""


@dataclass(slots=True)
class LabAnalysis:
    research_themes: list[str]
    active_problems: list[str]
    projects: list[dict[str, str]]
    industry_signals: list[str]
    problem_signatures: list[str]
    keywords: list[str]
    problem: str
    research_area: str
    why_important: str
    existing_solutions: str
    gap: str
    idea: str
    feasibility: str
    source_method: str
    lab: str = ""
    expertise: list[str] = field(default_factory=list)
    recent_work: list[str] = field(default_factory=list)
    relevant_papers: list[str] = field(default_factory=list)
    funding_history: list[str] = field(default_factory=list)
    potential_project_role: str = "Unknown"
    partner_fit: str = "Unknown"
    fit_score: float = 0.0


class LabAgentError(Exception):
    """Raised when the lab agent cannot complete a task."""


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "item"


def normalize_tag(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return f"#{text}" if text else ""


def matches_lab_area(document: LabDocument, area: str | None) -> bool:
    if not area:
        return True
    keywords = THEME_KEYWORDS.get(area, ())
    if not keywords:
        return True
    combined = " ".join(f"{page.title} {page.content}" for page in document.pages).lower()
    return any(keyword in combined for keyword in keywords)


def load_requests_and_bs4() -> tuple[Any, Any]:
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise LabAgentError(
            "Automatic and URL modes require 'requests' and 'beautifulsoup4'. Install them before running those modes."
        ) from exc
    return requests, BeautifulSoup


def init_labs_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS lab_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lab_name TEXT NOT NULL,
            page_url TEXT NOT NULL,
            page_title TEXT NOT NULL,
            path TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS lab_reports (
            lab_name TEXT PRIMARY KEY,
            institution TEXT NOT NULL,
            research_area TEXT NOT NULL,
            keywords TEXT NOT NULL,
            path TEXT NOT NULL,
            source_type TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_lab_pages_url ON lab_pages(page_url)")
    connection.commit()


def page_exists(connection: sqlite3.Connection, page_url: str) -> bool:
    row = connection.execute("SELECT 1 FROM lab_pages WHERE page_url = ?", (page_url,)).fetchone()
    return row is not None


def report_exists(connection: sqlite3.Connection, lab_name: str) -> bool:
    row = connection.execute("SELECT 1 FROM lab_reports WHERE lab_name = ?", (lab_name,)).fetchone()
    return row is not None


def save_lab_records(connection: sqlite3.Connection, document: LabDocument, analysis: LabAnalysis, output_path: Path) -> None:
    timestamp = datetime.now(UTC).isoformat()
    for page in document.pages:
        connection.execute(
            """
            INSERT OR IGNORE INTO lab_pages (lab_name, page_url, page_title, path, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (document.name, page.url, page.title, str(output_path), timestamp),
        )
    connection.execute(
        """
        INSERT INTO lab_reports (lab_name, institution, research_area, keywords, path, source_type, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(lab_name) DO UPDATE SET
            institution=excluded.institution,
            research_area=excluded.research_area,
            keywords=excluded.keywords,
            path=excluded.path,
            source_type=excluded.source_type,
            updated_at=excluded.updated_at
        """,
        (
            document.name,
            document.institution,
            analysis.research_area,
            json.dumps(analysis.keywords),
            str(output_path),
            document.source_type,
            timestamp,
        ),
    )
    connection.commit()


def load_sources(sources_path: Path) -> list[LabSource]:
    if not sources_path.exists():
        raise LabAgentError(f"Labs sources file not found: {sources_path}")

    try:
        data = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise LabAgentError(f"Invalid YAML in {sources_path}: {exc}") from exc

    entries = data.get("labs", [])
    sources: list[LabSource] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = cleaned_text(str(entry.get("name", "")))
        url = cleaned_text(str(entry.get("url", "")))
        focus_pages = [cleaned_text(str(page)) for page in entry.get("focus_pages", []) if cleaned_text(str(page))]
        if name and url and focus_pages:
            sources.append(LabSource(name=name, url=url, focus_pages=focus_pages))
    if not sources:
        raise LabAgentError("No lab sources were loaded from sources/labs_sources.yaml.")
    return apply_refresh_policy("lab", sources)


def fetch_html(url: str) -> str:
    from core.shared_crawl_manager import SharedCrawlManager
    try:
        res = SharedCrawlManager.get().fetch(url)
        if res.status_code and res.status_code >= 400:
            LOGGER.warning("HTTP %s for %s", res.status_code, url)
            raise LabAgentError(f"Unable to fetch page {url}: HTTP {res.status_code}")
        LOGGER.debug("Fetched %s via %s", url, res.fetch_method)
        return res.html
    except LabAgentError:
        raise
    except Exception as exc:
        raise LabAgentError(f"Unable to fetch page {url}: {exc}") from exc

def extract_page_text(html: str) -> tuple[str, str]:
    _, BeautifulSoup = load_requests_and_bs4()
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.find("h1") or soup.title
    title = cleaned_text(title_node.get_text(" ", strip=True)) if title_node else "Untitled Page"
    content = cleaned_text(" ".join(node.get_text(" ", strip=True) for node in soup.find_all(["h1", "h2", "h3", "p", "li"])))
    return title, content[:6000]


def resolve_focus_url(base_url: str, focus_page: str) -> str:
    if focus_page.startswith("http://") or focus_page.startswith("https://"):
        return focus_page
    return urljoin(base_url, focus_page)


def collect_lab_document(source: LabSource, connection: sqlite3.Connection) -> LabDocument | None:
    pages: list[LabPage] = []
    for focus_page in source.focus_pages:
        page_url = resolve_focus_url(source.url, focus_page)
        if page_exists(connection, page_url):
            LOGGER.info("Skipping already stored lab page %s", page_url)
            continue
        try:
            html = fetch_html(page_url)
        except LabAgentError as exc:
            LOGGER.warning("Skipping lab page %s: %s", page_url, exc)
            continue
        title, content = extract_page_text(html)
        if not content:
            continue
        pages.append(LabPage(url=page_url, title=title, content=content))

    if not pages:
        return None

    return LabDocument(
        name=source.name,
        institution=source.name,
        base_url=source.url,
        source_type="Automatic",
        pages=pages,
    )


def discover_lab_candidates(query: str, limit: int = 8) -> list[dict[str, object]]:
    """Discover research institutions through OpenAlex instead of a fixed allow-list."""
    requests, _ = load_requests_and_bs4()
    try:
        from core.http_client import fetch_json
        response, _ = fetch_json(
            "https://api.openalex.org/institutions",
            params={"search": query, "per-page": limit},
            timeout=(8.0, 20.0),
            retries=1,
        )
        results = response.get("results", [])
    except Exception as exc:
        raise LabAgentError(f"OpenAlex institution discovery failed: {exc}") from exc

    candidates: list[dict[str, object]] = []
    for item in results:
        candidates.append(
            {
                "id": item.get("id", ""),
                "name": item.get("display_name", ""),
                "homepage": item.get("homepage_url", ""),
                "country": (item.get("country") or {}).get("display_name", "Unknown"),
                "works_count": item.get("works_count", 0),
                "cited_by_count": item.get("cited_by_count", 0),
            }
        )
    return candidates


def build_discovered_lab_documents(funding_context: str, area: str | None = None, limit: int = 8) -> list[LabDocument]:
    candidates = discover_lab_candidates(funding_context or area or "configured research")
    documents: list[LabDocument] = []
    for candidate in candidates:
        name = cleaned_text(str(candidate.get("name", "")))
        homepage = cleaned_text(str(candidate.get("homepage", "")))
        if not name or not homepage:
            continue
        summary = (
            f"Institution: {name}\nCountry: {candidate.get('country', 'Unknown')}\n"
            f"OpenAlex works: {candidate.get('works_count', 0)}\nCitations: {candidate.get('cited_by_count', 0)}\n"
            f"Funding/research query context: {funding_context}\n"
        )
        documents.append(
            LabDocument(
                name=name,
                institution=name,
                base_url=homepage,
                source_type="OpenAlex Discovery",
                pages=[LabPage(url=str(candidate.get("id", homepage)), title=name, content=summary)],
            )
        )
    return documents[:limit]


def build_auto_documents(sources_path: Path, db_path: Path, area: str | None = None) -> list[LabDocument]:
    connection = db_connect(db_path)
    init_labs_db(connection)
    documents: list[LabDocument] = []
    for source in load_sources(sources_path):
        LOGGER.info("Collecting lab intelligence from %s", source.name)
        document = collect_lab_document(source, connection)
        if document is not None and matches_lab_area(document, area):
            documents.append(document)
        elif report_exists(connection, source.name):
            LOGGER.info("No new pages for lab '%s'; existing report already present.", source.name)
    return documents


def build_manual_url_document(url: str, lab_name: str, institution: str | None, funding_context: str = "") -> LabDocument:
    html = fetch_html(url)
    title, content = extract_page_text(html)
    return LabDocument(
        name=lab_name,
        institution=institution or lab_name,
        base_url=url,
        source_type="Manual",
        pages=[LabPage(url=url, title=title, content=content)],
        funding_context=funding_context,
    )


def build_manual_text_document(text: str, lab_name: str, institution: str | None, title: str | None, funding_context: str = "") -> LabDocument:
    page_title = title or f"{lab_name} research overview"
    manual_url = f"manual://{slugify(lab_name)}/{slugify(page_title)}"
    return LabDocument(
        name=lab_name,
        institution=institution or lab_name,
        base_url=manual_url,
        source_type="Manual",
        pages=[LabPage(url=manual_url, title=page_title, content=cleaned_text(text))],
        funding_context=funding_context,
    )


def build_analysis_prompt(document: LabDocument) -> str:
    combined = []
    for page in document.pages:
        combined.append(f"PAGE: {page.title}\nURL: {page.url}\nTEXT: {page.content[:1800]}")
    payload = "\n\n".join(combined)[:7000]
    area_text = ", ".join(FOCUS_AREAS)
    return textwrap.dedent(
        f"""\
        Task: extract blockchain lab intelligence.

        Funding context: {current_funding_prompt_context.get() or "No specific funding call supplied."}

        Lab: {document.name}
        Institution: {document.institution}

        Content:
        {payload}

        Return JSON only with keys:
        research_themes
        active_problems
        projects
        industry_signals
        problem_signatures
        keywords
        problem
        research_area
        why_important
        existing_solutions
        gap
        idea
        feasibility

        Rules:
        - research_themes: list of up to 5 short items
        - active_problems: list of up to 5 short items
        - projects: list of objects with name and goal
        - industry_signals: list of up to 4 short items
        - problem_signatures: list of up to 4 short items
        - keywords: list of up to 6 lowercase items
        - research_area: one of {area_text}
        - all string fields short and concrete
        - no markdown
        """
    )


def parse_llm_response(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise LabAgentError("Local LLM response was not valid JSON.")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LabAgentError(f"Local LLM returned invalid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise LabAgentError("Local LLM response was not a JSON object.")
    return parsed


def sanitize_list(values: object, max_items: int) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned_items: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = cleaned_text(str(value))
        if text and text.lower() not in seen:
            cleaned_items.append(text)
            seen.add(text.lower())
        if len(cleaned_items) >= max_items:
            break
    return cleaned_items


def sanitize_keywords(values: object, max_items: int = 6) -> list[str]:
    return [item.lower() for item in sanitize_list(values, max_items)]


def sanitize_projects(values: object, max_items: int = 5) -> list[dict[str, str]]:
    if not isinstance(values, list):
        return []
    projects: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        name = cleaned_text(str(value.get("name", "")))
        goal = cleaned_text(str(value.get("goal", "")))
        if name and goal:
            projects.append({"name": name, "goal": goal})
        if len(projects) >= max_items:
            break
    return projects


def infer_theme_area(text: str) -> str:
    lowered = text.lower()
    for area, keywords in THEME_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return area
    return "General"


def build_problem_signatures(keywords: list[str], themes: list[str]) -> list[str]:
    signatures = []
    for item in keywords[:2] + [theme.lower() for theme in themes[:2]]:
        cleaned_item = cleaned_text(item)
        if cleaned_item and cleaned_item not in signatures:
            signatures.append(cleaned_item)
    return signatures[:4] or ["blockchain research"]


def fallback_analysis(document: Any) -> Any:
    raise ValueError("LLM extraction failed and synthetic fallback is disabled")


def analyze_document(document: LabDocument) -> LabAnalysis:
    prompt = build_analysis_prompt(document)
    LOGGER.info("Analyzing lab '%s' with local llm_provider", document.name)
    try:
        parsed = parse_llm_response(llm_generate(prompt))
    except Exception as exc:
        LOGGER.warning("Using fallback lab extraction for '%s': %s", document.name, exc)
        return fallback_analysis(document)

    research_area = "General"
    if research_area not in FOCUS_AREAS:
        research_area = infer_research_area(json.dumps(parsed))
    research_themes = sanitize_list(parsed.get("research_themes"), 5)
    keywords = sanitize_keywords(parsed.get("keywords"), 6)
    active_problems = sanitize_list(parsed.get("active_problems"), 5)
    projects = sanitize_projects(parsed.get("projects"), 5)
    industry_signals = sanitize_list(parsed.get("industry_signals"), 4)
    combined_text = " ".join(page.content for page in document.pages)
    problem_signatures = sanitize_list(parsed.get("problem_signatures"), 4) or build_problem_signatures(keywords, research_themes)
    problem = active_problems[0] if active_problems else "", combined_text[:220]
    why_important = coalesce_text(parsed.get("why_important"), "UNKNOWN")
    
    existing_solutions = coalesce_text(parsed.get("existing_solutions"), "UNKNOWN")
    
    gap = coalesce_text(parsed.get("gap"), "UNKNOWN")
    
    idea = coalesce_text(parsed.get("idea"), "UNKNOWN")
    
    feasibility = coalesce_text(parsed.get("feasibility"), "UNKNOWN")
    
    return LabAnalysis(
        research_themes=research_themes,
        active_problems=active_problems,
        projects=projects,
        industry_signals=industry_signals,
        problem_signatures=problem_signatures,
        keywords=keywords,
        problem=problem,
        research_area=research_area,
        why_important=why_important,
        existing_solutions=existing_solutions,
        gap=gap,
        idea=idea,
        feasibility=feasibility,
        source_method="ollama",
        lab=document.name,
        expertise=sanitize_list(parsed.get("expertise"), 6),
        recent_work=sanitize_list(parsed.get("recent_work"), 6),
        relevant_papers=sanitize_list(parsed.get("relevant_papers"), 8),
        funding_history=sanitize_list(parsed.get("funding_history"), 6),
        potential_project_role="Research partner",
        partner_fit="Unknown",
        fit_score=max(0.0, min(1.0, float(parsed.get("fit_score") or 0.0))),
    )


def build_tags(analysis: LabAnalysis) -> list[str]:
    tags = ["#Labs", "#Blockchain"]
    area_tag = normalize_tag(analysis.research_area)
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


def project_lines(projects: list[dict[str, str]]) -> str:
    if not projects:
        return "- No specific projects identified."
    return "\n".join(f"- {project['name']}: {project['goal']}" for project in projects)


def source_lines(document: LabDocument) -> str:
    if document.source_type == "Manual":
        return "Manual"
    return "\n".join(f"- {page.url}" for page in document.pages)


def build_markdown(document: LabDocument, analysis: LabAnalysis, tags: list[str]) -> str:
    keywords_line = " ".join(normalize_tag(keyword) for keyword in analysis.keywords if normalize_tag(keyword)) or "#labs"
    sections = [
        f"# Lab: {document.name}",
        "",
        "## Institution",
        document.institution,
        "",
        "## Research Themes",
        bullet_lines(analysis.research_themes),
        "",
        "## Active Problems",
        bullet_lines(analysis.active_problems),
        "",
        "## Projects",
        project_lines(analysis.projects),
        "",
        "## Industry Signals",
        bullet_lines(analysis.industry_signals),
        "",
        "## Problem Signatures",
        bullet_lines(analysis.problem_signatures),
        "",
        "## Keywords",
        keywords_line,
        "",
        "## Problem",
        analysis.problem,
        "",
        "## Source",
        source_lines(document),
        "",
        "## Layer",
        "Labs",
        "",
        "## Research Area",
        analysis.research_area,
        "",
        "## Why Important",
        analysis.why_important,
        "",
        "## Existing Solutions",
        analysis.existing_solutions,
        "",
        "## Gap",
        analysis.gap,
        "",
        "## Idea",
        analysis.idea,
        "",
        "## Feasibility",
        analysis.feasibility,
        "",
        "## Tags",
        " ".join(tags),
    ]
    return "\n".join(sections) + "\n"


def save_markdown_output(document: LabDocument, analysis: LabAnalysis, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{slugify(document.name)}.md"
    output_path.write_text(build_markdown(document, analysis, build_tags(analysis)), encoding="utf-8")
    LOGGER.info("Saved lab markdown output to %s", output_path)
    return output_path


def process_documents(documents: list[LabDocument], output_dir: Path, db_path: Path) -> int:
    connection = db_connect(db_path)
    init_labs_db(connection)
    created = 0
    for document in documents:
        analysis = analyze_document(document)
        output_path = save_markdown_output(document, analysis, output_dir)
        save_lab_records(connection, document, analysis, output_path)
        created += 1
    return created


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and structure blockchain lab intelligence for RIF.")
    parser.add_argument("--mode", choices=["auto", "url", "text"], required=True, help="Input mode")
    parser.add_argument("--area", help="Optional research area filter for auto mode")
    parser.add_argument("--sources-path", default=str(DEFAULT_SOURCES_PATH), help="Path to sources/labs_sources.yaml")
    parser.add_argument("--url", help="Manual URL input")
    parser.add_argument("--text", help="Manual raw text input")
    parser.add_argument("--lab-name", help="Lab name for manual modes")
    parser.add_argument("--institution", help="Institution label for manual modes")
    parser.add_argument("--title", help="Optional title for manual text mode")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for lab markdown outputs")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path for lab memory")
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    configure_logging(args.log_level)

    output_dir = Path(args.output_dir)
    db_path = Path(args.db_path)

    try:
        if args.mode == "auto":
            documents = build_auto_documents(Path(args.sources_path), db_path, args.area)
        elif args.mode == "url":
            if not args.url or not args.lab_name:
                parser.error("--url and --lab-name are required for mode=url")
            documents = [build_manual_url_document(args.url, args.lab_name, args.institution)]
        else:
            if not args.text or not args.lab_name:
                parser.error("--text and --lab-name are required for mode=text")
            documents = [build_manual_text_document(args.text, args.lab_name, args.institution, args.title)]

        created = process_documents(documents, output_dir, db_path)
    except LabAgentError as exc:
        LOGGER.error("Lab agent failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processed {len(documents)} lab inputs. Created {created} markdown file(s).")
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

    agent_name = "lab"
    layer = "Lab"
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

    LOGGER.info("Starting lab agent run: mode=%s area=%s", mode, area)

    try:
        if mode == "configured_scan":
            sources_path = Path(payload.get("sources_path", DEFAULT_SOURCES_PATH))
            documents = build_auto_documents(sources_path, db_path, area)
        elif mode == "manual_url":
            url = payload["url"]
            lab_name = payload.get("lab_name") or infer_name_from_url(url, "Manual Lab")
            documents = [build_manual_url_document(url, lab_name, payload.get("institution"))]
        else:
            text = payload["text"]
            lab_name = payload.get("lab_name") or payload.get("title") or "Manual Lab"
            documents = [build_manual_text_document(text, lab_name, payload.get("institution"), payload.get("title"))]

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
            LOGGER.info("Finished lab agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
            return response

        created = process_documents(documents, output_dir, db_path)
    except LabAgentError as exc:
        LOGGER.exception("Lab agent failed during run_agent")
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
        problem_headings=("Problem", "Active Problems"),
        started_at=started_at,
        default_source=payload.get("url") or payload.get("source"),
    )
    LOGGER.info("Finished lab agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
    return response


if __name__ == "__main__":
    raise SystemExit(main())
