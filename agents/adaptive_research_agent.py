from __future__ import annotations

import argparse
import logging
import re
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.agent_interface import finalize_collection_agent_response
from core.schemas import create_error_response
from core.funding_selection import FundingCallContext
from core.funding_context import generate_funding_queries
from core.mission_gate import relevance
from agents.utils import cleaned_text

LOGGER = logging.getLogger("rif.adaptive_research_agent")
AGENT_NAME = "adaptive_research"
LAYER_NAME = "Adaptive Research"
DEFAULT_OUTPUT_DIR = Path("outputs/intermediate/adaptive_research")


@dataclass(slots=True)
class AdaptiveDocument:
    title: str
    source: str
    content: str
    research_area: str = "Unscoped"
    source_type: str = "adaptive_web_research"
    query: str = ""
    retrieved_at: str = ""


def _safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-") or "topic"


def _fetch(url: str) -> str:
    from core.http_client import fetch_text
    text, _ = fetch_text(url, timeout=(8.0, 20.0), retries=1)
    return text


def _extract_html_text(html: str) -> tuple[str, str]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    title = cleaned_text((soup.find("h1") or soup.title).get_text(" ", strip=True)) if (soup.find("h1") or soup.title) else "Adaptive Research Source"
    blocks = []
    for node in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        text = cleaned_text(node.get_text(" ", strip=True))
        if len(text) >= 30:
            blocks.append(text)
    return title, cleaned_text(" ".join(blocks))[:7000]


def _openalex(query: str, limit: int = 6) -> list[AdaptiveDocument]:
    from core.http_client import fetch_json
    compact = re.sub(r"[^A-Za-z0-9\- ]+", " ", query)
    compact = " ".join(compact.split())[:100].rsplit(" ", 1)[0] or "research"
    data, _ = fetch_json(
        "https://api.openalex.org/works",
        params={"search": compact, "per-page": min(limit, 10)},
        headers={"User-Agent": "RIF-Adaptive-Research/1.0 (rif-adaptive-research-agent/1.0)"},
        timeout=(8.0, 20.0), retries=2,
    )
    docs: list[AdaptiveDocument] = []
    for item in data.get("results", []) or []:
        title = cleaned_text(str(item.get("display_name") or item.get("title") or ""))
        abstract = item.get("abstract_inverted_index") or {}
        words = []
        for word, positions in abstract.items():
            for pos in positions or []:
                words.append((pos, word))
        abstract_text = " ".join(word for _, word in sorted(words))
        summary = cleaned_text(abstract_text) or cleaned_text(str(item.get("concepts", "")))
        source = str(item.get("doi") or item.get("id") or "https://openalex.org/")
        if title and summary:
            docs.append(AdaptiveDocument(title=title, source=source, content=f"{title}. {summary[:5000]}", query=query, retrieved_at=datetime.now(UTC).isoformat()))
    return docs


def _search_web(query: str, limit: int = 4) -> list[AdaptiveDocument]:
    """Use public search endpoints only; extract candidate URLs and fetch them instead of treating search results as evidence."""
    docs: list[AdaptiveDocument] = []
    encoded = urllib.parse.quote_plus(query)
    urls = [
        f"https://www.google.com/search?q={encoded}&num={limit*2}",
        f"https://www.bing.com/search?q={encoded}&count={limit*2}",
    ]
    for url in urls:
        try:
            html = _fetch(url)
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            candidates = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("/url?q="):
                    href = urllib.parse.unquote(href.split("/url?q=")[1].split("&")[0])
                if href.startswith("http") and "google.com" not in href and "bing.com" not in href:
                    if href not in candidates:
                        candidates.append(href)
                        if len(candidates) >= limit:
                            break
            
            for candidate in candidates:
                try:
                    cand_html = _fetch(candidate)
                    title, text = _extract_html_text(cand_html)
                    if len(text) > 200:
                        docs.append(AdaptiveDocument(title=title, source=candidate, content=text, query=query, retrieved_at=datetime.now(UTC).isoformat()))
                except Exception as exc:
                    LOGGER.debug("Failed to fetch candidate %s: %s", candidate, exc)
            if docs:
                break
        except Exception as exc:
            LOGGER.warning("Adaptive web search failed for %s: %s", query, exc)
    return docs


