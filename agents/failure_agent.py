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

DEFAULT_OUTPUT_DIR = Path("outputs/failures")
DEFAULT_DB_PATH = DEFAULT_OUTPUT_DIR / "failure_memory.db"
DEFAULT_SOURCES_PATH = Path("sources/failure_sources.yaml")
RELEVANCE_KEYWORDS = [
    "exploit",
    "hack",
    "vulnerability",
    "incident",
    "postmortem",
    "root cause",
    "depeg",
    "oracle manipulation",
    "price manipulation",
    "bridge",
    "cross-chain",
    "governance attack",
    "liquidation",
    "mev",
    "block builder",
    "transaction ordering",
    "smart contract bug",
    "reentrancy",
    "access control",
    "proof of reserves",
    "custody",
    "reserve failure",
    "stablecoin",
    "tokenized asset",
    "rwa",
    "carbon credit",
    "double counting",
    "mrv",
    "registry",
    "fraud",
    "did",
    "identity",
    "privacy leak",
    "verifiable credential",
    "depin",
    "sensor",
    "availability",
    "reliability",
    "downtime",
]
FAILURE_TYPES = [
    "Exploit",
    "Depeg",
    "Oracle Failure",
    "Bridge Hack",
    "Governance Attack",
    "Carbon Fraud",
    "Privacy Failure",
    "Operational Failure",
]
FOCUSED_CONTENT_KEYWORDS = [
    "incident",
    "postmortem",
    "root cause",
    "what happened",
    "impact",
    "exploit",
    "hack",
    "failure",
    "vulnerability",
    "prevention",
    "mitigation",
    "loss",
    "attack",
]
LOGGER = logging.getLogger("rif.failure_agent")


@dataclass(slots=True)
class FailureSource:
    name: str
    type: str
    url: str
    focus: str


@dataclass(slots=True)
class FailureDocument:
    incident_name: str
    source_type: str
    evidence_type: str
    area_hint: str
    focus: str
    url: str
    content: str
    source_mode: str


@dataclass(slots=True)
class FailureAnalysis:
    incident_name: str
    source_type: str
    evidence_type: str
    incident_type: str
    area: str
    what_failed: str
    root_cause: str
    impact: str
    technical_problem: str
    research_opportunity: str
    prototype_idea: str
    prevention_mechanism: str
    problem_signature: str
    keywords: list[str]
    confidence: str
    incident_date: str
    estimated_loss: str
    attack_vector: str
    affected_system: str
    recurrence_signal: str
    evidence_strength: str
    source_method: str


class FailureAgentError(Exception):
    """Raised when the failure agent cannot complete a task."""


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
        raise FailureAgentError(
            "Automatic and URL modes require 'requests' and 'beautifulsoup4'. Install them before running those modes."
        ) from exc
    return requests, BeautifulSoup


