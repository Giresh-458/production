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

DEFAULT_OUTPUT_DIR = Path("outputs/hackathons")
DEFAULT_DB_PATH = DEFAULT_OUTPUT_DIR / "hackathon_memory.db"
DEFAULT_SOURCES_PATH = Path("sources/hackathon_sources.yaml")

RELEVANCE_KEYWORDS = [
    "hackathon",
    "challenge",
    "bounty",
    "prize",
    "track",
    "sponsor",
    "build",
    "prototype",
    "demo",
    "submission",
    "judging criteria",
    "problem statement",
    "oracle",
    "data feeds",
    "cross-chain",
    "smart contracts",
    "compliance",
    "public goods",
    "government challenge",
]
BUILDER_SIGNAL_KEYWORDS = [
    "problem statement",
    "challenge track",
    "bounty",
    "sponsor prize",
    "required technology",
    "expected output",
    "builder requirement",
    "judging criteria",
    "ecosystem need",
    "implementation opportunity",
    "prototype",
    "demo",
]
PROBLEM_STATEMENT_KEYWORDS = [
    "problem statement",
    "challenge",
    "bounty",
    "track",
    "prize",
    "build",
    "prototype",
    "submission",
    "judging",
    "requirements",
    "expected output",
    "use case",
    "what to build",
    "builder",
    "sponsor track",
]
SOURCE_TYPE_MAP = {
    "hackathon_platform": "Hackathon",
    "ecosystem_hackathon": "Hackathon",
    "bounty_or_grants_platform": "Bounty",
    "challenge_platform": "Challenge",
    "government_challenge_platform": "Government Challenge",
    "government_or_startup_challenge_platform": "Innovation Challenge",
    "government_hackathon": "Government Challenge",
}
EVIDENCE_TYPE_MAP = {
    "hackathon_platform": "Problem Statement",
    "ecosystem_hackathon": "Sponsor Track",
    "bounty_or_grants_platform": "Bounty",
    "challenge_platform": "Problem Statement",
    "government_challenge_platform": "Problem Statement",
    "government_or_startup_challenge_platform": "Problem Statement",
    "government_hackathon": "Problem Statement",
}
TECH_VOCAB = [
    "smart contracts",
    "oracle",
    "data feeds",
    "cross-chain",
    "did",
    "verifiable credentials",
    "zk",
    "zero-knowledge proofs",
    "stablecoins",
    "depin infrastructure",
    "tokenization",
    "compliance layer",
]
LOGGER = logging.getLogger("rif.hackathon_agent")
RECURSIVE_LINK_KEYWORDS = tuple(sorted(set(RELEVANCE_KEYWORDS + PROBLEM_STATEMENT_KEYWORDS + ["track", "bounty", "prize", "sponsor", "challenge", "builder"])))
RECURSIVE_CONFIG = RecursiveCollectionConfig(
    max_depth=2,
    max_pages=10,
    link_keywords=RECURSIVE_LINK_KEYWORDS,
    relevance_threshold=2,
)


@dataclass(slots=True)
class HackathonSource:
    name: str
    type: str
    url: str
    focus: str


@dataclass(slots=True)
class HackathonDocument:
    challenge_name: str
    source_type: str
    evidence_type: str
    sponsor_or_organizer: str
    area_hint: str
    focus: str
    url: str
    content: str
    source_mode: str
    recursive_review_id: str = ""
    recursive_page_count: int = 1


@dataclass(slots=True)
class HackathonAnalysis:
    challenge_name: str
    source_type: str
    evidence_type: str
    sponsor_or_organizer: str
    area: str
    problem_statement: str
    required_technology: str
    expected_output: str
    industry_research_relevance: str
    prototype_idea: str
    funding_collaboration_signal: str
    problem_signature: str
    keywords: list[str]
    confidence: str
    source_method: str
    signal_class: str = "emerging_demand"
    evidence_strength: str = "Low"
    corroboration_required: bool = True


class HackathonAgentError(Exception):
    """Raised when the hackathon agent cannot complete a task."""


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
        raise HackathonAgentError(
            "Automatic and URL modes require 'requests' and 'beautifulsoup4'. Install them before running those modes."
        ) from exc
    return requests, BeautifulSoup