def run_agent(mode: str, area: str | None = None, input_data: dict[str, Any] | None = None, funding_context: FundingCallContext | None = None) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    payload = input_data or {}
    output_dir = Path(payload.get("output_dir", DEFAULT_OUTPUT_DIR))
    if mode != "configured_scan":
        return create_error_response(agent=AGENT_NAME, layer=LAYER_NAME, mode=mode, area=area, errors=["adaptive_research supports configured_scan only"], started_at=started_at, finished_at=datetime.now(UTC))
    if not funding_context:
        return create_error_response(agent=AGENT_NAME, layer=LAYER_NAME, mode=mode, area=area, errors=["adaptive_research requires a selected funding context"], started_at=started_at, finished_at=datetime.now(UTC))

    try:
        queries = generate_funding_queries(funding_context, max_queries=6)
        queries = list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))[:6]

        documents: list[AdaptiveDocument] = []
        for query in queries:
            try:
                documents.extend(_openalex(query, limit=5))
            except Exception as exc:
                LOGGER.warning("OpenAlex adaptive query failed: %s", exc)
            if len(documents) < 8:
                documents.extend(_search_web(query, limit=4))
            if len(documents) >= 18:
                break

        # Retrieval is not evidence. Apply a post-retrieval mission gate before any
        # adaptive document enters the shared intelligence corpus.
        gated: list[AdaptiveDocument] = []
        gate_meta: list[dict[str, Any]] = []
        for doc in documents:
            probe = {"title": doc.title, "problem_statement": doc.content[:1200], "context_summary": doc.content[:3000], "source_type": doc.source_type, "evidence_type": "adaptive", "keywords": doc.query.split()}
            gate = relevance(probe, funding_context)
            gate_meta.append({"source": doc.source, "title": doc.title, **gate})
            if gate["passed"]:
                gated.append(doc)
        documents = gated

        # Always retain the funding mission itself as a grounding artifact if external
        # discovery is temporarily unavailable. This prevents an empty pipeline from
        # pretending that an unknown research area has no evidence.
        if not documents:
            rc = funding_context.research_context
            mission = "\n".join(rc.investigation_questions if rc else [])
            documents.append(AdaptiveDocument(
                title=f"Adaptive research mission — {funding_context.call_title or funding_context.program_name}",
                source=funding_context.application_url or funding_context.source_url or "funding-context://selected-call",
                content=cleaned_text(f"Funding priorities: {', '.join(funding_context.research_priorities or [])}. {mission}"),
                query="funding mission",
                retrieved_at=datetime.now(UTC).isoformat(),
            ))

        # Deduplicate by title/source and cap the artifact set.
        seen: set[tuple[str, str]] = set()
        unique: list[AdaptiveDocument] = []
        for doc in documents:
            key = (doc.title.casefold(), doc.source.casefold())
            if key in seen or not doc.content.strip():
                continue
            seen.add(key)
            unique.append(doc)
        unique = unique[:24]

        response = finalize_collection_agent_response(
            agent=AGENT_NAME,
            layer=LAYER_NAME,
            mode=mode,
            area=area or "Unscoped",
            documents=unique,
            output_dir=output_dir,
            started_at=started_at,
            funding_context=funding_context,
            funding_contexts=[funding_context] if funding_context else None,
            run_id=payload.get("run_id"),
            default_source=funding_context.source_url,
            warnings=["Adaptive mode is enabled because canonical research-area coverage may be incomplete; evidence remains explicitly marked Unscoped until domain normalization has evidence."] if (area or "Unscoped") == "Unscoped" else [],
        )
        response.setdefault("metadata", {})["adaptive_queries"] = queries
        response["metadata"]["selected_funding_call_id"] = funding_context.funding_call_id
        response["metadata"]["mission_relevance_gate"] = {"candidates": len(gate_meta), "accepted": len(gated), "rejected": max(0, len(gate_meta)-len(gated)), "details": gate_meta[:50]}
        return response
    except Exception as exc:
        LOGGER.exception("Adaptive research agent failed")
        return create_error_response(agent=AGENT_NAME, layer=LAYER_NAME, mode=mode, area=area, errors=[str(exc)], started_at=started_at, finished_at=datetime.now(UTC))


def main() -> int:
    parser = argparse.ArgumentParser(description="Adaptive research discovery for unknown research areas")
    parser.add_argument("--mode", default="configured_scan")
    parser.add_argument("--area", default="Unscoped")
    args = parser.parse_args()
    # CLI usage requires a context and is intentionally not supported without pipeline orchestration.
    print("adaptive_research is intended to run through the RIF pipeline with a selected funding call context")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
