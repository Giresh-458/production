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
from core.web_collection import collect_source
from core.structured_sources import collect_structured_source
from core.web_collection import combine_pages

DEFAULT_OUTPUT_DIR = Path("outputs/data_availability")
DEFAULT_DB_PATH = DEFAULT_OUTPUT_DIR / "data_availability_memory.db"
DEFAULT_SOURCES_PATH = Path("sources/data_sources.yaml")
RELEVANCE_KEYWORDS = [
    "dataset",
    "benchmark",
    "api",
    "simulator",
    "test suite",
    "registry",
    "dashboard",
    "explorer",
    "metrics",
    "transaction data",
    "emissions data",
    "carbon registry",
    "mrv",
    "reserve report",
    "transparency",
    "proof of reserve",
    "stablecoin supply",
    "liquidity",
    "depeg",
    "mev",
    "relay",
    "block data",
    "transaction ordering",
    "did",
    "verifiable credentials",
    "conformance",
    "interoperability",
    "zk benchmark",
    "proving time",
    "verifier",
    "mobility trace",
    "v2x",
    "depin",
    "node uptime",
    "sensor data",
    "storage metrics",
]
EVAL_SIGNAL_KEYWORDS = [
    "experiment",
    "benchmark",
    "simulation",
    "prototype",
    "evaluation",
    "empirical validation",
    "trend analysis",
    "anomaly detection",
    "performance measurement",
    "conformance testing",
    "reproducibility",
    "comparison",
]
FOCUSED_CONTENT_KEYWORDS = [
    "dataset",
    "benchmark",
    "api",
    "simulator",
    "test suite",
    "metrics",
    "evaluation",
    "conformance",
    "reproducibility",
    "coverage",
    "transparency",
    "reserve report",
    "explorer",
    "registry",
]
LOGGER = logging.getLogger("rif.data_availability_agent")


@dataclass(slots=True)
class DataSource:
    name: str
    type: str
    url: str
    focus: str


@dataclass(slots=True)
class DataDocument:
    source_name: str
    source_type: str
    evidence_type: str
    area_hint: str
    focus: str
    url: str
    content: str
    source_mode: str


@dataclass(slots=True)
class DataAnalysis:
    source_name: str
    source_type: str
    evidence_type: str
    area: str
    what_it_provides: str
    usable_for: str
    access_type: str
    evaluation_possibility: str
    limitations: str
    prototype_use: str
    problem_signature: str
    keywords: list[str]
    confidence: str
    dataset_scope: str
    coverage_period: str
    license_or_access_terms: str
    benchmark_metrics: str
    benchmark_gap: str
    reproducibility: str
    source_method: str


class DataAvailabilityAgentError(Exception):
    """Raised when the data availability agent cannot complete a task."""


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
        raise DataAvailabilityAgentError(
            "Automatic and URL modes require 'requests' and 'beautifulsoup4'. Install them before running those modes."
        ) from exc
    return requests, BeautifulSoup