def init_hackathon_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS hackathon_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            challenge_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            sponsor_or_organizer TEXT NOT NULL,
            area TEXT NOT NULL,
            url TEXT NOT NULL,
            problem_statement TEXT NOT NULL,
            required_technology TEXT NOT NULL,
            expected_output TEXT NOT NULL,
            industry_research_relevance TEXT NOT NULL,
            prototype_idea TEXT NOT NULL,
            funding_collaboration_signal TEXT NOT NULL,
            problem_signature TEXT NOT NULL,
            keywords TEXT NOT NULL,
            confidence TEXT NOT NULL,
            path TEXT NOT NULL DEFAULT '',
            last_checked TEXT NOT NULL
        )
        """
    )
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_hackathon_url ON hackathon_data(url)")
    connection.commit()


def hackathon_exists(
    connection: sqlite3.Connection,
    challenge_name: str,
    url: str,
    sponsor_or_organizer: str,
    problem_signature: str | None = None,
) -> bool:
    if problem_signature:
        row = connection.execute(
            """
            SELECT 1 FROM hackathon_data
            WHERE url = ? OR (challenge_name = ? AND sponsor_or_organizer = ?) OR (challenge_name = ? AND sponsor_or_organizer = ? AND problem_signature = ?)
            """,
            (url, challenge_name, sponsor_or_organizer, challenge_name, sponsor_or_organizer, problem_signature),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT 1 FROM hackathon_data WHERE url = ? OR (challenge_name = ? AND sponsor_or_organizer = ?)",
            (url, challenge_name, sponsor_or_organizer),
        ).fetchone()
    return row is not None


def save_hackathon_record(
    connection: sqlite3.Connection,
    document: HackathonDocument,
    analysis: HackathonAnalysis,
    output_path: Path,
) -> None:
    connection.execute(
        """
        INSERT INTO hackathon_data (
            challenge_name,
            source_type,
            evidence_type,
            sponsor_or_organizer,
            area,
            url,
            problem_statement,
            required_technology,
            expected_output,
            industry_research_relevance,
            prototype_idea,
            funding_collaboration_signal,
            problem_signature,
            keywords,
            confidence,
            path,
            last_checked
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
                challenge_name=excluded.challenge_name,
                source_type=excluded.source_type,
                evidence_type=excluded.evidence_type,
                sponsor_or_organizer=excluded.sponsor_or_organizer,
                area=excluded.area,
                problem_statement=excluded.problem_statement,
                required_technology=excluded.required_technology,
                expected_output=excluded.expected_output,
                industry_research_relevance=excluded.industry_research_relevance,
                prototype_idea=excluded.prototype_idea,
                funding_collaboration_signal=excluded.funding_collaboration_signal,
                problem_signature=excluded.problem_signature,
                keywords=excluded.keywords,
                confidence=excluded.confidence,
                path=excluded.path,
                last_checked=excluded.last_checked
        """,
        (
            analysis.challenge_name,
            analysis.source_type,
            analysis.evidence_type,
            analysis.sponsor_or_organizer,
            analysis.area,
            document.url,
            analysis.problem_statement,
            analysis.required_technology,
            analysis.expected_output,
            analysis.industry_research_relevance,
            analysis.prototype_idea,
            analysis.funding_collaboration_signal,
            analysis.problem_signature,
            json.dumps(analysis.keywords),
            analysis.confidence,
            str(output_path),
            datetime.now(UTC).isoformat(),
        ),
    )
    connection.commit()


def load_sources(sources_path: Path) -> list[HackathonSource]:
    if not sources_path.exists():
        raise HackathonAgentError(f"Hackathon sources file not found: {sources_path}")
    try:
        data = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise HackathonAgentError(f"Invalid YAML in {sources_path}: {exc}") from exc

    root = data.get("hackathon_sources", {})
    sources: list[HackathonSource] = []
    for _, entries in root.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = cleaned_text(str(entry.get("name", "")))
            url = cleaned_text(str(entry.get("url", "")))
            raw_type = cleaned_text(str(entry.get("type", ""))) or "hackathon_platform"
            focus = cleaned_text(str(entry.get("focus", "")))
            if name and url:
                sources.append(HackathonSource(name=name, type=raw_type, url=url, focus=focus))
    if not sources:
        raise HackathonAgentError("No hackathon sources were loaded from sources/hackathon_sources.yaml.")
    return apply_refresh_policy("hackathon", sources)


def fetch_html(url: str) -> str:
    from core.shared_crawl_manager import SharedCrawlManager
    try:
        res = SharedCrawlManager.get().fetch(url)
        if res.status_code and res.status_code >= 400:
            LOGGER.warning("HTTP %s for %s", res.status_code, url)
            raise HackathonAgentError(f"Unable to fetch page {url}: HTTP {res.status_code}")
        LOGGER.debug("Fetched %s via %s", url, res.fetch_method)
        return res.html
    except HackathonAgentError:
        raise
    except Exception as exc:
        raise HackathonAgentError(f"Unable to fetch page {url}: {exc}") from exc

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
        focus_keywords=PROBLEM_STATEMENT_KEYWORDS + BUILDER_SIGNAL_KEYWORDS,
        area_keywords=[keyword for keywords in AREA_KEYWORDS.values() for keyword in keywords],
        min_score=3,
        max_chars=5000,
    ) or extract_problem_statement_text(blocks, title)
    return title, (focused_content or content)[:7000]


def _keyword_score(text: str, keywords: list[str]) -> int:
    lowered = text.lower()
    return sum(1 for keyword in keywords if keyword in lowered)


def extract_problem_statement_text(blocks: list[str], title: str) -> str:
    selected: list[str] = []
    seen: set[str] = set()
    for index, block in enumerate(blocks):
        if len(block) < 30:
            continue
        signal_score = _keyword_score(block, RELEVANCE_KEYWORDS)
        problem_score = _keyword_score(block, PROBLEM_STATEMENT_KEYWORDS)
        area_score = _keyword_score(block, [keyword for keywords in AREA_KEYWORDS.values() for keyword in keywords])
        if problem_score == 0:
            continue
        if signal_score + area_score < 2:
            continue
        for neighbor_index in (index - 1, index, index + 1):
            if 0 <= neighbor_index < len(blocks):
                snippet = cleaned_text(blocks[neighbor_index])
                if snippet and snippet not in seen:
                    selected.append(snippet)
                    seen.add(snippet)
        if len(selected) >= 18:
            break

    if not selected:
        return ""
    merged = cleaned_text(f"{title} {' '.join(selected)}")
    return merged[:5000]


def infer_area(text: str, default: str = "Unscoped") -> str:
    lowered = text.lower()
    if default in AREA_KEYWORDS and any(keyword in lowered for keyword in AREA_KEYWORDS[default]):
        return default
    for area, keywords in AREA_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return area
    return default


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


def infer_source_type(raw_type: str) -> str:
    return SOURCE_TYPE_MAP.get(raw_type, "Hackathon")


def infer_evidence_type(raw_type: str) -> str:
    return EVIDENCE_TYPE_MAP.get(raw_type, "Problem Statement")


def build_auto_documents(sources_path: Path, area: str | None = None) -> list[HackathonDocument]:
    import yaml
    raw_sources = yaml.safe_load(sources_path.read_text(encoding="utf-8")) if sources_path.exists() else {}
    from core.source_registry import extract_yaml_limits
    limits_by_url = extract_yaml_limits(raw_sources)
    from core.crawl_context import current_crawl_context
    documents: list[HackathonDocument] = []
    for source in load_sources(sources_path):
        LOGGER.info("Collecting hackathon intelligence from %s", source.name)
        try:
            html = fetch_html(source.url)
        except HackathonAgentError as exc:
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
        if not content:
            LOGGER.info("Skipping %s because no focused challenge/problem text was extracted.", source.name)
            update_recursive_review(
                review["review_id"],
                outcome="filtered_out_empty",
                outcome_reason="no focused challenge/problem text was extracted",
                artifact_title=title,
                selected_area=area or "",
            )
            continue
        full_text = cleaned_text(f"{source.name} {source.focus} {title} {content}")
        area_hint = area or infer_area(full_text, "Unscoped")
        if not is_relevant(full_text, area_hint):
            LOGGER.info("Skipping %s because the source text is not hackathon-relevant enough.", source.name)
            update_recursive_review(
                review["review_id"],
                outcome="filtered_out_irrelevant",
                outcome_reason="document did not pass hackathon relevance filters",
                artifact_title=title,
                selected_area=area_hint or "",
            )
            continue
        documents.append(
            HackathonDocument(
                challenge_name=title or source.name,
                source_type=infer_source_type(source.type),
                evidence_type=infer_evidence_type(source.type),
                sponsor_or_organizer=source.name,
                area_hint=area_hint,
                focus=source.focus,
                url=source.url,
                content=full_text,
                source_mode="Automatic",
                recursive_review_id=review["review_id"],
                recursive_page_count=len(pages),
            )
        )
        update_recursive_review(
            review["review_id"],
            outcome="accepted_for_collection",
            outcome_reason="document passed hackathon relevance filters",
            artifact_title=title,
            selected_area=area_hint or "",
        )
    return documents


def build_manual_url_document(
    url: str,
    challenge_name: str | None,
    source_type: str | None,
    evidence_type: str | None,
    sponsor_or_organizer: str | None,
    area: str | None,
    focus: str | None,
) -> HackathonDocument:
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
    title, content = combine_recursive_pages(pages, root_name=challenge_name or cleaned_text(urlparse(url).netloc), focus=focus or "")
    resolved_name = challenge_name or title or cleaned_text(urlparse(url).netloc)
    resolved_org = sponsor_or_organizer or cleaned_text(urlparse(url).netloc)
    full_text = cleaned_text(f"{resolved_name} {resolved_org} {focus or ''} {content}")
    area_hint = area or infer_area(full_text, "Unscoped")
    if not is_relevant(full_text, area_hint):
        raise HackathonAgentError("Manual URL content does not map to the selected research areas or hackathon signals.")
    return HackathonDocument(
        challenge_name=resolved_name,
        source_type=source_type or "Hackathon",
        evidence_type=evidence_type or "Problem Statement",
        sponsor_or_organizer=resolved_org,
        area_hint=area_hint,
        focus=focus or title,
        url=url,
        content=full_text,
        source_mode="Manual",
    )


def build_manual_text_document(
    text: str,
    challenge_name: str,
    source_type: str | None,
    evidence_type: str | None,
    sponsor_or_organizer: str | None,
    area: str | None,
    focus: str | None,
) -> HackathonDocument:
    content = cleaned_text(text)
    resolved_org = sponsor_or_organizer or "Manual"
    area_hint = area or infer_area(cleaned_text(f"{challenge_name} {resolved_org} {focus or ''} {content}"), "Unscoped")
    if not is_relevant(cleaned_text(f"{area_hint} {focus or ''} {content}"), area_hint):
        raise HackathonAgentError("Manual text does not map to the selected research areas or hackathon signals.")
    return HackathonDocument(
        challenge_name=challenge_name,
        source_type=source_type or "Manual",
        evidence_type=evidence_type or "Problem Statement",
        sponsor_or_organizer=resolved_org,
        area_hint=area_hint,
        focus=focus or area_hint,
        url=f"manual://{slugify(challenge_name)}/{slugify(area_hint)}",
        content=content,
        source_mode="Manual",
    )


def build_analysis_prompt(document: HackathonDocument) -> str:
    area_text = ", ".join(AREA_KEYWORDS.keys())
    return textwrap.dedent(
        f"""\
        Extract hackathon intelligence.

        Funding context: {current_funding_prompt_context.get() or "No specific funding call supplied."}

        Challenge: {document.challenge_name}
        Organizer: {document.sponsor_or_organizer}
        Source type: {document.source_type}

        Content:
        {document.content[:5000]}

        Return JSON only with keys:
        challenge_name
        source_type
        area
        problem_statement
        sponsor_or_organizer
        required_technology
        expected_output
        industry_research_relevance
        prototype_idea
        funding_collaboration_signal
        keywords
        confidence

        Rules:
        - area: one of {area_text}
        - problem_statement: 1 short sentence
        - required_technology: short phrase
        - expected_output: short phrase
        - industry_research_relevance: 1 short sentence
        - prototype_idea: 1 short sentence
        - funding_collaboration_signal: 1 short sentence
        - keywords: up to 5 lowercase items
        - confidence: High, Medium, or Low
        - signal_class: emerging_demand
        - evidence_strength: Low by default; Medium only if the challenge describes a repeated operational failure or deployment blocker
        - corroboration_required: true unless the source explicitly contains strong failure/deployment evidence
        - do not call a hackathon challenge a novel research gap by itself
        - no markdown
        """
    )


def parse_llm_response(text: str) -> dict[str, object]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise HackathonAgentError("Local LLM response was not valid JSON.")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise HackathonAgentError(f"Local LLM returned invalid JSON: {exc}") from exc

    required = (
        "challenge_name",
        "source_type",
        "area",
        "problem_statement",
        "sponsor_or_organizer",
        "required_technology",
        "expected_output",
        "industry_research_relevance",
        "prototype_idea",
        "funding_collaboration_signal",
        "keywords",
        "confidence",
    )
    missing = [key for key in required if key not in parsed]
    if missing:
        raise HackathonAgentError(f"Local LLM response missing keys: {', '.join(missing)}")
    return parsed


def sanitize_list(values: object, max_items: int = 5) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = cleaned_text(str(value)).lower()
        if text and text not in seen:
            cleaned.append(text)
            seen.add(text)
        if len(cleaned) >= max_items:
            break
    return cleaned


def build_problem_signature(area: str, technology: str, statement: str) -> str:
    tech = cleaned_text(technology).lower()
    statement_keywords = [token for token in re.split(r"[^a-zA-Z0-9]+", statement.lower()) if len(token) > 3][:3]
    parts = [area.lower()]
    if tech:
        parts.append(tech)
    parts.extend(statement_keywords[:2])
    return " | ".join(dict.fromkeys(parts))


def detect_required_technology(text: str) -> str:
    lowered = text.lower()
    matched = [item for item in TECH_VOCAB if item in lowered or item.replace("-", " ") in lowered]
    if matched:
        return ", ".join(matched[:4])
    return "smart contracts"


def fallback_analysis(document: Any) -> Any:
    raise ValueError("LLM extraction failed and synthetic fallback is disabled")


def analyze_document(document: HackathonDocument) -> HackathonAnalysis:
    prompt = build_analysis_prompt(document)
    LOGGER.info("Analyzing hackathon signal '%s' with local llm_provider", document.challenge_name)
    try:
        response_text = llm_generate(prompt)
        parsed = parse_llm_response(response_text)
    except Exception as exc:
        LOGGER.warning("Using fallback hackathon extraction for '%s': %s", document.challenge_name, exc)
        return fallback_analysis(document)

    area = cleaned_text(str(parsed["area"])) or document.area_hint
    if area not in AREA_KEYWORDS:
        area = document.area_hint
    problem_statement = cleaned_text(str(parsed["problem_statement"]))
    required_technology = cleaned_text(str(parsed["required_technology"])) or detect_required_technology(document.content)
    return HackathonAnalysis(
        challenge_name=cleaned_text(str(parsed["challenge_name"])) or document.challenge_name,
        source_type=cleaned_text(str(parsed["source_type"])) or document.source_type,
        evidence_type=document.evidence_type,
        sponsor_or_organizer=cleaned_text(str(parsed["sponsor_or_organizer"])) or document.sponsor_or_organizer,
        area=area,
        problem_statement=problem_statement,
        required_technology=required_technology,
        expected_output=cleaned_text(str(parsed["expected_output"])) or "prototype demo",
        industry_research_relevance=cleaned_text(str(parsed["industry_research_relevance"])) or "Prototype-oriented ecosystem need.",
        prototype_idea=cleaned_text(str(parsed["prototype_idea"])) or f"Build a small {area} prototype.",
        funding_collaboration_signal=cleaned_text(str(parsed["funding_collaboration_signal"])) or f"Sponsor signal from {document.sponsor_or_organizer}.",
        problem_signature=build_problem_signature(area, required_technology, problem_statement),
        keywords=sanitize_list(parsed.get("keywords"), max_items=5),
        confidence=cleaned_text(str(parsed["confidence"])) or "Medium",
        source_method="ollama",
        signal_class="emerging_demand",
        evidence_strength=cleaned_text(str(parsed.get("evidence_strength", "Low"))) or "Low",
        corroboration_required=bool(parsed.get("corroboration_required", True)),
    )


def save_markdown_output(document: HackathonDocument, analysis: HackathonAnalysis, output_dir: Path) -> Path:
    area_dir = output_dir / slugify(analysis.area)
    area_dir.mkdir(parents=True, exist_ok=True)
    output_path = area_dir / f"{slugify(analysis.challenge_name)}.md"

    keyword_tags = " ".join(normalize_tag(keyword) for keyword in analysis.keywords if normalize_tag(keyword))
    content = textwrap.dedent(
        f"""\
        # Hackathon Signal: {analysis.challenge_name}

        ## Layer
        Hackathon

        ## Source Type
        {analysis.source_type}

        ## Evidence Type
        {analysis.evidence_type}

        ## Sponsor / Organizer
        {analysis.sponsor_or_organizer}

        ## Confidence
        {analysis.confidence}

        ## Area
        {analysis.area}

        ## Problem Statement
        {analysis.problem_statement}

        ## Required Technology
        {analysis.required_technology}

        ## Expected Output
        {analysis.expected_output}

        ## Industry / Research Relevance
        {analysis.industry_research_relevance}

        ## Prototype Idea
        {analysis.prototype_idea}

        ## Funding / Collaboration Signal
        {analysis.funding_collaboration_signal}

        ## Problem Signature
        {analysis.problem_signature}

        ## Keywords
        {keyword_tags}

        ## Source
        {document.url}

        ## Tags
        #Hackathon #Blockchain #Prototype {normalize_tag(analysis.area)}

        ## Extraction Method
        {analysis.source_method}
        """
    ).strip() + "\n"
    output_path.write_text(content, encoding="utf-8")
    LOGGER.info("Saved hackathon markdown output to %s", output_path)
    return output_path


def write_insights(connection: sqlite3.Connection, output_dir: Path) -> None:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT area, problem_statement, sponsor_or_organizer, required_technology, prototype_idea, funding_collaboration_signal
        FROM hackathon_data
        ORDER BY last_checked DESC
        """
    ).fetchall()

    problem_counter = Counter(row["problem_statement"] for row in rows if row["problem_statement"])
    sponsor_counter = Counter(row["sponsor_or_organizer"] for row in rows if row["sponsor_or_organizer"])
    tech_counter = Counter()
    prototype_counter = Counter(row["prototype_idea"] for row in rows if row["prototype_idea"])
    funding_counter = Counter(row["funding_collaboration_signal"] for row in rows if row["funding_collaboration_signal"])
    area_map: dict[str, list[str]] = defaultdict(list)

    for row in rows:
        for chunk in row["required_technology"].split(","):
            tech = cleaned_text(chunk)
            if tech:
                tech_counter[tech] += 1
        if row["area"] and row["problem_statement"]:
            area_map[row["area"]].append(row["problem_statement"])

    def bullet_lines(counter: Counter, fallback: str) -> list[str]:
        if not counter:
            return [f"- {fallback}"]
        return [f"- {item}" for item, _ in counter.most_common(5)]

    lines = [
        "# Hackathon / Challenge Intelligence Insights",
        "",
        "## Most Common Prototype Themes",
        *bullet_lines(tech_counter, "No prototype technologies captured yet."),
        "",
        "## Repeated Problem Statements",
        *bullet_lines(problem_counter, "No repeated problem statements captured yet."),
        "",
        "## Sponsor / Ecosystem Patterns",
        *bullet_lines(sponsor_counter, "No sponsor patterns captured yet."),
        "",
        "## Common Required Technologies",
        *bullet_lines(tech_counter, "No required technologies captured yet."),
        "",
        "## Prototype Opportunities",
        *bullet_lines(prototype_counter, "No prototype opportunities captured yet."),
        "",
        "## Funding / Collaboration Signals",
        *bullet_lines(funding_counter, "No funding or collaboration signals captured yet."),
        "",
        "## Area-wise Summary",
    ]
    for area in AREA_KEYWORDS:
        samples = area_map.get(area, [])
        summary = samples[0] if samples else "No entries yet."
        lines.extend([f"- {area}: {summary}"])

    lines.extend(
        [
            "",
            "## Hackathon-to-Research Mapping",
        ]
    )
    if rows:
        for row in rows[:5]:
            lines.append(
                f"- {row['problem_statement']} -> {row['required_technology']} -> {row['prototype_idea']} -> {row['funding_collaboration_signal']}"
            )
    else:
        lines.append("- No hackathon mappings captured yet.")

    output_dir.mkdir(parents=True, exist_ok=True)
    insights_path = output_dir / "insights.md"
    insights_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("Saved hackathon insights to %s", insights_path)