def init_failure_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS failure_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            incident_type TEXT NOT NULL,
            area TEXT NOT NULL,
            url TEXT NOT NULL,
            what_failed TEXT NOT NULL,
            root_cause TEXT NOT NULL,
            impact TEXT NOT NULL,
            technical_problem TEXT NOT NULL,
            research_opportunity TEXT NOT NULL,
            prototype_idea TEXT NOT NULL,
            prevention_mechanism TEXT NOT NULL,
            problem_signature TEXT NOT NULL,
            keywords TEXT NOT NULL,
            confidence TEXT NOT NULL,
            incident_date TEXT NOT NULL DEFAULT '',
            estimated_loss TEXT NOT NULL DEFAULT '',
            attack_vector TEXT NOT NULL DEFAULT '',
            affected_system TEXT NOT NULL DEFAULT '',
            recurrence_signal TEXT NOT NULL DEFAULT '',
            evidence_strength TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL DEFAULT '',
            last_checked TEXT NOT NULL
        )
        """
    )
    for column in ("incident_date", "estimated_loss", "attack_vector", "affected_system", "recurrence_signal", "evidence_strength"):
        try:
            connection.execute(f"ALTER TABLE failure_data ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_failure_url ON failure_data(url)")
    connection.commit()


def failure_exists(
    connection: sqlite3.Connection,
    incident_name: str,
    url: str,
    incident_type: str | None = None,
    problem_signature: str | None = None,
) -> bool:
    if incident_type and problem_signature:
        row = connection.execute(
            """
            SELECT 1 FROM failure_data
            WHERE url = ? OR incident_name = ? OR (incident_name = ? AND incident_type = ? AND problem_signature = ?)
            """,
            (url, incident_name, incident_name, incident_type, problem_signature),
        ).fetchone()
    else:
        row = connection.execute("SELECT 1 FROM failure_data WHERE url = ? OR incident_name = ?", (url, incident_name)).fetchone()
    return row is not None


def save_failure_record(
    connection: sqlite3.Connection,
    document: FailureDocument,
    analysis: FailureAnalysis,
    output_path: Path,
) -> None:
    connection.execute(
        """
        INSERT INTO failure_data (
            incident_name,
            source_type,
            evidence_type,
            incident_type,
            area,
            url,
            what_failed,
            root_cause,
            impact,
            technical_problem,
            research_opportunity,
            prototype_idea,
            prevention_mechanism,
            problem_signature,
            keywords,
            confidence,
            incident_date,
            estimated_loss,
            attack_vector,
            affected_system,
            recurrence_signal,
            evidence_strength,
            path,
            last_checked
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
                incident_name=excluded.incident_name,
                source_type=excluded.source_type,
                evidence_type=excluded.evidence_type,
                incident_type=excluded.incident_type,
                area=excluded.area,
                what_failed=excluded.what_failed,
                root_cause=excluded.root_cause,
                impact=excluded.impact,
                technical_problem=excluded.technical_problem,
                research_opportunity=excluded.research_opportunity,
                prototype_idea=excluded.prototype_idea,
                prevention_mechanism=excluded.prevention_mechanism,
                problem_signature=excluded.problem_signature,
                keywords=excluded.keywords,
                confidence=excluded.confidence,
                incident_date=excluded.incident_date,
                estimated_loss=excluded.estimated_loss,
                attack_vector=excluded.attack_vector,
                affected_system=excluded.affected_system,
                recurrence_signal=excluded.recurrence_signal,
                evidence_strength=excluded.evidence_strength,
                path=excluded.path,
                last_checked=excluded.last_checked
        """,
        (
            analysis.incident_name,
            analysis.source_type,
            analysis.evidence_type,
            analysis.incident_type,
            analysis.area,
            document.url,
            analysis.what_failed,
            analysis.root_cause,
            analysis.impact,
            analysis.technical_problem,
            analysis.research_opportunity,
            analysis.prototype_idea,
            analysis.prevention_mechanism,
            analysis.problem_signature,
            json.dumps(analysis.keywords),
            analysis.confidence,
            analysis.incident_date,
            analysis.estimated_loss,
            analysis.attack_vector,
            analysis.affected_system,
            analysis.recurrence_signal,
            analysis.evidence_strength,
            str(output_path),
            datetime.now(UTC).isoformat(),
        ),
    )
    connection.commit()


def load_sources(sources_path: Path) -> list[FailureSource]:
    if not sources_path.exists():
        raise FailureAgentError(f"Failure sources file not found: {sources_path}")
    try:
        data = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise FailureAgentError(f"Invalid YAML in {sources_path}: {exc}") from exc
    root = data.get("failure_sources", {})
    sources: list[FailureSource] = []
    for _, entries in root.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = cleaned_text(str(entry.get("name", "")))
            source_type = cleaned_text(str(entry.get("type", ""))) or "incident_database"
            url = cleaned_text(str(entry.get("url", "")))
            focus = cleaned_text(str(entry.get("focus", "")))
            if name and url:
                sources.append(FailureSource(name=name, type=source_type, url=url, focus=focus))
    if not sources:
        raise FailureAgentError("No failure sources were loaded from sources/failure_sources.yaml.")
    return apply_refresh_policy("failure", sources)


def fetch_html(url: str) -> str:
    from core.shared_crawl_manager import SharedCrawlManager
    try:
        res = SharedCrawlManager.get().fetch(url)
        if res.status_code and res.status_code >= 400:
            LOGGER.warning("HTTP %s for %s", res.status_code, url)
            raise FailureAgentError(f"Unable to fetch page {url}: HTTP {res.status_code}")
        LOGGER.debug("Fetched %s via %s", url, res.fetch_method)
        return res.html
    except FailureAgentError:
        raise
    except Exception as exc:
        raise FailureAgentError(f"Unable to fetch page {url}: {exc}") from exc

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
        "incident_database": "Incident Database",
        "security_reports": "Research Report",
        "research_report": "Research Report",
        "security_blog": "Security Blog",
        "governance_forum": "Postmortem",
        "protocol_blog": "Postmortem",
        "research_blog": "Research Report",
        "registry_news": "Watchdog Report",
        "watchdog_reports": "Watchdog Report",
    }
    return mapping.get(raw_type, "Manual")


def infer_evidence_type(raw_type: str) -> str:
    mapping = {
        "incident_database": "Incident Report",
        "security_reports": "Exploit Analysis",
        "research_report": "Fraud Report",
        "security_blog": "Audit Lesson",
        "governance_forum": "Governance Failure",
        "protocol_blog": "Protocol Postmortem",
        "research_blog": "Exploit Analysis",
        "registry_news": "Fraud Report",
        "watchdog_reports": "Fraud Report",
    }
    return mapping.get(raw_type, "Incident Report")


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


def build_auto_documents(sources_path: Path, area: str | None = None) -> list[FailureDocument]:
    import yaml
    raw_sources = yaml.safe_load(sources_path.read_text(encoding="utf-8")) if sources_path.exists() else {}
    from core.source_registry import extract_yaml_limits
    limits_by_url = extract_yaml_limits(raw_sources)
    from core.crawl_context import current_crawl_context
    documents: list[FailureDocument] = []
    for source in load_sources(sources_path):
        LOGGER.info("Collecting failure incident intelligence from %s", source.name)
        try:
            limits = limits_by_url.get(source.url, {})

            ctx = current_crawl_context.get(None)

            if ctx: ctx.begin_source()

            result = collect_source(source.url, keywords=('incident', 'exploit', 'vulnerability', 'postmortem', 'outage', 'attack'), max_depth=limits.get('max_depth', 2), max_pages=limits.get('max_pages', 8), max_documents=limits.get('max_documents'), max_seconds=limits.get('max_seconds'))
            title = result.pages[0].title if result.pages else source.name
            full_text = cleaned_text(combine_pages(result, prefix=f"{source.name} {source.focus}"))
        except Exception as exc:
            LOGGER.warning("Skipping source %s: %s", source.name, exc)
            continue
        area_hint = area or infer_area(full_text, "Unscoped")
        if not is_relevant(full_text, area_hint):
            LOGGER.info("Skipping %s because the source text did not pass relevance filters.", source.name)
            continue
        documents.append(FailureDocument(incident_name=title or source.name, source_type=infer_source_type(source.type), evidence_type=infer_evidence_type(source.type), area_hint=area_hint, focus=source.focus, url=source.url, content=full_text, source_mode="Automatic"))
    return documents


def build_manual_url_document(
    url: str,
    incident_name: str | None,
    source_type: str | None,
    evidence_type: str | None,
    area: str | None,
    focus: str | None,
) -> FailureDocument:
    html = fetch_html(url)
    title, content = extract_page_text(html)
    resolved_name = incident_name or title or cleaned_text(urlparse(url).netloc)
    full_text = cleaned_text(f"{resolved_name} {focus or ''} {content}")
    area_hint = area or infer_area(full_text, "Unscoped")
    if not is_relevant(full_text, area_hint):
        raise FailureAgentError("Manual URL content does not map to the selected research areas or failure signals.")
    return FailureDocument(
        incident_name=resolved_name,
        source_type=source_type or "Manual",
        evidence_type=evidence_type or "Incident Report",
        area_hint=area_hint,
        focus=focus or title,
        url=url,
        content=full_text,
        source_mode="Manual",
    )


def build_manual_text_document(
    text: str,
    incident_name: str,
    source_type: str | None,
    evidence_type: str | None,
    area: str | None,
    focus: str | None,
) -> FailureDocument:
    content = cleaned_text(text)
    area_hint = area or infer_area(cleaned_text(f"{incident_name} {focus or ''} {content}"), "Unscoped")
    if not is_relevant(cleaned_text(f"{area_hint} {focus or ''} {content}"), area_hint):
        raise FailureAgentError("Manual text does not map to the selected research areas or failure signals.")
    return FailureDocument(
        incident_name=incident_name,
        source_type=source_type or "Manual",
        evidence_type=evidence_type or "Incident Report",
        area_hint=area_hint,
        focus=focus or area_hint,
        url=f"manual://{slugify(incident_name)}/{slugify(area_hint)}",
        content=content,
        source_mode="Manual",
    )


def build_analysis_prompt(document: FailureDocument) -> str:
    return textwrap.dedent(
        f"""\
        Extract failure intelligence.

        Funding context: {current_funding_prompt_context.get() or "No specific funding call supplied."}

        Incident name hint: {document.incident_name}
        Area hint: {document.area_hint}
        Source type hint: {document.source_type}

        Return JSON only with keys:
        incident_name
        area
        incident_type
        what_failed
        root_cause
        impact
        technical_problem
        research_opportunity
        prototype_idea
        prevention_mechanism
        problem_signature
        keywords
        confidence
        incident_date
        estimated_loss
        attack_vector
        affected_system
        recurrence_signal
        evidence_strength
        source_type
        evidence_type

        Rules:
        - incident_type: Exploit, Depeg, Oracle Failure, Bridge Hack, Governance Attack, Carbon Fraud, Privacy Failure, or Operational Failure
        - area: RWA, ESG, ZK-IoV, DID, DePIN, MEV, Stablecoins, or DigitalHealthCPS
        - keywords: list of up to 6 lowercase items
        - confidence: High, Medium, or Low
        - incident_date: date/year if explicitly supported by the source, otherwise Unknown
        - estimated_loss: loss amount/currency if explicitly supported, otherwise Unknown
        - attack_vector: concrete attack/failure mechanism, not a generic label
        - affected_system: protocol, contract, bridge, registry, oracle, infrastructure, etc.
        - recurrence_signal: whether similar failures appear recurring, or Unknown if not evidenced
        - evidence_strength: Very High for primary postmortem/audit/incident record, High for specialist security report, Medium for secondary report, Low for commentary
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
            raise FailureAgentError("Local LLM response was not valid JSON.")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise FailureAgentError(f"Local LLM returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise FailureAgentError("Local LLM response was not a JSON object.")
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


def build_problem_signature(area: str, incident_type: str, technical_problem: str) -> str:
    return f"{area} | {cleaned_text(incident_type).lower()} | {cleaned_text(technical_problem).lower()[:60]}"


def fallback_analysis(document: Any) -> Any:
    raise ValueError("LLM extraction failed and synthetic fallback is disabled")


def analyze_document(document: FailureDocument) -> FailureAnalysis:
    LOGGER.info("Analyzing failure source '%s' with local llm_provider", document.incident_name)
    try:
        parsed = parse_llm_response(llm_generate(build_analysis_prompt(document)))
    except Exception as exc:
        LOGGER.warning("Using fallback failure extraction for '%s': %s", document.incident_name, exc)
        return fallback_analysis(document)

    incident_name = document.incident_name
    area = document.area_hint
    if area not in AREA_KEYWORDS:
        area = infer_area(json.dumps(parsed), document.area_hint or "Unscoped")
    incident_type = infer_incident_type(document.content)
    if incident_type not in FAILURE_TYPES:
        incident_type = infer_incident_type(document.content)
    what_failed = coalesce_text(parsed.get("what_failed"), "UNKNOWN")
    root_cause = coalesce_text(parsed.get("root_cause"), "UNKNOWN")
    
    impact = coalesce_text(parsed.get("impact"), "UNKNOWN")
    
    technical_problem = coalesce_text(parsed.get("technical_problem"), "UNKNOWN")
    
    research_opportunity = coalesce_text(parsed.get("research_opportunity"), "UNKNOWN")
    prototype_idea = coalesce_text(parsed.get("prototype_idea"), "UNKNOWN")
    
    prevention_mechanism = coalesce_text(parsed.get("prevention_mechanism"), "UNKNOWN")
    
    problem_signature = build_problem_signature(area, incident_type, technical_problem)
    keywords = sanitize_list(parsed.get("keywords"), max_items=6, lowercase=True)
    confidence = "Medium".title()
    if confidence not in {"High", "Medium", "Low"}:
        confidence = "Medium"
    incident_date = "Unknown"
    estimated_loss = "Unknown"
    attack_vector = technical_problem
    affected_system = document.incident_name
    recurrence_signal = "Unknown"
    evidence_strength = "Medium".title()
    if evidence_strength not in {"Very High", "High", "Medium", "Low"}:
        evidence_strength = "Medium"
    source_type = document.source_type
    evidence_type = document.evidence_type
    return FailureAnalysis(
        incident_name=incident_name,
        source_type=source_type,
        evidence_type=evidence_type,
        incident_type=incident_type,
        area=area,
        what_failed=what_failed,
        root_cause=root_cause,
        impact=impact,
        technical_problem=technical_problem,
        research_opportunity=research_opportunity,
        prototype_idea=prototype_idea,
        prevention_mechanism=prevention_mechanism,
        problem_signature=problem_signature,
        keywords=keywords,
        confidence=confidence,
        incident_date=incident_date,
        estimated_loss=estimated_loss,
        attack_vector=attack_vector,
        affected_system=affected_system,
        recurrence_signal=recurrence_signal,
        evidence_strength=evidence_strength,
        source_method="ollama",
    )


def build_tags(analysis: FailureAnalysis) -> list[str]:
    tags = ["#Failure", "#Blockchain", "#Security"]
    area_tag = normalize_tag(analysis.area)
    if area_tag and area_tag not in tags:
        tags.append(area_tag)
    for keyword in analysis.keywords:
        tag = normalize_tag(keyword)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def build_markdown(document: FailureDocument, analysis: FailureAnalysis) -> str:
    keywords_line = " ".join(normalize_tag(keyword) for keyword in analysis.keywords if normalize_tag(keyword)) or "#failure"
    tags = build_tags(analysis)
    lines = [
        f"# Failure Signal: {analysis.incident_name}",
        "",
        "## Layer",
        "Failure",
        "",
        "## Source Type",
        analysis.source_type,
        "",
        "## Evidence Type",
        analysis.evidence_type,
        "",
        "## Incident Type",
        analysis.incident_type,
        "",
        "## Confidence",
        analysis.confidence,
        "",
        "## Area",
        analysis.area,
        "",
        "## Incident Date",
        analysis.incident_date,
        "",
        "## Estimated Loss",
        analysis.estimated_loss,
        "",
        "## Affected System",
        analysis.affected_system,
        "",
        "## Attack Vector",
        analysis.attack_vector,
        "",
        "## Recurrence Signal",
        analysis.recurrence_signal,
        "",
        "## Evidence Strength",
        analysis.evidence_strength,
        "",
        "## What Failed",
        analysis.what_failed,
        "",
        "## Root Cause",
        analysis.root_cause,
        "",
        "## Impact",
        analysis.impact,
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
        "## Prevention Mechanism",
        analysis.prevention_mechanism,
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


def save_markdown_output(document: FailureDocument, analysis: FailureAnalysis, output_dir: Path) -> Path:
    area_dir = output_dir / slugify(analysis.area)
    area_dir.mkdir(parents=True, exist_ok=True)
    output_path = area_dir / f"{slugify(analysis.incident_name)}.md"
    output_path.write_text(build_markdown(document, analysis), encoding="utf-8")
    LOGGER.info("Saved failure markdown output to %s", output_path)
    return output_path


def fetch_all_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT incident_name, source_type, evidence_type, incident_type, area, what_failed, root_cause, impact,
               technical_problem, research_opportunity, prototype_idea, prevention_mechanism, problem_signature, keywords
        FROM failure_data
        ORDER BY area, incident_name
        """
    ).fetchall()


def write_insights(connection: sqlite3.Connection, output_dir: Path) -> None:
    rows = fetch_all_rows(connection)
    if not rows:
        return
    type_counter: Counter[str] = Counter()
    root_counter: Counter[str] = Counter()
    technical_counter: Counter[str] = Counter()
    research_counter: Counter[str] = Counter()
    prototype_counter: Counter[str] = Counter()
    prevention_counter: Counter[str] = Counter()
    area_summary: dict[str, list[str]] = defaultdict(list)
    mappings: list[str] = []

    for row in rows:
        type_counter.update([cleaned_text(row["incident_type"]).lower()])
        root_counter.update([cleaned_text(row["root_cause"]).lower()])
        technical_counter.update([cleaned_text(row["technical_problem"]).lower()])
        research_counter.update([cleaned_text(row["research_opportunity"]).lower()])
        prototype_counter.update([cleaned_text(row["prototype_idea"]).lower()])
        prevention_counter.update([cleaned_text(row["prevention_mechanism"]).lower()])
        area_summary[row["area"]].append(cleaned_text(row["technical_problem"]))
        mappings.append(
            f"- {cleaned_text(row['what_failed'])} -> {cleaned_text(row['root_cause'])} -> {cleaned_text(row['technical_problem'])} -> {cleaned_text(row['research_opportunity'])} -> {cleaned_text(row['prototype_idea'])}"
        )

    lines = ["# Failure / Incident Intelligence Insights", "", "## Most Common Failure Types"]
    for item, count in type_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Repeated Root Causes"])
    for item, count in root_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Repeated Technical Problems"])
    for item, count in technical_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## High-Value Research Opportunities"])
    for item, count in research_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Prototype Opportunities"])
    for item, count in prototype_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Prevention Mechanism Patterns"])
    for item, count in prevention_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Area-wise Summary"])
    for area in ["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"]:
        summary = "; ".join(area_summary.get(area, [])[:3]) or "No processed entries yet."
        lines.append(f"- {area}: {summary}")

    lines.extend(["", "## Failure-to-Research Mapping"])
    lines.extend(mappings[:10] or ["- No mappings yet."])

    insights_path = output_dir / "insights.md"
    insights_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("Saved failure insights to %s", insights_path)


def process_documents(documents: list[FailureDocument], output_dir: Path, db_path: Path) -> int:
    connection = db_connect(db_path)
    init_failure_db(connection)
    created = 0
    for document in documents:
        if failure_exists(connection, document.incident_name, document.url):
            LOGGER.info("Skipping already stored failure source '%s'", document.incident_name)
            continue
        analysis = analyze_document(document)
        if failure_exists(connection, analysis.incident_name, document.url, analysis.incident_type, analysis.problem_signature):
            LOGGER.info("Skipping duplicate failure source '%s' after signature match", analysis.incident_name)
            continue
        output_path = save_markdown_output(document, analysis, output_dir)
        save_failure_record(connection, document, analysis, output_path)
        created += 1
    write_insights(connection, output_dir)
    return created


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and structure failure intelligence for RIF.")
    parser.add_argument("--mode", choices=["auto", "url", "text"], required=True, help="Input mode")
    parser.add_argument("--area", help="Optional research area filter for auto mode")
    parser.add_argument("--sources-path", default=str(DEFAULT_SOURCES_PATH), help="Path to sources/failure_sources.yaml")
    parser.add_argument("--url", help="Manual URL input")
    parser.add_argument("--text", help="Manual raw text input")
    parser.add_argument("--incident-name", help="Incident name for manual modes")
    parser.add_argument("--source-type", help="Optional source type override")
    parser.add_argument("--evidence-type", help="Optional evidence type override")
    parser.add_argument("--area", choices=["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"], help="Optional area override")
    parser.add_argument("--focus", help="Optional focus hint")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for failure markdown outputs")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path for failure memory")
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
                    args.incident_name,
                    args.source_type,
                    args.evidence_type,
                    args.area,
                    args.focus,
                )
            ]
        else:
            if not args.text or not args.incident_name:
                parser.error("--text and --incident-name are required for mode=text")
            documents = [
                build_manual_text_document(
                    args.text,
                    args.incident_name,
                    args.source_type,
                    args.evidence_type,
                    args.area,
                    args.focus,
                )
            ]
        created = process_documents(documents, output_dir, db_path)
    except FailureAgentError as exc:
        LOGGER.error("Failure agent failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processed {len(documents)} failure inputs. Created {created} new markdown file(s).")
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

    agent_name = "failure"
    layer = "Failure"
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

    LOGGER.info("Starting failure agent run: mode=%s area=%s", mode, area)

    try:
        if mode == "configured_scan":
            sources_path = Path(payload.get("sources_path", DEFAULT_SOURCES_PATH))
            documents = build_auto_documents(sources_path, area)
        elif mode == "manual_url":
            url = payload["url"]
            documents = [
                build_manual_url_document(
                    url,
                    payload.get("incident_name") or infer_title_from_url(url, "Manual Incident"),
                    payload.get("source_type", "Incident Database"),
                    payload.get("evidence_type", "Incident Report"),
                    area or payload.get("area"),
                    payload.get("focus", area or "configured incident"),
                )
            ]
        else:
            text = payload["text"]
            documents = [
                build_manual_text_document(
                    text,
                    payload.get("incident_name") or payload.get("title") or "Manual Incident",
                    payload.get("source_type", "Manual"),
                    payload.get("evidence_type", "Exploit Analysis"),
                    area or payload.get("area"),
                    payload.get("focus", area or "configured incident"),
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
            LOGGER.info("Finished failure agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
            return response

        created = process_documents(documents, output_dir, db_path)
    except FailureAgentError as exc:
        LOGGER.exception("Failure agent failed during run_agent")
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
        problem_headings=("Technical Problem", "Root Cause"),
        started_at=started_at,
        default_source=payload.get("url") or payload.get("source"),
    )
    LOGGER.info("Finished failure agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
    return response


if __name__ == "__main__":
    raise SystemExit(main())