def init_data_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS data_availability_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            area TEXT NOT NULL,
            url TEXT NOT NULL,
            what_it_provides TEXT NOT NULL,
            usable_for TEXT NOT NULL,
            access_type TEXT NOT NULL,
            evaluation_possibility TEXT NOT NULL,
            limitations TEXT NOT NULL,
            prototype_use TEXT NOT NULL,
            problem_signature TEXT NOT NULL,
            keywords TEXT NOT NULL,
            confidence TEXT NOT NULL,
            dataset_scope TEXT NOT NULL DEFAULT '',
            coverage_period TEXT NOT NULL DEFAULT '',
            license_or_access_terms TEXT NOT NULL DEFAULT '',
            benchmark_metrics TEXT NOT NULL DEFAULT '',
            benchmark_gap TEXT NOT NULL DEFAULT '',
            reproducibility TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL DEFAULT '',
            last_checked TEXT NOT NULL
        )
        """
    )
    for column in ("dataset_scope", "coverage_period", "license_or_access_terms", "benchmark_metrics", "benchmark_gap", "reproducibility"):
        try:
            connection.execute(f"ALTER TABLE data_availability_data ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_data_url ON data_availability_data(url)")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_data_source_area ON data_availability_data(source_name, area)"
    )
    connection.commit()


def data_exists(
    connection: sqlite3.Connection,
    source_name: str,
    url: str,
    area: str | None = None,
    problem_signature: str | None = None,
) -> bool:
    if area and problem_signature:
        row = connection.execute(
            """
            SELECT 1 FROM data_availability_data
            WHERE url = ? OR (source_name = ? AND area = ?) OR (source_name = ? AND area = ? AND problem_signature = ?)
            """,
            (url, source_name, area, source_name, area, problem_signature),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT 1 FROM data_availability_data WHERE url = ? OR source_name = ?",
            (url, source_name),
        ).fetchone()
    return row is not None


def save_data_record(
    connection: sqlite3.Connection,
    document: DataDocument,
    analysis: DataAnalysis,
    output_path: Path,
) -> None:
    connection.execute(
        """
        INSERT INTO data_availability_data (
            source_name,
            source_type,
            evidence_type,
            area,
            url,
            what_it_provides,
            usable_for,
            access_type,
            evaluation_possibility,
            limitations,
            prototype_use,
            problem_signature,
            keywords,
            confidence,
            dataset_scope,
            coverage_period,
            license_or_access_terms,
            benchmark_metrics,
            benchmark_gap,
            reproducibility,
            path,
            last_checked
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
                source_name=excluded.source_name,
                source_type=excluded.source_type,
                evidence_type=excluded.evidence_type,
                area=excluded.area,
                what_it_provides=excluded.what_it_provides,
                usable_for=excluded.usable_for,
                access_type=excluded.access_type,
                evaluation_possibility=excluded.evaluation_possibility,
                limitations=excluded.limitations,
                prototype_use=excluded.prototype_use,
                problem_signature=excluded.problem_signature,
                keywords=excluded.keywords,
                confidence=excluded.confidence,
                dataset_scope=excluded.dataset_scope,
                coverage_period=excluded.coverage_period,
                license_or_access_terms=excluded.license_or_access_terms,
                benchmark_metrics=excluded.benchmark_metrics,
                benchmark_gap=excluded.benchmark_gap,
                reproducibility=excluded.reproducibility,
                path=excluded.path,
                last_checked=excluded.last_checked
        """,
        (
            analysis.source_name,
            analysis.source_type,
            analysis.evidence_type,
            analysis.area,
            document.url,
            analysis.what_it_provides,
            analysis.usable_for,
            analysis.access_type,
            analysis.evaluation_possibility,
            analysis.limitations,
            analysis.prototype_use,
            analysis.problem_signature,
            json.dumps(analysis.keywords),
            analysis.confidence,
            analysis.dataset_scope,
            analysis.coverage_period,
            analysis.license_or_access_terms,
            analysis.benchmark_metrics,
            analysis.benchmark_gap,
            analysis.reproducibility,
            str(output_path),
            datetime.now(UTC).isoformat(),
        ),
    )
    connection.commit()


def load_sources(sources_path: Path) -> list[DataSource]:
    if not sources_path.exists():
        raise DataAvailabilityAgentError(f"Data sources file not found: {sources_path}")
    try:
        data = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise DataAvailabilityAgentError(f"Invalid YAML in {sources_path}: {exc}") from exc
    root = data.get("data_sources", {})
    sources: list[DataSource] = []
    for _, entries in root.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = cleaned_text(str(entry.get("name", "")))
            source_type = cleaned_text(str(entry.get("type", ""))) or "dataset"
            url = cleaned_text(str(entry.get("url", "")))
            focus = cleaned_text(str(entry.get("focus", "")))
            if name and url:
                sources.append(DataSource(name=name, type=source_type, url=url, focus=focus))
    if not sources:
        raise DataAvailabilityAgentError("No data sources were loaded from sources/data_sources.yaml.")
    return apply_refresh_policy("data_availability", sources)


def fetch_html(url: str) -> str:
    from core.shared_crawl_manager import SharedCrawlManager
    try:
        res = SharedCrawlManager.get().fetch(url)
        if res.status_code and res.status_code >= 400:
            LOGGER.warning("HTTP %s for %s", res.status_code, url)
            raise DataAvailabilityAgentError(f"Unable to fetch page {url}: HTTP {res.status_code}")
        LOGGER.debug("Fetched %s via %s", url, res.fetch_method)
        return res.html
    except DataAvailabilityAgentError:
        raise
    except Exception as exc:
        raise DataAvailabilityAgentError(f"Unable to fetch page {url}: {exc}") from exc

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
        focus_keywords=FOCUSED_CONTENT_KEYWORDS + EVAL_SIGNAL_KEYWORDS,
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


def infer_source_type(raw_type: str) -> str:
    mapping = {
        "dashboard_or_api": "API",
        "dashboard": "Dashboard",
        "block_explorer": "Explorer",
        "documentation_or_data": "Documentation",
        "registry": "Registry",
        "registry_or_data_platform": "Registry",
        "dataset_or_api": "Dataset",
        "emissions_data_platform": "Dataset",
        "simulator": "Simulator",
        "dataset": "Dataset",
        "benchmark_repo": "Benchmark",
        "benchmark": "Benchmark",
        "test_suite": "Testnet",
        "code_examples": "Documentation",
        "dashboard_or_explorer": "Explorer",
        "explorer": "Explorer",
        "data_or_dashboard": "Dashboard",
        "analytics_dashboard": "Dashboard",
        "transparency_report": "Report",
        "protocol_dashboard": "Dashboard",
    }
    return mapping.get(raw_type, "Manual")


def infer_evidence_type(raw_type: str) -> str:
    mapping = {
        "dashboard_or_api": "API Documentation",
        "dashboard": "Dashboard",
        "block_explorer": "Dataset Description",
        "documentation_or_data": "API Documentation",
        "registry": "Registry Data",
        "registry_or_data_platform": "Registry Data",
        "dataset_or_api": "Dataset Description",
        "emissions_data_platform": "Dataset Description",
        "simulator": "Benchmark Repository",
        "dataset": "Dataset Description",
        "benchmark_repo": "Benchmark Repository",
        "benchmark": "Benchmark Repository",
        "test_suite": "Test Suite",
        "code_examples": "API Documentation",
        "dashboard_or_explorer": "Dashboard",
        "explorer": "Dataset Description",
        "data_or_dashboard": "Dashboard",
        "analytics_dashboard": "Dashboard",
        "transparency_report": "Transparency Report",
        "protocol_dashboard": "Dashboard",
    }
    return mapping.get(raw_type, "Dataset Description")


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


def build_auto_documents(sources_path: Path, area: str | None = None) -> list[DataDocument]:
    import yaml
    raw_sources = yaml.safe_load(sources_path.read_text(encoding="utf-8")) if sources_path.exists() else {}
    from core.source_registry import extract_yaml_limits
    limits_by_url = extract_yaml_limits(raw_sources)
    from core.crawl_context import current_crawl_context
    documents: list[DataDocument] = []
    for source in load_sources(sources_path):
        LOGGER.info("Collecting data availability intelligence from %s", source.name)
        try:
            structured = collect_structured_source(source.url, source.type, area)
            if structured:
                full_text = structured.text
                title = structured.title or source.name
                collected_url = structured.url
            else:
                limits = limits_by_url.get(source.url, {})

                ctx = current_crawl_context.get(None)

                if ctx: ctx.begin_source()

                result = collect_source(source.url, keywords=('dataset'), max_depth=limits.get('max_depth', 2), max_pages=limits.get('max_pages', 8), max_documents=limits.get('max_documents'), max_seconds=limits.get('max_seconds'))
                title = result.pages[0].title if result.pages else source.name
                full_text = cleaned_text(combine_pages(result, prefix=f"{source.name} {source.focus}"))
                collected_url = source.url
        except Exception as exc:
            LOGGER.warning("Skipping source %s: %s", source.name, exc)
            continue
        area_hint = area or infer_area(full_text, "Unscoped")
        if not is_relevant(full_text, area_hint):
            LOGGER.info("Skipping %s because the source text did not pass relevance filters.", source.name)
            continue
        documents.append(DataDocument(source_name=source.name, source_type=infer_source_type(source.type), evidence_type=infer_evidence_type(source.type), area_hint=area_hint, focus=source.focus, url=collected_url, content=full_text, source_mode="Automatic"))
    return documents


def build_manual_url_document(
    url: str,
    source_name: str | None,
    source_type: str | None,
    evidence_type: str | None,
    area: str | None,
    focus: str | None,
) -> DataDocument:
    html = fetch_html(url)
    title, content = extract_page_text(html)
    resolved_name = source_name or title or cleaned_text(urlparse(url).netloc)
    full_text = cleaned_text(f"{resolved_name} {focus or ''} {content}")
    area_hint = area or infer_area(full_text, "Unscoped")
    if not is_relevant(full_text, area_hint):
        raise DataAvailabilityAgentError("Manual URL content does not map to the selected research areas or evaluation signals.")
    return DataDocument(
        source_name=resolved_name,
        source_type=source_type or "Manual",
        evidence_type=evidence_type or "Dataset Description",
        area_hint=area_hint,
        focus=focus or title,
        url=url,
        content=full_text,
        source_mode="Manual",
    )


def build_manual_text_document(
    text: str,
    source_name: str,
    source_type: str | None,
    evidence_type: str | None,
    area: str | None,
    focus: str | None,
) -> DataDocument:
    content = cleaned_text(text)
    area_hint = area or infer_area(cleaned_text(f"{source_name} {focus or ''} {content}"), "Unscoped")
    if not is_relevant(cleaned_text(f"{area_hint} {focus or ''} {content}"), area_hint):
        raise DataAvailabilityAgentError("Manual text does not map to the selected research areas or evaluation signals.")
    return DataDocument(
        source_name=source_name,
        source_type=source_type or "Manual",
        evidence_type=evidence_type or "Dataset Description",
        area_hint=area_hint,
        focus=focus or area_hint,
        url=f"manual://{slugify(source_name)}/{slugify(area_hint)}",
        content=content,
        source_mode="Manual",
    )


def build_analysis_prompt(document: DataDocument) -> str:
    return textwrap.dedent(
        f"""\
        Extract data availability intelligence.

        Funding context: {current_funding_prompt_context.get() or "No specific funding call supplied."}

        Source name hint: {document.source_name}
        Area hint: {document.area_hint}
        Source type hint: {document.source_type}

        Return JSON only with keys:
        source_name
        area
        source_type
        what_it_provides
        usable_for
        access_type
        evaluation_possibility
        limitations
        prototype_use
        problem_signature
        keywords
        confidence
        dataset_scope
        coverage_period
        license_or_access_terms
        benchmark_metrics
        benchmark_gap
        reproducibility
        evidence_type

        Rules:
        - source_type: Dataset, Benchmark, API, Simulator, Testnet, Explorer, Registry, Report, Dashboard, Documentation, or Manual
        - area: RWA, ESG, ZK-IoV, DID, DePIN, MEV, Stablecoins, or DigitalHealthCPS
        - access_type: Open, Restricted, Paid, Manual, or API
        - keywords: list of up to 6 lowercase items
        - confidence: High, Medium, or Low
        - dataset_scope: what population, protocol, geography, or system the data covers
        - coverage_period: dates/versions/time span covered, or Unknown
        - license_or_access_terms: license, restrictions, or Unknown
        - benchmark_metrics: measurable metrics available, or Unknown
        - benchmark_gap: the most important missing coverage/label/metric that blocks rigorous evaluation, or None identified
        - reproducibility: High, Medium, or Low based on documented access, versioning, scripts, and repeatability
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
            raise DataAvailabilityAgentError("Local LLM response was not valid JSON.")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise DataAvailabilityAgentError(f"Local LLM returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DataAvailabilityAgentError("Local LLM response was not a JSON object.")
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


def infer_access_type(text: str, source_type: str = "") -> str:
    lowered = text.lower()
    if "api" in source_type.lower() or "api" in lowered:
        return "API"
    if "paid" in lowered or "subscription" in lowered:
        return "Paid"
    if "restricted" in lowered or "login" in lowered:
        return "Restricted"
    return "Open"


def build_problem_signature(area: str, source_name: str, evaluation_possibility: str) -> str:
    return f"{area} | {cleaned_text(source_name).lower()[:60]} | {cleaned_text(evaluation_possibility).lower()[:60]}"


def fallback_analysis(document: Any) -> Any:
    raise ValueError("LLM extraction failed and synthetic fallback is disabled")


def analyze_document(document: DataDocument) -> DataAnalysis:
    LOGGER.info("Analyzing data source '%s' with local llm_provider", document.source_name)
    try:
        parsed = parse_llm_response(llm_generate(build_analysis_prompt(document)))
    except Exception as exc:
        LOGGER.warning("Using fallback data-availability extraction for '%s': %s", document.source_name, exc)
        return fallback_analysis(document)

    source_name = document.source_name
    area = document.area_hint
    if area not in AREA_KEYWORDS:
        area = infer_area(json.dumps(parsed), document.area_hint or "Unscoped")
    source_type = document.source_type
    evidence_type = document.evidence_type
    what_it_provides = coalesce_text(parsed.get("what_it_provides"), "UNKNOWN")
    
    usable_for = coalesce_text(parsed.get("usable_for"), "UNKNOWN")
    
    access_type = infer_access_type(document.content, source_type).title()
    if access_type not in {"Open", "Restricted", "Paid", "Manual", "Api", "API"}:
        access_type = "Open"
    if access_type == "Api":
        access_type = "API"
    evaluation_possibility = coalesce_text(parsed.get("evaluation_possibility"), "UNKNOWN")
    
    limitations = coalesce_text(parsed.get("limitations"), "UNKNOWN")
    
    prototype_use = coalesce_text(parsed.get("prototype_use"), "UNKNOWN")
    
    problem_signature = build_problem_signature(area, source_name, evaluation_possibility)
    keywords = sanitize_list(parsed.get("keywords"), max_items=6, lowercase=True)
    confidence = "Medium".title()
    if confidence not in {"High", "Medium", "Low"}:
        confidence = "Medium"
    dataset_scope = "Unknown"
    coverage_period = "Unknown"
    license_or_access_terms = "Unknown"
    benchmark_metrics = "Unknown"
    benchmark_gap = "None identified"
    reproducibility = "Medium".title()
    if reproducibility not in {"High", "Medium", "Low"}:
        reproducibility = "Medium"
    return DataAnalysis(
        source_name=source_name,
        source_type=source_type,
        evidence_type=evidence_type,
        area=area,
        what_it_provides=what_it_provides,
        usable_for=usable_for,
        access_type=access_type,
        evaluation_possibility=evaluation_possibility,
        limitations=limitations,
        prototype_use=prototype_use,
        problem_signature=problem_signature,
        keywords=keywords,
        confidence=confidence,
        dataset_scope=dataset_scope,
        coverage_period=coverage_period,
        license_or_access_terms=license_or_access_terms,
        benchmark_metrics=benchmark_metrics,
        benchmark_gap=benchmark_gap,
        reproducibility=reproducibility,
        source_method="ollama",
    )


def build_tags(analysis: DataAnalysis) -> list[str]:
    tags = ["#DataAvailability", "#Blockchain", "#Benchmark"]
    area_tag = normalize_tag(analysis.area)
    if area_tag and area_tag not in tags:
        tags.append(area_tag)
    for keyword in analysis.keywords:
        tag = normalize_tag(keyword)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def build_markdown(document: DataDocument, analysis: DataAnalysis) -> str:
    keywords_line = " ".join(normalize_tag(keyword) for keyword in analysis.keywords if normalize_tag(keyword)) or "#dataset"
    tags = build_tags(analysis)
    lines = [
        f"# Data / Benchmark Signal: {analysis.source_name}",
        "",
        "## Layer",
        "DataAvailability",
        "",
        "## Source Type",
        analysis.source_type,
        "",
        "## Evidence Type",
        analysis.evidence_type,
        "",
        "## Data Source",
        f"{analysis.source_name} | {document.url if document.source_mode != 'Manual' or document.url.startswith('http') else 'Manual'}",
        "",
        "## Confidence",
        analysis.confidence,
        "",
        "## Area",
        analysis.area,
        "",
        "## What It Provides",
        analysis.what_it_provides,
        "",
        "## Usable For",
        analysis.usable_for,
        "",
        "## Access Type",
        analysis.access_type,
        "",
        "## Evaluation Possibility",
        analysis.evaluation_possibility,
        "",
        "## Limitations",
        analysis.limitations,
        "",
        "## Dataset Scope",
        analysis.dataset_scope,
        "",
        "## Coverage Period",
        analysis.coverage_period,
        "",
        "## License / Access Terms",
        analysis.license_or_access_terms,
        "",
        "## Benchmark Metrics",
        analysis.benchmark_metrics,
        "",
        "## Benchmark Gap",
        analysis.benchmark_gap,
        "",
        "## Reproducibility",
        analysis.reproducibility,
        "",
        "## Prototype Use",
        analysis.prototype_use,
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


def save_markdown_output(document: DataDocument, analysis: DataAnalysis, output_dir: Path) -> Path:
    area_dir = output_dir / slugify(analysis.area)
    area_dir.mkdir(parents=True, exist_ok=True)
    output_path = area_dir / f"{slugify(analysis.source_name)}.md"
    output_path.write_text(build_markdown(document, analysis), encoding="utf-8")
    LOGGER.info("Saved data-availability markdown output to %s", output_path)
    return output_path


def fetch_all_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT source_name, source_type, evidence_type, area, what_it_provides, usable_for, access_type,
               evaluation_possibility, limitations, prototype_use, problem_signature, keywords
        FROM data_availability_data
        ORDER BY area, source_name
        """
    ).fetchall()


def write_insights(connection: sqlite3.Connection, output_dir: Path) -> None:
    rows = fetch_all_rows(connection)
    if not rows:
        return
    strongest_counter: Counter[str] = Counter()
    limitation_counter: Counter[str] = Counter()
    evaluation_counter: Counter[str] = Counter()
    prototype_counter: Counter[str] = Counter()
    benchmark_gap_counter: Counter[str] = Counter()
    area_summary: dict[str, list[str]] = defaultdict(list)
    mappings: list[str] = []

    for row in rows:
        strongest_counter.update([cleaned_text(row["source_name"]).lower()])
        limitation_counter.update([cleaned_text(row["limitations"]).lower()])
        evaluation_counter.update([cleaned_text(row["evaluation_possibility"]).lower()])
        prototype_counter.update([cleaned_text(row["prototype_use"]).lower()])
        benchmark_gap_counter.update([cleaned_text(row["limitations"]).lower()])
        area_summary[row["area"]].append(cleaned_text(row["usable_for"]))
        mappings.append(
            f"- {cleaned_text(row['source_name'])} -> {cleaned_text(row['evaluation_possibility'])} -> {cleaned_text(row['usable_for'])} -> {cleaned_text(row['prototype_use'])}"
        )

    lines = ["# Data Availability / Benchmark Intelligence Insights", "", "## Strongest Data Sources"]
    for item, count in strongest_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Weakest / Missing Data Areas"])
    for item, count in limitation_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Evaluation Opportunities"])
    for item, count in evaluation_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Prototype Opportunities"])
    for item, count in prototype_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Benchmark Gaps"])
    for item, count in benchmark_gap_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Area-wise Summary"])
    for area in ["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"]:
        summary = "; ".join(area_summary.get(area, [])[:3]) or "No processed entries yet."
        lines.append(f"- {area}: {summary}")

    lines.extend(["", "## Data-to-Research Mapping"])
    lines.extend(mappings[:10] or ["- No mappings yet."])

    insights_path = output_dir / "insights.md"
    insights_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("Saved data-availability insights to %s", insights_path)


def process_documents(documents: list[DataDocument], output_dir: Path, db_path: Path) -> int:
    connection = db_connect(db_path)
    init_data_db(connection)
    created = 0
    for document in documents:
        if data_exists(connection, document.source_name, document.url):
            LOGGER.info("Skipping already stored data source '%s'", document.source_name)
            continue
        analysis = analyze_document(document)
        if data_exists(connection, analysis.source_name, document.url, analysis.area, analysis.problem_signature):
            LOGGER.info("Skipping duplicate data source '%s' after signature match", analysis.source_name)
            continue
        output_path = save_markdown_output(document, analysis, output_dir)
        save_data_record(connection, document, analysis, output_path)
        created += 1
    write_insights(connection, output_dir)
    return created


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and structure data-availability intelligence for RIF.")
    parser.add_argument("--mode", choices=["auto", "url", "text"], required=True, help="Input mode")
    parser.add_argument("--area", help="Optional research area filter for auto mode")
    parser.add_argument("--sources-path", default=str(DEFAULT_SOURCES_PATH), help="Path to sources/data_sources.yaml")
    parser.add_argument("--url", help="Manual URL input")
    parser.add_argument("--text", help="Manual raw text input")
    parser.add_argument("--source-name", help="Source name for manual modes")
    parser.add_argument("--source-type", help="Optional source type override")
    parser.add_argument("--evidence-type", help="Optional evidence type override")
    parser.add_argument("--area", choices=["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"], help="Optional area override")
    parser.add_argument("--focus", help="Optional focus hint")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for data-availability markdown outputs")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path for data-availability memory")
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
                    args.source_name,
                    args.source_type,
                    args.evidence_type,
                    args.area,
                    args.focus,
                )
            ]
        else:
            if not args.text or not args.source_name:
                parser.error("--text and --source-name are required for mode=text")
            documents = [
                build_manual_text_document(
                    args.text,
                    args.source_name,
                    args.source_type,
                    args.evidence_type,
                    args.area,
                    args.focus,
                )
            ]
        created = process_documents(documents, output_dir, db_path)
    except DataAvailabilityAgentError as exc:
        LOGGER.error("Data availability agent failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processed {len(documents)} data-availability inputs. Created {created} new markdown file(s).")
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

    agent_name = "data_availability"
    layer = "DataAvailability"
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

    LOGGER.info("Starting data-availability agent run: mode=%s area=%s", mode, area)

    try:
        if mode == "configured_scan":
            sources_path = Path(payload.get("sources_path", DEFAULT_SOURCES_PATH))
            documents = build_auto_documents(sources_path, area)
        elif mode == "manual_url":
            url = payload["url"]
            documents = [
                build_manual_url_document(
                    url,
                    payload.get("source_name") or infer_title_from_url(url, "Manual Data Source"),
                    payload.get("source_type", "Dataset"),
                    payload.get("evidence_type", "Dataset Description"),
                    area or payload.get("area"),
                    payload.get("focus", area or "benchmark and evaluation"),
                )
            ]
        else:
            text = payload["text"]
            documents = [
                build_manual_text_document(
                    text,
                    payload.get("source_name") or payload.get("title") or "Manual Data Source",
                    payload.get("source_type", "Manual"),
                    payload.get("evidence_type", "Dataset Description"),
                    area or payload.get("area"),
                    payload.get("focus", area or "benchmark and evaluation"),
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
            LOGGER.info("Finished data_availability agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
            return response

        created = process_documents(documents, output_dir, db_path)
    except DataAvailabilityAgentError as exc:
        LOGGER.exception("Data-availability agent failed during run_agent")
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
        problem_headings=("Usable For", "Evaluation Possibility"),
        started_at=started_at,
        default_source=payload.get("url") or payload.get("source"),
    )
    LOGGER.info("Finished data-availability agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
    return response


if __name__ == "__main__":
    raise SystemExit(main())
