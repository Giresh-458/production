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

DEFAULT_OUTPUT_DIR = Path("outputs/investment")
DEFAULT_DB_PATH = DEFAULT_OUTPUT_DIR / "investment_memory.db"
DEFAULT_SOURCES_PATH = Path("sources/investment_sources.yaml")
RELEVANCE_KEYWORDS = [
    "investment thesis",
    "portfolio",
    "funding round",
    "market map",
    "infrastructure",
    "tokenization",
    "rwa",
    "real-world assets",
    "stablecoin",
    "settlement",
    "payments",
    "compliance",
    "custody",
    "liquidity",
    "depin",
    "decentralized physical infrastructure",
    "zk",
    "zero-knowledge",
    "proving",
    "did",
    "identity",
    "verifiable credentials",
    "mev",
    "block building",
    "transaction ordering",
    "carbon markets",
    "esg",
    "climate finance",
    "oracle",
    "interoperability",
    "protocol",
    "developer activity",
    "market structure",
]
INVESTMENT_SIGNAL_KEYWORDS = [
    "investment", "invest", "funding", "funded",

    "investment thesis",
    "portfolio pattern",
    "funded company",
    "funding round",
    "market infrastructure need",
    "technical demand",
    "capital flow",
    "capital is flowing",
    "investment areas",
    "venture",
    "market opportunity",
    "portfolio narrative",
    "implied problem being funded",
    "technical infrastructure need",
    "infrastructure gap",
    "sector outlook",
]
FOCUSED_CONTENT_KEYWORDS = [
    "investment thesis",
    "portfolio",
    "funding round",
    "market map",
    "technical infrastructure need",
    "problem being funded",
    "market structure",
    "portfolio company",
    "capital flow",
    "settlement",
    "payments",
    "tokenomics",
    "market opportunity",
    "infrastructure gap",
]
LOGGER = logging.getLogger("rif.investment_agent")


@dataclass(slots=True)
class InvestmentSource:
    name: str
    type: str
    url: str
    focus: str


@dataclass(slots=True)
class InvestmentDocument:
    title: str
    investor_or_organization: str
    source_type: str
    evidence_type: str
    area_hint: str
    focus: str
    url: str
    content: str
    source_mode: str


@dataclass(slots=True)
class InvestmentAnalysis:
    title: str
    investor_or_organization: str
    source_type: str
    evidence_type: str
    area: str
    investment_thesis: str
    problem_funded: str
    portfolio_signals: str
    technical_infrastructure_need: str
    research_opportunity: str
    prototype_idea: str
    problem_signature: str
    keywords: list[str]
    confidence: str
    source_method: str
    market_validation: str = "Weak"
    round_size: str | None = None
    investment_date: str | None = None
    stage: str | None = None
    lead_investor: str | None = None
    sector: str | None = None
    funding_velocity: str | None = None
    repeat_funding: str | None = None


class InvestmentAgentError(Exception):
    """Raised when the investment agent cannot complete a task."""


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
        raise InvestmentAgentError(
            "Automatic and URL modes require 'requests' and 'beautifulsoup4'. Install them before running those modes."
        ) from exc
    return requests, BeautifulSoup


def init_investment_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS investment_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            investor_or_organization TEXT NOT NULL,
            source_type TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            area TEXT NOT NULL,
            url TEXT NOT NULL,
            investment_thesis TEXT NOT NULL,
            problem_funded TEXT NOT NULL,
            portfolio_signals TEXT NOT NULL,
            technical_infrastructure_need TEXT NOT NULL,
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
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_investment_url ON investment_data(url)")
    connection.commit()


def investment_exists(
    connection: sqlite3.Connection,
    title: str,
    url: str,
    investor_or_organization: str,
    problem_signature: str | None = None,
) -> bool:
    if problem_signature:
        row = connection.execute(
            """
            SELECT 1 FROM investment_data
            WHERE url = ? OR (title = ? AND investor_or_organization = ?) OR (title = ? AND investor_or_organization = ? AND problem_signature = ?)
            """,
            (url, title, investor_or_organization, title, investor_or_organization, problem_signature),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT 1 FROM investment_data WHERE url = ? OR (title = ? AND investor_or_organization = ?)",
            (url, title, investor_or_organization),
        ).fetchone()
    return row is not None