def process_documents(documents: list[HackathonDocument], output_dir: Path, db_path: Path) -> int:
    connection = db_connect(db_path)
    init_hackathon_db(connection)
    created = 0
    for document in documents:
        analysis = analyze_document(document)
        if hackathon_exists(
            connection,
            analysis.challenge_name,
            document.url,
            analysis.sponsor_or_organizer,
            analysis.problem_signature,
        ):
            LOGGER.info("Skipping duplicate hackathon signal '%s'", analysis.challenge_name)
            continue
        output_path = save_markdown_output(document, analysis, output_dir)
        save_hackathon_record(connection, document, analysis, output_path)
        created += 1
    write_insights(connection, output_dir)
    return created


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and structure hackathon intelligence for RIF.")
    parser.add_argument("--mode", choices=["auto", "url", "text"], required=True, help="Input mode")
    parser.add_argument("--area", help="Optional research area filter for auto mode")
    parser.add_argument("--sources-path", default=str(DEFAULT_SOURCES_PATH), help="Path to sources/hackathon_sources.yaml")
    parser.add_argument("--url", help="Manual URL input")
    parser.add_argument("--text", help="Manual raw text input")
    parser.add_argument("--challenge-name", help="Hackathon or challenge name for manual modes")
    parser.add_argument("--source-type", help="Optional source type override")
    parser.add_argument("--evidence-type", help="Optional evidence type override")
    parser.add_argument("--sponsor", help="Sponsor or organizer for manual modes")
    parser.add_argument("--focus", help="Optional focus text for manual modes")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for hackathon markdown outputs")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path for hackathon memory")
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
                    args.challenge_name,
                    args.source_type,
                    args.evidence_type,
                    args.sponsor,
                    args.area,
                    args.focus,
                )
            ]
        else:
            if not args.text or not args.challenge_name:
                parser.error("--text and --challenge-name are required for mode=text")
            documents = [
                build_manual_text_document(
                    args.text,
                    args.challenge_name,
                    args.source_type,
                    args.evidence_type,
                    args.sponsor,
                    args.area,
                    args.focus,
                )
            ]

        created = process_documents(documents, output_dir, db_path)
    except HackathonAgentError as exc:
        LOGGER.error("Hackathon agent failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processed {len(documents)} hackathon inputs. Created {created} new markdown file(s).")
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

    agent_name = "hackathon"
    layer = "Hackathon"
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
    documents: list[HackathonDocument] = []

    LOGGER.info("Starting hackathon agent run: mode=%s area=%s", mode, area)

    try:
        if mode == "configured_scan":
            sources_path = Path(payload.get("sources_path", DEFAULT_SOURCES_PATH))
            documents = build_auto_documents(sources_path, area)
        elif mode == "manual_url":
            url = payload["url"]
            documents = [
                build_manual_url_document(
                    url,
                    payload.get("challenge_name") or payload.get("title") or infer_title_from_url(url, "Manual Hackathon Signal"),
                    payload.get("source_type"),
                    payload.get("evidence_type"),
                    payload.get("sponsor") or payload.get("source"),
                    area or payload.get("area"),
                    payload.get("focus"),
                )
            ]
        else:
            text = payload["text"]
            documents = [
                build_manual_text_document(
                    text,
                    payload.get("challenge_name") or payload.get("title") or "Manual Hackathon Signal",
                    payload.get("source_type"),
                    payload.get("evidence_type"),
                    payload.get("sponsor") or payload.get("source"),
                    area or payload.get("area"),
                    payload.get("focus"),
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
            LOGGER.info("Finished hackathon agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
            return response

        created = process_documents(documents, output_dir, db_path)
    except HackathonAgentError as exc:
        LOGGER.exception("Hackathon agent failed during run_agent")
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
        problem_headings=("Problem Statement", "Industry / Research Relevance"),
        started_at=started_at,
        default_source=payload.get("url") or payload.get("source"),
    )
    LOGGER.info("Finished hackathon agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
    return response


if __name__ == "__main__":
    raise SystemExit(main())
