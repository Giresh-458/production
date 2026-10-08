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

DEFAULT_OUTPUT_DIR = Path("outputs/opensource")
DEFAULT_DB_PATH = DEFAULT_OUTPUT_DIR / "opensource_memory.db"
DEFAULT_SOURCES_PATH = Path("sources/opensource_sources.yaml")
MAX_ISSUES_PER_REPO = 20
RELEVANCE_KEYWORDS = [
    "real-world assets",
    "performance",
    "scalability",
    "latency",
    "reliability",
    "engineering problem",
    "architecture limitation",
    "security problem",
    "research problem",

    "oracle",
    "rwa",
    "tokenization",
    "carbon",
    "mrv",
    "esg",
    "zero-knowledge",
    "zk",
    "proving",
    "verifier",
    "did",
    "verifiable credentials",
    "identity",
    "depin",
    "storage",
    "wireless",
    "sensor",
    "mev",
    "block builder",
    "transaction ordering",
    "stablecoin",
    "settlement",
    "liquidity",
    "governance",
    "scalability",
    "privacy",
    "interoperability",
    "security",
    "performance",
    "compliance",
]
FOCUSED_CONTENT_KEYWORDS = [
    "issue",
    "issues",
    "roadmap",
    "proposal",
    "release",
    "discussion",
    "enhancement",
    "bug",
    "improvement",
    "milestone",
    "security",
    "performance",
    "scalability",
    "interoperability",
    "help wanted",
    "research",
]
HIGH_SIGNAL_LABELS = [
    "bug",
    "enhancement",
    "help wanted",
    "good first issue",
    "performance",
    "security",
    "roadmap",
    "research",
    "protocol",
    "interoperability",
    "scalability",
    "privacy",
    "documentation",
]
LOGGER = logging.getLogger("rif.opensource_agent")
RECURSIVE_LINK_KEYWORDS = tuple(sorted(set(RELEVANCE_KEYWORDS + [keyword for values in AREA_KEYWORDS.values() for keyword in values] + ["issue", "issues", "roadmap", "proposal", "discussion", "docs", "release", "readme"])))
RECURSIVE_CONFIG = RecursiveCollectionConfig(
    max_depth=2,
    max_pages=10,
    link_keywords=RECURSIVE_LINK_KEYWORDS,
    relevance_threshold=2,
)


@dataclass(slots=True)
class OpenSourceSource:
    name: str
    type: str
    url: str
    focus: str


@dataclass(slots=True)
class OpenSourceDocument:
    project_name: str
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
class OpenSourceAnalysis:
    project_name: str
    area: str
    issue_or_item: str
    technical_problem: str
    why_it_matters: str
    research_opportunity: str
    prototype_idea: str
    problem_signature: str
    keywords: list[str]
    confidence: str
    source_type: str
    evidence_type: str
    source_method: str
    signal_class: str = "research_problem"
    github_activity: dict[str, object] | None = None


class OpenSourceAgentError(Exception):
    """Raised when the open-source agent cannot complete a task."""


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
        raise OpenSourceAgentError(
            "Automatic and URL modes require 'requests' and 'beautifulsoup4'. Install them before running those modes."
        ) from exc
    return requests, BeautifulSoup


def init_opensource_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS opensource_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            source_type TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            area TEXT NOT NULL,
            url TEXT NOT NULL,
            issue_or_item TEXT NOT NULL,
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
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_opensource_url ON opensource_data(url)")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_opensource_project_item ON opensource_data(project_name, issue_or_item)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_opensource_project_signature ON opensource_data(project_name, problem_signature)"
    )
    connection.commit()


def opensource_exists(
    connection: sqlite3.Connection,
    project_name: str,
    url: str,
    issue_or_item: str | None = None,
    problem_signature: str | None = None,
) -> bool:
    if issue_or_item and problem_signature:
        row = connection.execute(
            """
            SELECT 1 FROM opensource_data
            WHERE url = ? OR (project_name = ? AND issue_or_item = ?) OR (project_name = ? AND problem_signature = ?)
            """,
            (url, project_name, issue_or_item, project_name, problem_signature),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT 1 FROM opensource_data WHERE url = ? OR project_name = ?",
            (url, project_name),
        ).fetchone()
    return row is not None