def save_investment_record(
    connection: sqlite3.Connection,
    document: InvestmentDocument,
    analysis: InvestmentAnalysis,
    output_path: Path,
) -> None:
    connection.execute(
        """
        INSERT INTO investment_data (
            title,
            investor_or_organization,
            source_type,
            evidence_type,
            area,
            url,
            investment_thesis,
            problem_funded,
            portfolio_signals,
            technical_infrastructure_need,
            research_opportunity,
            prototype_idea,
            problem_signature,
            keywords,
            confidence,
            path,
            last_checked
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
                title=excluded.title,
                investor_or_organization=excluded.investor_or_organization,
                source_type=excluded.source_type,
                evidence_type=excluded.evidence_type,
                area=excluded.area,
                investment_thesis=excluded.investment_thesis,
                problem_funded=excluded.problem_funded,
                portfolio_signals=excluded.portfolio_signals,
                technical_infrastructure_need=excluded.technical_infrastructure_need,
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
            analysis.investor_or_organization,
            analysis.source_type,
            analysis.evidence_type,
            analysis.area,
            document.url,
            analysis.investment_thesis,
            analysis.problem_funded,
            analysis.portfolio_signals,
            analysis.technical_infrastructure_need,
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


def load_sources(sources_path: Path) -> list[InvestmentSource]:
    if not sources_path.exists():
        raise InvestmentAgentError(f"Investment sources file not found: {sources_path}")
    try:
        data = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise InvestmentAgentError(f"Invalid YAML in {sources_path}: {exc}") from exc
    root = data.get("investment_sources", {})
    sources: list[InvestmentSource] = []
    for _, entries in root.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = cleaned_text(str(entry.get("name", "")))
            source_type = cleaned_text(str(entry.get("type", ""))) or "vc_blog"
            url = cleaned_text(str(entry.get("url", "")))
            focus = cleaned_text(str(entry.get("focus", "")))
            if name and url:
                sources.append(InvestmentSource(name=name, type=source_type, url=url, focus=focus))
    if not sources:
        raise InvestmentAgentError("No investment sources were loaded from sources/investment_sources.yaml.")
    return apply_refresh_policy("investment", sources)


def fetch_html(url: str) -> str:
    from core.shared_crawl_manager import SharedCrawlManager
    try:
        res = SharedCrawlManager.get().fetch(url)
        if res.status_code and res.status_code >= 400:
            LOGGER.warning("HTTP %s for %s", res.status_code, url)
            raise InvestmentAgentError(f"Unable to fetch page {url}: HTTP {res.status_code}")
        LOGGER.debug("Fetched %s via %s", url, res.fetch_method)
        return res.html
    except InvestmentAgentError:
        raise
    except Exception as exc:
        raise InvestmentAgentError(f"Unable to fetch page {url}: {exc}") from exc

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
        focus_keywords=FOCUSED_CONTENT_KEYWORDS + INVESTMENT_SIGNAL_KEYWORDS,
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
        "vc_blog": "VC Blog",
        "research_blog": "Research Report",
        "investor_insights": "Research Report",
        "market_report": "Market Report",
        "research_report": "Research Report",
        "portfolio_or_blog": "Portfolio Page",
    }
    return mapping.get(raw_type, "Manual")


def infer_evidence_type(raw_type: str) -> str:
    mapping = {
        "vc_blog": "Investment Thesis",
        "research_blog": "Investor Commentary with Technical Detail",
        "investor_insights": "Sector Report",
        "market_report": "Market Map",
        "research_report": "Sector Report",
        "portfolio_or_blog": "Portfolio Pattern",
    }
    return mapping.get(raw_type, "Investment Thesis")


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


def build_auto_documents(sources_path: Path, area: str | None = None) -> list[InvestmentDocument]:
    import yaml
    raw_sources = yaml.safe_load(sources_path.read_text(encoding="utf-8")) if sources_path.exists() else {}
    from core.source_registry import extract_yaml_limits
    limits_by_url = extract_yaml_limits(raw_sources)
    from core.crawl_context import current_crawl_context
    documents: list[InvestmentDocument] = []
    for source in load_sources(sources_path):
        LOGGER.info("Collecting investment intelligence from %s", source.name)
        try:
            limits = limits_by_url.get(source.url, {})

            ctx = current_crawl_context.get(None)

            if ctx: ctx.begin_source()

            result = collect_source(source.url, keywords=('funding', 'investment', 'portfolio', 'thesis', 'venture', 'capital'), max_depth=limits.get('max_depth', 2), max_pages=limits.get('max_pages', 8), max_documents=limits.get('max_documents'), max_seconds=limits.get('max_seconds'))
            title = result.pages[0].title if result.pages else source.name
            full_text = cleaned_text(combine_pages(result, prefix=f"{source.name} {source.focus}"))
        except Exception as exc:
            LOGGER.warning("Skipping source %s: %s", source.name, exc)
            continue
        area_hint = area or infer_area(full_text, "Unscoped")
        if not is_relevant(full_text, area_hint):
            LOGGER.info("Skipping %s because the source text did not pass relevance filters.", source.name)
            continue
        documents.append(InvestmentDocument(title=title or source.name, investor_or_organization=source.name, source_type=infer_source_type(source.type), evidence_type=infer_evidence_type(source.type), area_hint=area_hint, focus=source.focus, url=source.url, content=full_text, source_mode="Automatic"))
    return documents


def build_manual_url_document(
    url: str,
    title: str | None,
    investor_or_organization: str | None,
    source_type: str | None,
    evidence_type: str | None,
    area: str | None,
    focus: str | None,
) -> InvestmentDocument:
    html = fetch_html(url)
    page_title, content = extract_page_text(html)
    resolved_title = title or page_title or cleaned_text(urlparse(url).netloc)
    resolved_org = investor_or_organization or cleaned_text(urlparse(url).netloc)
    full_text = cleaned_text(f"{resolved_title} {resolved_org} {focus or ''} {content}")
    area_hint = area or infer_area(full_text, "Unscoped")
    if not is_relevant(full_text, area_hint):
        raise InvestmentAgentError("Manual URL content does not map to the selected research areas or investment signals.")
    return InvestmentDocument(
        title=resolved_title,
        investor_or_organization=resolved_org,
        source_type=source_type or "Manual",
        evidence_type=evidence_type or "Investment Thesis",
        area_hint=area_hint,
        focus=focus or page_title,
        url=url,
        content=full_text,
        source_mode="Manual",
    )


def build_manual_text_document(
    text: str,
    title: str,
    investor_or_organization: str,
    source_type: str | None,
    evidence_type: str | None,
    area: str | None,
    focus: str | None,
) -> InvestmentDocument:
    content = cleaned_text(text)
    area_hint = area or infer_area(cleaned_text(f"{title} {investor_or_organization} {focus or ''} {content}"), "Unscoped")
    if not is_relevant(cleaned_text(f"{area_hint} {focus or ''} {content}"), area_hint):
        raise InvestmentAgentError("Manual text does not map to the selected research areas or investment signals.")
    return InvestmentDocument(
        title=title,
        investor_or_organization=investor_or_organization,
        source_type=source_type or "Manual",
        evidence_type=evidence_type or "Investment Thesis",
        area_hint=area_hint,
        focus=focus or area_hint,
        url=f"manual://{slugify(title)}/{slugify(area_hint)}",
        content=content,
        source_mode="Manual",
    )


def build_analysis_prompt(document: InvestmentDocument) -> str:
    return textwrap.dedent(
        f"""\
        Extract investment intelligence.

        Funding context: {current_funding_prompt_context.get() or "No specific funding call supplied."}

        Investor: {document.investor_or_organization}
        Source type hint: {document.source_type}
        Area hint: {document.area_hint}

        Return JSON only with keys:
        investor_or_organization
        source_type
        area
        investment_thesis
        problem_funded
        portfolio_signals
        technical_infrastructure_need
        research_opportunity
        prototype_idea
        problem_signature
        keywords
        confidence
        evidence_type

        Rules:
        - source_type: VC Blog, Investment Memo, Funding Round, Market Report, Portfolio Page, Research Report, or Manual
        - area: RWA, ESG, ZK-IoV, DID, DePIN, MEV, Stablecoins, or DigitalHealthCPS
        - keywords: list of up to 6 lowercase items
        - confidence: High, Medium, or Low
        - market_validation: Strong, Moderate, Weak, or Unknown; this is commercial validation only, never scientific proof
        - never convert investment activity into research evidence
        - only report round size, date, stage, lead investor, funding velocity, or repeat funding when supported by the source
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
            raise InvestmentAgentError("Local LLM response was not valid JSON.")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise InvestmentAgentError(f"Local LLM returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise InvestmentAgentError("Local LLM response was not a JSON object.")
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


def build_problem_signature(area: str, investment_thesis: str, technical_need: str) -> str:
    return f"{area} | {cleaned_text(investment_thesis).lower()[:60]} | {cleaned_text(technical_need).lower()[:60]}"


def fallback_analysis(document: Any) -> Any:
    raise ValueError("LLM extraction failed and synthetic fallback is disabled")


def analyze_document(document: InvestmentDocument) -> InvestmentAnalysis:
    LOGGER.info("Analyzing investment source '%s' with local llm_provider", document.title)
    try:
        parsed = parse_llm_response(llm_generate(build_analysis_prompt(document)))
    except Exception as exc:
        LOGGER.warning("Using fallback investment extraction for '%s': %s", document.title, exc)
        return fallback_analysis(document)

    investor_or_organization = document.investor_or_organization
    source_type = document.source_type
    evidence_type = document.evidence_type
    area = document.area_hint
    if area not in AREA_KEYWORDS:
        area = infer_area(json.dumps(parsed), document.area_hint or "Unscoped")
    investment_thesis = coalesce_text(parsed.get("investment_thesis"), "UNKNOWN")
    
    problem_funded = coalesce_text(parsed.get("problem_funded"), "UNKNOWN")
    
    portfolio_signals = coalesce_text(parsed.get("portfolio_signals"), "UNKNOWN")
    
    technical_need = coalesce_text(parsed.get("technical_need"), "UNKNOWN")
    
    research_opportunity = coalesce_text(parsed.get("research_opportunity"), "UNKNOWN")
    
    prototype_idea = coalesce_text(parsed.get("prototype_idea"), "UNKNOWN")
    
    problem_signature = build_problem_signature(area, investment_thesis, technical_need)
    keywords = sanitize_list(parsed.get("keywords"), max_items=6, lowercase=True)
    confidence = "Medium".title()
    if confidence not in {"High", "Medium", "Low"}:
        confidence = "Medium"
    return InvestmentAnalysis(
        title=document.title,
        investor_or_organization=investor_or_organization,
        source_type=source_type,
        evidence_type=evidence_type,
        area=area,
        investment_thesis=investment_thesis,
        problem_funded=problem_funded,
        portfolio_signals=portfolio_signals,
        technical_infrastructure_need=technical_need,
        research_opportunity=research_opportunity,
        prototype_idea=prototype_idea,
        problem_signature=problem_signature,
        keywords=keywords,
        confidence=confidence,
        source_method="ollama",
        market_validation="Unknown",
        round_size=coalesce_text(parsed.get("round_size"), None),
        investment_date=coalesce_text(parsed.get("investment_date"), None),
        stage=coalesce_text(parsed.get("stage"), None),
        lead_investor=coalesce_text(parsed.get("lead_investor"), None),
        sector=coalesce_text(parsed.get("sector"), None),
        funding_velocity=coalesce_text(parsed.get("funding_velocity"), None),
        repeat_funding=coalesce_text(parsed.get("repeat_funding"), None),
    )

def build_tags(analysis: InvestmentAnalysis) -> list[str]:
    tags = ["#Investment", "#Blockchain"]
    area_tag = normalize_tag(analysis.area)
    if area_tag and area_tag not in tags:
        tags.append(area_tag)
    for keyword in analysis.keywords:
        tag = normalize_tag(keyword)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def build_markdown(document: InvestmentDocument, analysis: InvestmentAnalysis) -> str:
    keywords_line = " ".join(normalize_tag(keyword) for keyword in analysis.keywords if normalize_tag(keyword)) or "#investment"
    tags = build_tags(analysis)
    lines = [
        f"# Investment Signal: {analysis.title}",
        "",
        "## Layer",
        "Investment",
        "",
        "## Source Type",
        analysis.source_type,
        "",
        "## Evidence Type",
        analysis.evidence_type,
        "",
        "## Investor / Organization",
        analysis.investor_or_organization,
        "",
        "## Confidence",
        analysis.confidence,
        "",
        "## Market Validation",
        analysis.market_validation,
        "",
        "## Round Size",
        analysis.round_size or "Unknown",
        "",
        "## Investment Date",
        analysis.investment_date or "Unknown",
        "",
        "## Stage",
        analysis.stage or "Unknown",
        "",
        "## Lead Investor",
        analysis.lead_investor or "Unknown",
        "",
        "## Sector",
        analysis.sector or "Unknown",
        "",
        "## Funding Velocity",
        analysis.funding_velocity or "Unknown",
        "",
        "## Repeat Funding",
        analysis.repeat_funding or "Unknown",
        "",
        "## Area",
        analysis.area,
        "",
        "## Investment Thesis",
        analysis.investment_thesis,
        "",
        "## Problem Being Funded",
        analysis.problem_funded,
        "",
        "## Portfolio / Company Signals",
        analysis.portfolio_signals,
        "",
        "## Technical Infrastructure Need",
        analysis.technical_infrastructure_need,
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


def save_markdown_output(document: InvestmentDocument, analysis: InvestmentAnalysis, output_dir: Path) -> Path:
    area_dir = output_dir / slugify(analysis.area)
    area_dir.mkdir(parents=True, exist_ok=True)
    output_path = area_dir / f"{slugify(analysis.title)}.md"
    output_path.write_text(build_markdown(document, analysis), encoding="utf-8")
    LOGGER.info("Saved investment markdown output to %s", output_path)
    return output_path


def fetch_all_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT title, investor_or_organization, source_type, evidence_type, area, investment_thesis, problem_funded,
               portfolio_signals, technical_infrastructure_need, research_opportunity, prototype_idea, problem_signature, keywords
        FROM investment_data
        ORDER BY area, title
        """
    ).fetchall()


def write_insights(connection: sqlite3.Connection, output_dir: Path) -> None:
    rows = fetch_all_rows(connection)
    if not rows:
        return
    thesis_counter: Counter[str] = Counter()
    capital_flow_counter: Counter[str] = Counter()
    technical_need_counter: Counter[str] = Counter()
    research_counter: Counter[str] = Counter()
    prototype_counter: Counter[str] = Counter()
    area_summary: dict[str, list[str]] = defaultdict(list)
    mappings: list[str] = []

    for row in rows:
        thesis_counter.update([cleaned_text(row["investment_thesis"]).lower()])
        capital_flow_counter.update([cleaned_text(row["problem_funded"]).lower()])
        technical_need_counter.update([cleaned_text(row["technical_infrastructure_need"]).lower()])
        research_counter.update([cleaned_text(row["research_opportunity"]).lower()])
        prototype_counter.update([cleaned_text(row["prototype_idea"]).lower()])
        area_summary[row["area"]].append(cleaned_text(row["technical_infrastructure_need"]))
        mappings.append(
            f"- {cleaned_text(row['investment_thesis'])} -> {cleaned_text(row['problem_funded'])} -> {cleaned_text(row['technical_infrastructure_need'])} -> {cleaned_text(row['research_opportunity'])}"
        )

    lines = ["# VC / Investment Intelligence Insights", "", "## Most Common Investment Themes"]
    for item, count in thesis_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Capital Flow Patterns"])
    for item, count in capital_flow_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Repeated Infrastructure Needs"])
    for item, count in technical_need_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## High-Value Research Opportunities"])
    for item, count in research_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Prototype Opportunities"])
    for item, count in prototype_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Area-wise Summary"])
    for area in ["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"]:
        summary = "; ".join(area_summary.get(area, [])[:3]) or "No processed entries yet."
        lines.append(f"- {area}: {summary}")

    lines.extend(["", "## Investment-to-Research Mapping"])
    lines.extend(mappings[:10] or ["- No mappings yet."])

    insights_path = output_dir / "insights.md"
    insights_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("Saved investment insights to %s", insights_path)


def process_documents(documents: list[InvestmentDocument], output_dir: Path, db_path: Path) -> int:
    connection = db_connect(db_path)
    init_investment_db(connection)
    created = 0
    for document in documents:
        if investment_exists(connection, document.title, document.url, document.investor_or_organization):
            LOGGER.info("Skipping already stored investment source '%s'", document.title)
            continue
        analysis = analyze_document(document)
        if investment_exists(connection, analysis.title, document.url, analysis.investor_or_organization, analysis.problem_signature):
            LOGGER.info("Skipping duplicate investment source '%s' after signature match", analysis.title)
            continue
        output_path = save_markdown_output(document, analysis, output_dir)
        save_investment_record(connection, document, analysis, output_path)
        created += 1
    write_insights(connection, output_dir)
    return created


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and structure investment intelligence for RIF.")
    parser.add_argument("--mode", choices=["auto", "url", "text"], required=True, help="Input mode")
    parser.add_argument("--area", help="Optional research area filter for auto mode")
    parser.add_argument("--sources-path", default=str(DEFAULT_SOURCES_PATH), help="Path to sources/investment_sources.yaml")
    parser.add_argument("--url", help="Manual URL input")
    parser.add_argument("--text", help="Manual raw text input")
    parser.add_argument("--title", help="Signal title for manual modes")
    parser.add_argument("--investor-or-organization", help="Investor or organization for manual modes")
    parser.add_argument("--source-type", help="Optional source type override")
    parser.add_argument("--evidence-type", help="Optional evidence type override")
    parser.add_argument("--area", choices=["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"], help="Optional area override")
    parser.add_argument("--focus", help="Optional focus hint")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for investment markdown outputs")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path for investment memory")
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
                    args.investor_or_organization,
                    args.source_type,
                    args.evidence_type,
                    args.area,
                    args.focus,
                )
            ]
        else:
            if not args.text or not args.title or not args.investor_or_organization:
                parser.error("--text, --title, and --investor-or-organization are required for mode=text")
            documents = [
                build_manual_text_document(
                    args.text,
                    args.title,
                    args.investor_or_organization,
                    args.source_type,
                    args.evidence_type,
                    args.area,
                    args.focus,
                )
            ]
        created = process_documents(documents, output_dir, db_path)
    except InvestmentAgentError as exc:
        LOGGER.error("Investment agent failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processed {len(documents)} investment inputs. Created {created} new markdown file(s).")
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

    agent_name = "investment"
    layer = "Investment"
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

    LOGGER.info("Starting investment agent run: mode=%s area=%s", mode, area)

    try:
        if mode == "configured_scan":
            sources_path = Path(payload.get("sources_path", DEFAULT_SOURCES_PATH))
            documents = build_auto_documents(sources_path, area)
        elif mode == "manual_url":
            url = payload["url"]
            documents = [
                build_manual_url_document(
                    url,
                    payload.get("title") or infer_title_from_url(url, "Manual Investment Signal"),
                    payload.get("investor_or_organization", "Unknown"),
                    payload.get("source_type", "Research Report"),
                    payload.get("evidence_type", "Investment Thesis"),
                    area or payload.get("area"),
                    payload.get("focus", area or "investment signal"),
                )
            ]
        else:
            text = payload["text"]
            documents = [
                build_manual_text_document(
                    text,
                    payload.get("title") or "Manual Investment Signal",
                    payload.get("investor_or_organization", "Unknown"),
                    payload.get("source_type", "Manual"),
                    payload.get("evidence_type", "Investor Commentary with Technical Detail"),
                    area or payload.get("area"),
                    payload.get("focus", area or "investment signal"),
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
            LOGGER.info("Finished investment agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
            return response

        created = process_documents(documents, output_dir, db_path)
    except InvestmentAgentError as exc:
        LOGGER.exception("Investment agent failed during run_agent")
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
        problem_headings=("Technical Infrastructure Need", "Problem Being Funded"),
        started_at=started_at,
        default_source=payload.get("url") or payload.get("source"),
    )
    LOGGER.info("Finished investment agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
    return response


if __name__ == "__main__":
    raise SystemExit(main())