def save_opensource_record(
    connection: sqlite3.Connection,
    document: OpenSourceDocument,
    analysis: OpenSourceAnalysis,
    output_path: Path,
) -> None:
    connection.execute(
        """
        INSERT INTO opensource_data (
            project_name,
            source_type,
            evidence_type,
            area,
            url,
            issue_or_item,
            technical_problem,
            research_opportunity,
            prototype_idea,
            problem_signature,
            keywords,
            confidence,
            path,
            last_checked
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
                project_name=excluded.project_name,
                source_type=excluded.source_type,
                evidence_type=excluded.evidence_type,
                area=excluded.area,
                issue_or_item=excluded.issue_or_item,
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
            analysis.project_name,
            analysis.source_type,
            analysis.evidence_type,
            analysis.area,
            document.url,
            analysis.issue_or_item,
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


def load_sources(sources_path: Path) -> list[OpenSourceSource]:
    if not sources_path.exists():
        raise OpenSourceAgentError(f"Open-source sources file not found: {sources_path}")
    try:
        data = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise OpenSourceAgentError(f"Invalid YAML in {sources_path}: {exc}") from exc
    root = data.get("opensource_sources", {})
    sources: list[OpenSourceSource] = []
    for _, entries in root.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = cleaned_text(str(entry.get("name", "")))
            source_type = cleaned_text(str(entry.get("type", ""))) or "documentation"
            url = cleaned_text(str(entry.get("url", "")))
            focus = cleaned_text(str(entry.get("focus", "")))
            if name and url:
                sources.append(OpenSourceSource(name=name, type=source_type, url=url, focus=focus))
    if not sources:
        raise OpenSourceAgentError("No open-source sources were loaded from sources/opensource_sources.yaml.")
    return apply_refresh_policy("opensource", sources)


def fetch_html(url: str) -> str:
    from core.shared_crawl_manager import SharedCrawlManager
    try:
        res = SharedCrawlManager.get().fetch(url)
        if res.status_code and res.status_code >= 400:
            LOGGER.warning("HTTP %s for %s", res.status_code, url)
            raise OpenSourceAgentError(f"Unable to fetch page {url}: HTTP {res.status_code}")
        LOGGER.debug("Fetched %s via %s", url, res.fetch_method)
        return res.html
    except OpenSourceAgentError:
        raise
    except Exception as exc:
        raise OpenSourceAgentError(f"Unable to fetch page {url}: {exc}") from exc

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


def infer_source_type(source_type: str) -> str:
    mapping = {
        "github_repo": "GitHub Repository",
        "forum": "Forum Discussion",
        "improvement_proposals": "Improvement Proposal",
        "documentation": "Documentation",
    }
    return mapping.get(source_type, "Documentation")


def infer_evidence_type(source_type: str) -> str:
    mapping = {
        "github_repo": "README",
        "forum": "Discussion",
        "improvement_proposals": "Proposal",
        "documentation": "Documentation",
    }
    return mapping.get(source_type, "Documentation")


GITHUB_CLASS_RULES = {
    "security_problem": ("security", "vulnerability", "exploit", "cve", "attack"),
    "performance_limitation": ("slow", "latency", "throughput", "benchmark", "performance", "scalability", "timeout"),
    "architecture_limitation": ("architecture", "redesign", "refactor", "protocol limitation", "cannot support"),
    "research_problem": ("research", "proof", "formal", "model", "algorithm", "open question"),
    "engineering_task": ("implement", "integration", "support", "upgrade", "refactor"),
    "feature": ("feature request", "add support", "would like", "enhancement"),
    "documentation": ("docs", "documentation", "readme", "typo"),
    "bug": ("bug", "error", "exception", "fails", "failure", "regression"),
}


def classify_github_item(title: str, body: str, labels: list[str] | None = None) -> str:
    text = f"{title} {body} {' '.join(labels or [])}".lower()
    
    # Prevent false positives from "not an exploit" or "not a vulnerability"
    negations = ["not a claim of an exploit", "not an exploit", "not a vulnerability", "not a security", "not a bug"]
    for neg in negations:
        if neg in text:
            text = text.replace("exploit", "").replace("vulnerability", "").replace("security", "").replace("bug", "")

    for category in ("security_problem", "performance_limitation", "architecture_limitation", "research_problem", "bug", "feature", "documentation", "engineering_task"):
        if any(term in text for term in GITHUB_CLASS_RULES[category]):
            return category
    return "engineering_task"


def _github_repo_parts(url: str) -> tuple[str, str] | None:
    match = re.search(r"github\.com/([^/]+)/([^/#?]+)", url)
    if not match:
        return None
    return match.group(1), match.group(2).removesuffix(".git")


def fetch_github_snapshot(source: OpenSourceSource) -> tuple[str, dict[str, object]]:
    """Use GitHub REST as a first-class source instead of scraping HTML."""
    parts = _github_repo_parts(source.url)
    if not parts:
        raise OpenSourceAgentError(f"Not a GitHub repository URL: {source.url}")
    requests, _ = load_requests_and_bs4()
    owner, repo = parts
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "RIF-OpenSource-Agent/1.0"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def get(endpoint: str, params: dict[str, object] | None = None):
        from core.http_client import get_response
        response = get_response(
            f"https://api.github.com{endpoint}",
            headers=headers,
            params=params or {},
            timeout=(8.0, 20.0),
            retries=1,
        )
        return response.json()

    try:
        repo_data = get(f"/repos/{owner}/{repo}")
        issues = get(f"/repos/{owner}/{repo}/issues", {"state": "open", "per_page": 20})
        pulls = get(f"/repos/{owner}/{repo}/pulls", {"state": "open", "per_page": 10})
        releases = get(f"/repos/{owner}/{repo}/releases", {"per_page": 5})
        commits = get(f"/repos/{owner}/{repo}/commits", {"per_page": 10})
        try:
            advisories = get(f"/repos/{owner}/{repo}/security-advisories", {"per_page": 10})
        except Exception:
            advisories = []
    except Exception as exc:
        raise OpenSourceAgentError(f"GitHub API failed for {owner}/{repo}: {exc}") from exc

    classified: dict[str, list[dict[str, object]]] = {}
    for item in issues:
        if "pull_request" in item:
            continue
        labels = [str(label.get("name", "")) for label in item.get("labels", []) if isinstance(label, dict)]
        category = classify_github_item(str(item.get("title", "")), str(item.get("body", "")), labels)
        classified.setdefault(category, []).append({
            "title": str(item.get("title", "")),
            "url": str(item.get("html_url", "")),
            "labels": labels,
            "updated_at": str(item.get("updated_at", "")),
        })

    parts_text = [
        f"GitHub repository: {repo_data.get('full_name', f'{owner}/{repo}')}",
        f"Description: {repo_data.get('description', '')}",
        f"Stars: {repo_data.get('stargazers_count', 0)}; forks: {repo_data.get('forks_count', 0)}",
        f"Open issues: {repo_data.get('open_issues_count', 0)}",
    ]
    for category, items in classified.items():
        parts_text.append(f"{category}:")
        parts_text.extend(f"- {item['title']} | {item['url']}" for item in items[:8])
    parts_text.append("Open pull requests:")
    parts_text.extend(f"- {item.get('title','')} | {item.get('html_url','')}" for item in pulls[:8])
    parts_text.append("Recent releases:")
    parts_text.extend(f"- {item.get('name') or item.get('tag_name','')} | {item.get('published_at','')}" for item in releases[:5])
    parts_text.append("Recent commits:")
    parts_text.extend(f"- {item.get('commit',{}).get('message','').splitlines()[0]} | {item.get('html_url','')}" for item in commits[:10])
    parts_text.append("Security advisories:")
    parts_text.extend(f"- {item.get('summary','')} | {item.get('severity','')}" for item in advisories[:10])

    metadata = {
        "github_activity": {
            "open_issues": len([x for x in issues if "pull_request" not in x]),
            "open_pull_requests": len(pulls),
            "recent_releases": len(releases),
            "recent_commits": len(commits),
            "security_advisories": len(advisories),
            "classified_items": {k: len(v) for k, v in classified.items()},
        },
        "github_classified_items": classified,
    }
    return cleaned_text("\n".join(parts_text)), metadata


def build_auto_documents(sources_path: Path, area: str | None = None) -> list[OpenSourceDocument]:
    import yaml
    raw_sources = yaml.safe_load(sources_path.read_text(encoding="utf-8")) if sources_path.exists() else {}
    from core.source_registry import extract_yaml_limits
    limits_by_url = extract_yaml_limits(raw_sources)
    from core.crawl_context import current_crawl_context
    documents: list[OpenSourceDocument] = []
    for source in load_sources(sources_path):
        LOGGER.info("Collecting open-source intelligence from %s", source.name)
        github_metadata: dict[str, object] = {}
        if source.type == "github_repo":
            try:
                content, github_metadata = fetch_github_snapshot(source)
                title = source.name
                area_hint = area or infer_area(cleaned_text(f"{source.name} {source.focus} {content}"), "Unscoped")
                if not is_relevant(cleaned_text(f"{source.name} {area_hint} {source.focus or ''} {content}"), area_hint):
                    LOGGER.info("Skipping GitHub source %s because it is not relevant.", source.name)
                    continue
                documents.append(
                    OpenSourceDocument(
                        project_name=source.name,
                        source_type="GitHub Repository",
                        evidence_type="GitHub Activity",
                        area_hint=area_hint,
                        focus=source.focus,
                        url=source.url,
                        title=title,
                        content=cleaned_text(f"{source.name} {source.focus} {content}"),
                        source_mode="Automatic",
                    )
                )
                continue
            except OpenSourceAgentError as exc:
                LOGGER.warning("GitHub-native collection failed for %s; falling back to web: %s", source.name, exc)
        try:
            html = fetch_html(source.url)
        except OpenSourceAgentError as exc:
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
        full_text = cleaned_text(f"{source.name} {source.focus} {title} {content}")
        area_hint = area or infer_area(full_text, "Unscoped")
        if not is_relevant(full_text, area_hint):
            LOGGER.info("Skipping %s because the source text is not relevant to the selected areas.", source.name)
            update_recursive_review(
                review["review_id"],
                outcome="filtered_out_irrelevant",
                outcome_reason="document did not pass open-source relevance filters",
                artifact_title=title,
                selected_area=area_hint or "",
            )
            continue
        documents.append(
            OpenSourceDocument(
                project_name=source.name,
                source_type=infer_source_type(source.type),
                evidence_type=infer_evidence_type(source.type),
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
            outcome_reason="document passed open-source relevance filters",
            artifact_title=title,
            selected_area=area_hint or "",
        )
    return documents


def build_manual_url_document(
    url: str,
    project_name: str | None,
    source_type: str | None,
    evidence_type: str | None,
    area: str | None,
    focus: str | None,
) -> OpenSourceDocument:
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
    title, content = combine_recursive_pages(pages, root_name=project_name or cleaned_text(urlparse(url).netloc), focus=focus or "")
    name = project_name or title or cleaned_text(urlparse(url).netloc)
    source_type_value = source_type or "Documentation"
    evidence_type_value = evidence_type or "Documentation"
    full_text = cleaned_text(f"{name} {area or ''} {focus or ''} {title} {content}")
    area_hint = area or infer_area(full_text, "Unscoped")
    if not is_relevant(full_text, area_hint):
        raise OpenSourceAgentError("Manual URL content does not map to the selected research areas or engineering signals.")
    return OpenSourceDocument(
        project_name=name,
        source_type=source_type_value,
        evidence_type=evidence_type_value,
        area_hint=area_hint,
        focus=focus or title,
        url=url,
        title=title,
        content=full_text,
        source_mode="Manual",
    )


def build_manual_text_document(
    text: str,
    project_name: str,
    source_type: str | None,
    evidence_type: str | None,
    area: str | None,
    focus: str | None,
) -> OpenSourceDocument:
    content = cleaned_text(text)
    area_hint = area or infer_area(cleaned_text(f"{project_name} {focus or ''} {content}"), "Unscoped")
    if not is_relevant(cleaned_text(f"{project_name} {area_hint} {focus or ''} {content}"), area_hint):
        raise OpenSourceAgentError("Manual text does not map to the selected research areas or engineering signals.")
    return OpenSourceDocument(
        project_name=project_name,
        source_type=source_type or "GitHub Repository",
        evidence_type=evidence_type or "Issue",
        area_hint=area_hint,
        focus=focus or area_hint,
        url=f"manual://{slugify(project_name)}/{slugify(area_hint)}",
        title=project_name,
        content=content,
        source_mode="Manual",
    )


def build_analysis_prompt(document: OpenSourceDocument) -> str:
    return textwrap.dedent(
        f"""\
        Extract open-source intelligence.

        Funding context: {current_funding_prompt_context.get() or "No specific funding call supplied."}

        Project: {document.project_name}
        Area hint: {document.area_hint}
        Source type: {document.source_type}
        Evidence type: {document.evidence_type}

        Return JSON only with keys:
        project_name
        area
        issue_or_item
        technical_problem
        why_it_matters
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
        - signal_class: bug, feature, documentation, engineering_task, performance_limitation, security_problem, architecture_limitation, research_problem
        - github_activity: object with counts when the source is GitHub; otherwise null
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
            raise OpenSourceAgentError("Local LLM response was not valid JSON.")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise OpenSourceAgentError(f"Local LLM returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise OpenSourceAgentError("Local LLM response was not a JSON object.")
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


def build_problem_signature(area: str, issue_or_item: str, technical_problem: str) -> str:
    return f"{area} | {cleaned_text(issue_or_item).lower()[:60]} | {cleaned_text(technical_problem).lower()[:60]}"


def fallback_analysis(document: Any) -> Any:
    raise ValueError("LLM extraction failed and synthetic fallback is disabled")


def analyze_document(document: OpenSourceDocument) -> OpenSourceAnalysis:
    LOGGER.info("Analyzing open-source source '%s' with local llm_provider", document.project_name)
    try:
        parsed = parse_llm_response(llm_generate(build_analysis_prompt(document)))
    except Exception as exc:
        LOGGER.warning("Using fallback open-source extraction for '%s': %s", document.project_name, exc)
        return fallback_analysis(document)

    area = document.area_hint
    if area not in AREA_KEYWORDS:
        area = infer_area(json.dumps(parsed), document.area_hint or "Unscoped")
    project_name = document.project_name
    issue_or_item = coalesce_text(parsed.get("issue_or_item"), "UNKNOWN")
    technical_problem = coalesce_text(parsed.get("technical_problem"), "UNKNOWN")
    
    why_it_matters = coalesce_text(parsed.get("why_it_matters"), "UNKNOWN")
    
    research_opportunity = coalesce_text(parsed.get("research_opportunity"), "UNKNOWN")
    
    prototype_idea = coalesce_text(parsed.get("prototype_idea"), "UNKNOWN")
    
    problem_signature = build_problem_signature(area, issue_or_item, technical_problem)
    keywords = sanitize_list(parsed.get("keywords"), max_items=6, lowercase=True)
    confidence = "Medium".title()
    if confidence not in {"High", "Medium", "Low"}:
        confidence = "Medium"
    source_type = document.source_type
    evidence_type = document.evidence_type
    return OpenSourceAnalysis(
        project_name=project_name,
        area=area,
        issue_or_item=issue_or_item,
        technical_problem=technical_problem,
        why_it_matters=why_it_matters,
        research_opportunity=research_opportunity,
        prototype_idea=prototype_idea,
        problem_signature=problem_signature,
        keywords=keywords,
        confidence=confidence,
        source_type=source_type,
        evidence_type=evidence_type,
        source_method="ollama",
        signal_class=classify_github_item(document.title, document.content),
        github_activity=parsed.get("github_activity") if isinstance(parsed.get("github_activity"), dict) else None,
    )


def build_tags(analysis: OpenSourceAnalysis) -> list[str]:
    tags = ["#OpenSource", "#Blockchain"]
    area_tag = normalize_tag(analysis.area)
    if area_tag and area_tag not in tags:
        tags.append(area_tag)
    for keyword in analysis.keywords:
        tag = normalize_tag(keyword)
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def build_markdown(document: OpenSourceDocument, analysis: OpenSourceAnalysis) -> str:
    tags = build_tags(analysis)
    keywords_line = " ".join(normalize_tag(keyword) for keyword in analysis.keywords if normalize_tag(keyword)) or "#opensource"
    lines = [
        f"# Open Source Signal: {analysis.project_name}",
        "",
        "## Layer",
        "OpenSource",
        "",
        "## Source Type",
        analysis.source_type,
        "",
        "## Evidence Type",
        analysis.evidence_type,
        "",
        "## Project / Repository",
        analysis.project_name,
        "",
        "## Confidence",
        analysis.confidence,
        "",
        "## Area",
        analysis.area,
        "",
        "## Repository / Source",
        document.url if document.source_mode != "Manual" or document.url.startswith("http") else "Manual",
        "",
        "## Issue / Roadmap Item",
        analysis.issue_or_item,
        "",
        "## Technical Problem",
        analysis.technical_problem,
        "",
        "## Why It Matters",
        analysis.why_it_matters,
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


def save_markdown_output(document: OpenSourceDocument, analysis: OpenSourceAnalysis, output_dir: Path) -> Path:
    area_dir = output_dir / slugify(analysis.area)
    area_dir.mkdir(parents=True, exist_ok=True)
    output_path = area_dir / f"{slugify(analysis.project_name)}.md"
    output_path.write_text(build_markdown(document, analysis), encoding="utf-8")
    LOGGER.info("Saved open-source markdown output to %s", output_path)
    return output_path


def fetch_all_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT project_name, source_type, evidence_type, area, issue_or_item, technical_problem,
               research_opportunity, prototype_idea, problem_signature, keywords
        FROM opensource_data
        ORDER BY area, project_name
        """
    ).fetchall()


def write_insights(connection: sqlite3.Connection, output_dir: Path) -> None:
    rows = fetch_all_rows(connection)
    if not rows:
        return
    engineering_counter: Counter[str] = Counter()
    gap_counter: Counter[str] = Counter()
    research_counter: Counter[str] = Counter()
    prototype_counter: Counter[str] = Counter()
    area_summary: dict[str, list[str]] = defaultdict(list)
    mappings: list[str] = []

    for row in rows:
        engineering_counter.update([cleaned_text(row["issue_or_item"]).lower()])
        gap_counter.update([cleaned_text(row["technical_problem"]).lower()])
        research_counter.update([cleaned_text(row["research_opportunity"]).lower()])
        prototype_counter.update([cleaned_text(row["prototype_idea"]).lower()])
        area_summary[row["area"]].append(cleaned_text(row["technical_problem"]))
        mappings.append(
            f"- {cleaned_text(row['issue_or_item'])} -> {cleaned_text(row['technical_problem'])} -> {cleaned_text(row['research_opportunity'])}"
        )

    lines = ["# Open-Source / Protocol Intelligence Insights", "", "## Most Common Engineering Problems"]
    for item, count in engineering_counter.most_common(5):
        lines.append(f"- {item}: {count}")

    lines.extend(["", "## Repeated Technical Gaps"])
    for item, count in gap_counter.most_common(5):
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

    lines.extend(["", "## Engineering-to-Research Mapping"])
    lines.extend(mappings[:10] or ["- No mappings yet."])

    insights_path = output_dir / "insights.md"
    insights_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("Saved open-source insights to %s", insights_path)


def process_documents(documents: list[OpenSourceDocument], output_dir: Path, db_path: Path) -> int:
    connection = db_connect(db_path)
    init_opensource_db(connection)
    created = 0
    for document in documents:
        if opensource_exists(connection, document.project_name, document.url):
            LOGGER.info("Skipping already stored open-source source '%s'", document.project_name)
            continue
        analysis = analyze_document(document)
        if opensource_exists(connection, analysis.project_name, document.url, analysis.issue_or_item, analysis.problem_signature):
            LOGGER.info("Skipping duplicate open-source source '%s' after signature match", analysis.project_name)
            continue
        output_path = save_markdown_output(document, analysis, output_dir)
        save_opensource_record(connection, document, analysis, output_path)
        created += 1
    write_insights(connection, output_dir)
    return created


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and structure open-source intelligence for RIF.")
    parser.add_argument("--mode", choices=["auto", "url", "text"], required=True, help="Input mode")
    parser.add_argument("--area", help="Optional research area filter for auto mode")
    parser.add_argument("--sources-path", default=str(DEFAULT_SOURCES_PATH), help="Path to sources/opensource_sources.yaml")
    parser.add_argument("--url", help="Manual URL input")
    parser.add_argument("--text", help="Manual raw text input")
    parser.add_argument("--project-name", help="Project or repository name for manual modes")
    parser.add_argument("--source-type", help="Optional source type override")
    parser.add_argument("--evidence-type", help="Optional evidence type override")
    parser.add_argument("--area", choices=["RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"], help="Optional area override")
    parser.add_argument("--focus", help="Optional focus hint")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for open-source markdown outputs")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path for open-source memory")
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
                    args.project_name,
                    args.source_type,
                    args.evidence_type,
                    args.area,
                    args.focus,
                )
            ]
        else:
            if not args.text or not args.project_name:
                parser.error("--text and --project-name are required for mode=text")
            documents = [
                build_manual_text_document(
                    args.text,
                    args.project_name,
                    args.source_type,
                    args.evidence_type,
                    args.area,
                    args.focus,
                )
            ]
        created = process_documents(documents, output_dir, db_path)
    except OpenSourceAgentError as exc:
        LOGGER.error("Open-source agent failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Processed {len(documents)} open-source inputs. Created {created} new markdown file(s).")
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

    agent_name = "opensource"
    layer = "OpenSource"
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

    LOGGER.info("Starting open-source agent run: mode=%s area=%s", mode, area)

    try:
        if mode == "configured_scan":
            sources_path = Path(payload.get("sources_path", DEFAULT_SOURCES_PATH))
            documents = build_auto_documents(sources_path, area)
        elif mode == "manual_url":
            url = payload["url"]
            documents = [
                build_manual_url_document(
                    url,
                    payload.get("project_name") or infer_name_from_url(url, "Manual Project"),
                    payload.get("source_type", "GitHub Repository"),
                    payload.get("evidence_type", "Documentation"),
                    area or payload.get("area"),
                    payload.get("focus", area or "protocol engineering"),
                )
            ]
        else:
            text = payload["text"]
            documents = [
                build_manual_text_document(
                    text,
                    payload.get("project_name") or payload.get("title") or "Manual Project",
                    payload.get("source_type", "Manual"),
                    payload.get("evidence_type", "Technical Discussion"),
                    area or payload.get("area"),
                    payload.get("focus", area or "protocol engineering"),
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
            LOGGER.info("Finished opensource agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
            return response

        created = process_documents(documents, output_dir, db_path)
    except OpenSourceAgentError as exc:
        LOGGER.exception("Open-source agent failed during run_agent")
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
        problem_headings=("Technical Problem", "Research Opportunity"),
        started_at=started_at,
        default_source=payload.get("url") or payload.get("source"),
    )
    LOGGER.info("Finished open-source agent run: status=%s outputs=%s", response["status"], len(response["outputs"]))
    return response


if __name__ == "__main__":
    raise SystemExit(main())
