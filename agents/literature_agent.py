from __future__ import annotations

import argparse
import json
import logging
import re
import socket
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from core.funding_selection import FundingCallContext
from core.funding_context import build_agent_specific_funding_context, current_funding_prompt_context, current_funding_queries, generate_funding_queries
import collections
import math

from core.semantic_retrieval import semantic_scores

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.db import (
    DEFAULT_DB_PATH,
    bootstrap_memory_from_outputs,
    db_connect,
    fetch_catalog_entries,
    init_papers_db,
    paper_exists,
    save_paper_record,
)
from agents.utils import cleaned_text
from core.llm_provider import generate as llm_generate
from core.schemas import AREA_KEYWORDS

ARXIV_API_URL = "http://export.arxiv.org/api/query"
ARXIV_ATOM_NAMESPACE = {"atom": "http://www.w3.org/2005/Atom"}
DEFAULT_RESULTS = 3
LOOKBACK_YEARS = 5
MAX_ARXIV_RESULTS = 18
MAX_PER_SOURCE = 10
ARXIV_MAX_RETRIES = 3
ARXIV_MIN_INTERVAL_SECONDS = 3.0
ARXIV_STATE_PATH = PROJECT_ROOT / "outputs" / "workflow" / "arxiv_api_state.json"
ARXIV_BASE_BACKOFF_SECONDS = 5
DEFAULT_OUTPUT_DIR = Path("outputs/literature")
BLOCKCHAIN_CONTEXT_TERMS = ["blockchain", "smart contract", "ethereum", "web3", "crypto"]
BLOCKCHAIN_TERMS = {
    "blockchain", "distributed ledger", "smart contract", "smart contracts",
    "cryptocurrency", "token", "tokenization", "decentralized",
    "zero-knowledge", "zk", "ethereum", "rollup", "consensus",
    "on-chain", "off-chain", "web3", "defi", "stablecoin", "identity",
}
STATIC_TAGS = {
    "RWA": "#RWA", "ESG": "#ESG", "ZK-IoV": "#ZK", "DID": "#DID",
    "DePIN": "#DePIN", "MEV": "#MEV", "Stablecoins": "#Stablecoins",
    "DigitalHealthCPS": "#DigitalHealthCPS",
}
CONCEPT_VOCAB = [
    "trust", "verification", "privacy", "scalability", "compliance", "identity",
    "interoperability", "security", "governance", "efficiency", "latency",
    "auditability", "resilience", "tokenization",
]
LOGGER = logging.getLogger("rif.literature_agent")


@dataclass(slots=True)
class Paper:
    title: str
    summary: str
    url: str
    published: datetime | None
    authors: list[str]
    # Provenance fields
    source: str
    source_id: str
    doi: str | None
    venue: str | None
    retrieval_timestamp: str
    # Scoring
    score: float = 0.0
    relevance_score: float = 0.0
    impact_score: float = 0.0
    recency_score: float = 0.0
    problem_clarity_score: float = 0.0
    semantic_score: float = 0.0


@dataclass(slots=True)
class AnalysisResult:
    problem: str
    gap: str
    insight: str
    keyword_tags: list[str]
    concept_tags: list[str]
    related_titles: list[str]
    source: str


class LiteratureAgentError(Exception):
    pass


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def get_keywords(research_area: str | None) -> list[str]:
    normalized_area = str(research_area or "").strip()
    if normalized_area and normalized_area in AREA_KEYWORDS:
        return AREA_KEYWORDS[normalized_area]
    # Unscoped funding-driven literature retrieval uses concrete mission terms,
    # never a synthetic taxonomy label.
    terms: list[str] = []
    for query in (current_funding_queries.get() or []):
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", str(query).lower()):
            if token not in {"what", "which", "where", "does", "this", "that", "research", "funding", "opportunity"} and token not in terms:
                terms.append(token)
    return terms[:24] or ["research"]


def _search_terms(research_area: str | None) -> list[str]:
    terms = list(get_keywords(research_area))
    mission_queries = current_funding_queries.get() or []
    for query in mission_queries:
        text = cleaned_text(str(query))
        if text and text not in terms:
            terms.append(text)
    # Keep queries bounded; investigation questions are more informative than
    # a huge keyword OR expression and can otherwise make provider APIs noisy.
    return terms[:10]


def build_query(research_area: str | None) -> str:
    terms = _search_terms(research_area)
    funding_queries = [q.strip() for q in (current_funding_queries.get() or []) if str(q).strip()]
    query_terms = list(dict.fromkeys(terms + funding_queries[:4]))
    base_terms = " OR ".join(f'all:"{term}"' for term in query_terms)
    # Blockchain context is required only for blockchain-native domains.
    # DigitalHealthCPS must remain a genuine healthcare/CPS search.
    if research_area in {"RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins"}:
        context_terms = " OR ".join(f'all:"{term}"' for term in BLOCKCHAIN_CONTEXT_TERMS)
        return f"({base_terms}) AND ({context_terms})"
    return f"({base_terms})"


def parse_datetime(value: str | int | None) -> datetime | None:
    """Parse only explicit bibliographic dates; never substitute the current date."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except Exception:
            pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except Exception:
            pass
    return None  # UNKNOWN — do not fabricate


def contains_any(text: str, terms: Iterable[str]) -> int:
    lowered = text.lower()
    return sum(1 for term in terms if term.lower() in lowered)


def compute_relevance(text: str, research_keywords: list[str]) -> float:
    keyword_hits = contains_any(text, research_keywords)
    keyword_score = min(keyword_hits / max(len(research_keywords), 1), 1.0)
    return round(keyword_score, 4)


def compute_recency(published: datetime | None, now: datetime) -> float:
    if published is None:
        return 0.0
    max_age_days = LOOKBACK_YEARS * 365
    age_days = max((now - published).days, 0)
    normalized = max(0.0, 1 - (age_days / max_age_days))
    return round(normalized, 4)


def compute_problem_clarity(summary: str) -> float:
    indicators = [
        "problem", "challenge", "limitation", "bottleneck",
        "we address", "we propose", "however", "existing", "gap",
    ]
    hits = contains_any(summary, indicators)
    length_bonus = min(len(summary.split()) / 180, 1.0)
    return round(min((hits / len(indicators)) + (0.35 * length_bonus), 1.0), 4)

def score_paper(paper: Paper, research_area: str, now: datetime) -> Paper | None:
    search_text = f"{paper.title} {paper.summary}"
    keywords = get_keywords(research_area)
    
    relevance_score = compute_relevance(search_text, keywords)
    if relevance_score == 0:
        return None
        
    recency_score = compute_recency(paper.published, now)
    problem_clarity_score = compute_problem_clarity(paper.summary)
    
    impact_score = min(len(paper.authors) / 6, 1.0) * 0.5
    
    overall_score = round(
        (0.4 * relevance_score) + (0.3 * impact_score) + (0.2 * recency_score) + (0.1 * problem_clarity_score),
        4,
    )
    
    paper.relevance_score = relevance_score
    paper.recency_score = recency_score
    paper.problem_clarity_score = problem_clarity_score
    paper.impact_score = impact_score
    paper.score = overall_score
    return paper


def enforce_api_spacing() -> None:
    time.sleep(1.0)

def fetch_arxiv_entries(research_area: str, max_results: int = MAX_PER_SOURCE) -> list[Paper]:
    query = build_query(research_area)
    params = urllib.parse.urlencode({
        "search_query": query, "start": 0, "max_results": max_results,
        "sortBy": "submittedDate", "sortOrder": "descending",
    })
    request_url = f"{ARXIV_API_URL}?{params}"
    LOGGER.info("Fetching arXiv papers for area '%s'", research_area)
    request = urllib.request.Request(
        request_url, headers={"User-Agent": "RIF-Literature-Agent/3.0"}
    )
    
    papers = []
    try:
        from core.http_client import get_response
        response = get_response(request_url, headers={"User-Agent": "RIF-Literature-Agent/3.1"}, timeout=(8.0, 20.0), retries=1)
        payload = response.content
        root = ET.fromstring(payload)
        entries = root.findall("atom:entry", ARXIV_ATOM_NAMESPACE)
        for entry in entries:
            title = cleaned_text(entry.findtext("atom:title", default="", namespaces=ARXIV_ATOM_NAMESPACE))
            summary = cleaned_text(entry.findtext("atom:summary", default="", namespaces=ARXIV_ATOM_NAMESPACE))
            published_raw = entry.findtext("atom:published", default="", namespaces=ARXIV_ATOM_NAMESPACE)
            link = entry.find("atom:id", ARXIV_ATOM_NAMESPACE)
            if not title or not summary or not published_raw or link is None: continue
            
            authors = [
                cleaned_text(author.findtext("atom:name", default="", namespaces=ARXIV_ATOM_NAMESPACE))
                for author in entry.findall("atom:author", ARXIV_ATOM_NAMESPACE)
            ]
            papers.append(Paper(
                title=title, summary=summary, url=link.text,
                published=parse_datetime(published_raw), authors=authors,
                source="arXiv", source_id=link.text.split("/")[-1] if link.text else "",
                doi=None, venue=None, retrieval_timestamp=""
            ))
    except Exception as exc:
        LOGGER.warning("arXiv fetch failed: %s", exc)
    return papers

def _fetch_provider_json(url: str, source_name: str) -> dict:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "RIF-Literature-Agent/3.0"})
        from core.http_client import fetch_json
        data, _ = fetch_json(url, headers={"User-Agent": "RIF-Literature-Agent/3.1"}, timeout=(8.0, 20.0), retries=0)
        return data
    except Exception as exc:
        LOGGER.warning("%s fetch failed: %s", source_name, exc)
        return {}

def fetch_openalex_entries(research_area: str, max_results: int = MAX_PER_SOURCE) -> list[Paper]:
    # OpenAlex is much more reliable with short topical searches than with a
    # concatenation of long investigation questions. Run a few compact queries
    # and merge/deduplicate results instead of sending one giant URL.
    raw_terms = _search_terms(research_area)
    compact_terms: list[str] = []
    for term in raw_terms:
        text = re.sub(r"[^A-Za-z0-9\- ]+", " ", str(term))
        text = " ".join(text.split())
        if not text:
            continue
        if len(text) > 80:
            text = text[:80].rsplit(" ", 1)[0]
        if text.lower() not in {x.lower() for x in compact_terms}:
            compact_terms.append(text)
    queries = compact_terms[:4] or [research_area]
    papers_by_id: dict[str, Paper] = {}
    for query in queries:
        enforce_api_spacing()
        try:
            from core.http_client import fetch_json
            data, _ = fetch_json(
                "https://api.openalex.org/works",
                params={"search": query, "per-page": min(max_results, 10)},
                headers={"User-Agent": "RIF-Literature-Agent/3.1 (rif-research-agent/3.1)"},
                timeout=(8.0, 20.0), retries=0,
            )
        except Exception as exc:
            LOGGER.warning("OpenAlex query failed for %r: %s", query, exc)
            continue
        for item in data.get("results", []) or []:
            item_id = str(item.get("id") or item.get("doi") or item.get("display_name") or "")
            if item_id and item_id in papers_by_id:
                continue
            title = item.get("display_name") or item.get("title") or ""
            abstract = item.get("abstract_inverted_index", {})
            if isinstance(abstract, dict) and abstract:
                words = [(pos, word) for word, positions in abstract.items() for pos in (positions or [])]
                words.sort(key=lambda x: x[0])
                summary = " ".join(w for _, w in words)
            else:
                summary = "No abstract available."
            published_raw = item.get("publication_date", "")
            if not title or not published_raw:
                continue
            authors = [a.get("author", {}).get("display_name", "") for a in item.get("authorships", [])]
            doi = item.get("doi")
            paper = Paper(
                title=title, summary=summary, url=doi or item.get("id", ""),
                published=parse_datetime(published_raw), authors=authors,
                source="OpenAlex", source_id=item.get("id", ""), doi=doi,
                venue=((item.get("primary_location") or {}).get("source") or {}).get("display_name"),
                retrieval_timestamp=""
            )
            papers_by_id[item_id or paper.url] = paper
    return list(papers_by_id.values())[:max_results * 2]

    papers = []
    for item in data.get("results", []):
        title = item.get("title") or ""
        abstract = item.get("abstract_inverted_index", {})
        if isinstance(abstract, dict) and abstract:
            words = []
            for word, positions in abstract.items():
                for pos in positions:
                    words.append((pos, word))
            words.sort(key=lambda x: x[0])
            summary = " ".join(w[1] for w in words)
        else:
            summary = "No abstract available."
            
        published_raw = item.get("publication_date", "")
        if not title or not summary or not published_raw: continue
        authors = [a.get("author", {}).get("display_name", "") for a in item.get("authorships", [])]
        doi = item.get("doi")
        
        papers.append(Paper(
            title=title, summary=summary, url=doi or item.get("id", ""),
            published=parse_datetime(published_raw), authors=authors,
            source="OpenAlex", source_id=item.get("id", ""), doi=doi,
            venue=((item.get("primary_location") or {}).get("source") or {}).get("display_name"),
            retrieval_timestamp=""
        ))
    return papers

def fetch_semanticscholar_entries(research_area: str, max_results: int = MAX_PER_SOURCE) -> list[Paper]:
    enforce_api_spacing()
    query = urllib.parse.quote(" ".join(_search_terms(research_area)))
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?query={query}&limit={max_results}&fields=title,abstract,authors,year,url,venue,externalIds"
    data = _fetch_provider_json(url, "Semantic Scholar")
    
    papers = []
    for item in data.get("data", []):
        title = item.get("title") or ""
        summary = item.get("abstract") or "No abstract available."
        year = item.get("year")
        published_raw = str(year) if year else ""
        authors = [a.get("name", "") for a in item.get("authors", [])]
        doi = item.get("externalIds", {}).get("DOI")
        
        papers.append(Paper(
            title=title, summary=summary, url=item.get("url") or (f"https://doi.org/{doi}" if doi else ""),
            published=parse_datetime(year), authors=authors,
            source="Semantic Scholar", source_id=item.get("paperId", ""), doi=doi,
            venue=item.get("venue"), retrieval_timestamp=""
        ))
    return papers

def fetch_crossref_entries(research_area: str, max_results: int = MAX_PER_SOURCE) -> list[Paper]:
    enforce_api_spacing()
    query = urllib.parse.quote(" ".join(_search_terms(research_area)))
    url = f"https://api.crossref.org/works?query={query}&rows={max_results}"
    data = _fetch_provider_json(url, "Crossref")
    
    papers = []
    for item in data.get("message", {}).get("items", []):
        title_list = item.get("title", [])
        title = title_list[0] if title_list else ""
        summary = item.get("abstract", "No abstract available.")
        year = None
        for field in ["published", "published-online", "published-print", "issued"]:
            date_parts = item.get(field, {}).get("date-parts", [[]])
            if date_parts and date_parts[0] and date_parts[0][0]:
                year = date_parts[0][0]
                break
        published_value = None
        for field in ["published", "published-online", "published-print", "issued"]:
            date_parts = item.get(field, {}).get("date-parts", [[]])
            if date_parts and date_parts[0]:
                parts = date_parts[0]
                if parts and parts[0]:
                    if len(parts) >= 3:
                        published_value = f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
                    elif len(parts) >= 2:
                        published_value = f"{parts[0]:04d}-{parts[1]:02d}-01"
                    else:
                        published_value = str(parts[0])
                    break
        authors = [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in item.get("author", [])]
        doi = item.get("DOI")
        
        papers.append(Paper(
            title=title, summary=summary, url=item.get("URL") or (f"https://doi.org/{doi}" if doi else ""),
            published=parse_datetime(published_value), authors=authors,
            source="Crossref", source_id=doi or "", doi=doi,
            venue=item.get("publisher"), retrieval_timestamp=""
        ))
    return papers

def fetch_dblp_entries(research_area: str, max_results: int = MAX_PER_SOURCE) -> list[Paper]:
    enforce_api_spacing()
    query = urllib.parse.quote(" ".join(_search_terms(research_area)))
    url = f"https://dblp.org/search/publ/api?q={query}&h={max_results}&format=json"
    data = _fetch_provider_json(url, "DBLP")
    
    papers = []
    hits = data.get("result", {}).get("hits", {}).get("hit", [])
    if isinstance(hits, dict):
        hits = [hits]
    for hit in hits:
        item = hit.get("info", {})
        title = item.get("title", "")
        summary = "No abstract available (DBLP)."
        year = item.get("year")
        published_raw = str(year) if year else ""
        
        authors_obj = item.get("authors", {}).get("author", [])
        if isinstance(authors_obj, dict):
            authors = [authors_obj.get("text", "")]
        elif isinstance(authors_obj, list):
            authors = [a.get("text", "") if isinstance(a, dict) else str(a) for a in authors_obj]
        else:
            authors = []
            
        doi = item.get("doi")
        url_link = item.get("ee") or item.get("url", "")
        
        papers.append(Paper(
            title=title, summary=summary, url=url_link,
            published=parse_datetime(published_raw), authors=authors,
            source="DBLP", source_id=item.get("key", ""), doi=doi,
            venue=item.get("venue"), retrieval_timestamp=""
        ))
    return papers

def _paper_identity(paper: Paper) -> str:
    if paper.doi:
        return "doi:" + paper.doi.strip().lower()
    if paper.source_id:
        return f"{paper.source.lower()}:{paper.source_id.strip().lower()}"
    if paper.url:
        return "url:" + paper.url.strip().rstrip("/").lower()
    authors = "|".join(sorted(a.strip().lower() for a in paper.authors if a.strip())[:5])
    title_key = re.sub(r"[^a-z0-9]+", " ", paper.title.lower()).strip()
    return f"bib:{title_key}|{authors}|{paper.published.year if paper.published else 'unknown'}"


def deduplicate_papers(papers: list[Paper]) -> list[Paper]:
    """Deduplicate on bibliographic identity; identical titles may coexist."""
    seen: set[str] = set()
    unique: list[Paper] = []
    for paper in sorted(papers, key=lambda item: item.published or datetime.min.replace(tzinfo=UTC), reverse=True):
        key = _paper_identity(paper)
        if key in seen:
            continue
        seen.add(key)
        unique.append(paper)
    return unique


def compute_tfidf_vectors(corpus: list[str]) -> list[dict[str, float]]:
    tokenized = [re.findall(r'\w+', doc.lower()) for doc in corpus]
    N = len(corpus)
    if N == 0: return []
    
    df = collections.defaultdict(int)
    for tokens in tokenized:
        for term in set(tokens):
            df[term] += 1
            
    idf = {term: math.log(N / float(count)) for term, count in df.items()}
    
    vectors = []
    for tokens in tokenized:
        vec = collections.defaultdict(float)
        tf = collections.defaultdict(int)
        for term in tokens:
            tf[term] += 1
        for term, count in tf.items():
            vec[term] = count * idf[term]
        
        norm = math.sqrt(sum(v*v for v in vec.values()))
        if norm > 0:
            for term in vec:
                vec[term] /= norm
        vectors.append(vec)
    return vectors

def semantic_rerank(papers: list[Paper], query: str) -> list[Paper]:
    if not papers:
        return []
    corpus = [f"{p.title}. {p.summary}" for p in papers]
    scores = semantic_scores(query, corpus)
    for paper, score in zip(papers, scores):
        paper.semantic_score = round(float(score), 6)
    return sorted(
        papers,
        key=lambda item: (item.semantic_score, item.score, item.recency_score),
        reverse=True,
    )



def extract_research_gap(papers: list[Paper], area: str) -> AnalysisResult:
    if not papers:
        raise LiteratureAgentError("No papers available for gap extraction.")
        
    context = ""
    for i, p in enumerate(papers[:5], 1):
        context += f"Paper {i}: {p.title}\nAbstract: {p.summary}\n\n"
        
    prompt = textwrap.dedent(f'''\
        You are a research analyst identifying gaps in the field of {area}.
        Funding Context: {current_funding_prompt_context.get()}
        Read the following top papers and extract the overall problem being tackled, 
        the specific research gap or unaddressed challenge, and a key insight.

        Papers:
        {context}

        Return JSON only with keys:
        - problem: 1 sentence summarizing the shared problem
        - gap: 1 sentence explaining what is missing or unresolved
        - insight: 1 short sentence describing a potential path forward
        - keyword_tags: up to 5 lowercase keywords
        - concept_tags: up to 3 concepts
        - no markdown
    ''')
    
    response = llm_generate(prompt)
    try:
        parsed = json.loads(response)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", response, flags=re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
        else:
            parsed = {}
            
    return AnalysisResult(
        problem=cleaned_text(parsed.get("problem", "Could not extract problem.")),
        gap=cleaned_text(parsed.get("gap", "Could not extract gap.")),
        insight=cleaned_text(parsed.get("insight", "Could not extract insight.")),
        keyword_tags=parsed.get("keyword_tags", []),
        concept_tags=parsed.get("concept_tags", []),
        related_titles=[p.title for p in papers[:3]],
        source="Multi-source Synthesis"
    )

def analyze_papers(papers: list[Paper], area: str) -> AnalysisResult:
    best_papers = sorted(papers, key=lambda p: (p.semantic_score + p.score), reverse=True)
    return extract_research_gap(best_papers, area)


def process_area(connection, area: str | None, output_dir: Path, now: datetime, *, allow_llm_analysis: bool = False, funding_context: FundingCallContext | None = None) -> int:
    LOGGER.info("Starting collection for area: %s", area)
    
    all_papers = []
    all_papers.extend(fetch_arxiv_entries(area))
    all_papers.extend(fetch_openalex_entries(area))
    all_papers.extend(fetch_semanticscholar_entries(area))
    all_papers.extend(fetch_crossref_entries(area))
    all_papers.extend(fetch_dblp_entries(area))
    
    unique_papers = deduplicate_papers(all_papers)
    
    scored_papers = []
    for p in unique_papers:
        paper_year = p.published.year if p.published else 0
        if paper_exists(connection, title=p.title, url=p.url, doi=p.doi or "", source_id=p.source_id, authors="; ".join(p.authors), year=paper_year):
            continue
        cutoff = now - timedelta(days=LOOKBACK_YEARS * 365)
        if p.published is not None and p.published < cutoff:
            continue
        scored = score_paper(p, area, now)
        if scored:
            scored_papers.append(scored)
            
    query = build_query(area) if area else " ".join((current_funding_queries.get() or [])[:4])
    if not query:
        query = "research funding opportunity"
    semantically_ranked = semantic_rerank(scored_papers, query)
    
    if not semantically_ranked:
        LOGGER.info("No valid new papers found for %s", area)
        return 0

    if allow_llm_analysis:
        try:
            analysis = analyze_papers(semantically_ranked, area)
        except Exception as exc:
            LOGGER.error("LLM gap extraction failed; synthetic fallback is disabled: %s", exc)
            raise LiteratureAgentError(f"Evidence-backed literature synthesis unavailable: {exc}") from exc
    else:
        top = semantically_ranked[0]
        # Deterministic collection-stage representation. Do not invent a research gap.
        analysis = AnalysisResult(
            problem=top.title,
            gap="Gap synthesis deferred until cross-agent evidence is combined.",
            insight=cleaned_text(top.summary)[:280],
            keyword_tags=[k.lower() for k in get_keywords(area)[:5]],
            concept_tags=[],
            related_titles=[p.title for p in semantically_ranked[:5]],
            source="Collection-only deterministic"
        )
        
    effective_area = area or "Unscoped"
    for p in semantically_ranked:
        save_paper_record(
            connection,
            title=p.title,
            summary=p.summary,
            url=p.url,
            authors="; ".join(p.authors),
            year=(p.published.year if p.published else 0),
            research_area=effective_area,
            path=str(output_dir / f"{re.sub(r'[^a-zA-Z0-9]+', '-', effective_area.lower()).strip('-')}.md"),
            tags=["#Literature", STATIC_TAGS.get(area, "")],
            concept_tags=[],
            static_tags_by_area=STATIC_TAGS,
            source_id=p.source_id,
            doi=p.doi,
        )
        
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", effective_area.lower()).strip("-")
    md_path = output_dir / f"{slug}.md"
    
    lines = [
        f"# Research Gap: {effective_area}", "",
        "## Problem", analysis.problem, "",
        "## Gap", analysis.gap, "",
        "## Insight", analysis.insight, "",
        "## Keywords", " ".join(f"#{k}" for k in analysis.keyword_tags), "",
        "## Sources Used",
    ]
    for p in semantically_ranked[:5]:
        lines.append(f"- **{p.title}** ({p.source})")
        lines.append(f"  *DOI: {p.doi or 'N/A'}, Authors: {', '.join(p.authors[:3])}*")
    
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOGGER.info("Saved analysis for %s to %s", area, md_path)
    return len(semantically_ranked)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and analyze literature for RIF.")
    parser.add_argument("--mode", choices=["auto"], required=True, help="Input mode")
    parser.add_argument("--area", help="Research area for collection")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging verbosity")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for markdown outputs")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite database path for paper memory")
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    configure_logging(args.log_level)

    if args.mode != "auto" or not args.area:
        LOGGER.error("Literature agent only supports 'auto' mode with '--area' specified")
        return 1

    connection = db_connect(Path(args.db_path))
    init_papers_db(connection)
    now = datetime.now(UTC)

    try:
        process_area(connection, args.area, Path(args.output_dir), now, allow_llm_analysis=True)
    except Exception as exc:
        LOGGER.error("Literature collection failed: %s", exc)
        return 1

    return 0


def run_agent(mode: str, area: str | None = None, input_data: dict | None = None, funding_context: FundingCallContext | None = None, funding_contexts: list[FundingCallContext] | None = None) -> dict:
    from datetime import UTC, datetime
    from core.agent_interface import (
        default_intermediate_output_dir,
        finalize_collection_agent_response,
        finalize_file_agent_response,
        snapshot_markdown_files,
        validate_run_input,
    )
    from core.schemas import AREA_KEYWORDS, create_error_response

    agent_name = "literature"
    layer = "Literature"
    payload = input_data or {}
    started_at = datetime.now(UTC)
    validation_errors = validate_run_input(mode, payload)
    if validation_errors:
        return create_error_response(
            agent=agent_name, layer=layer, mode=mode, area=area,
            errors=validation_errors, started_at=started_at, finished_at=datetime.now(UTC),
        )

    output_dir = Path(payload.get("output_dir", default_intermediate_output_dir(agent_name)))
    before_snapshot = snapshot_markdown_files(output_dir)
    try:
        if mode == "configured_scan":
            connection = db_connect(Path(payload.get("db_path", DEFAULT_DB_PATH)))
            init_papers_db(connection)
            scan_area = area
            if not scan_area and funding_context:
                # Adaptive/unscoped mode: derive compact research terms from the selected call
                # rather than forcing the call into a fixed taxonomy.
                scan_area = None
            count = process_area(connection, scan_area, output_dir, datetime.now(UTC), allow_llm_analysis=bool(payload.get("allow_llm_analysis", False)), funding_context=funding_context)
            return finalize_file_agent_response(
                agent=agent_name, layer=layer, mode=mode, area=area,
                items_processed=count, items_saved=count, output_dir=output_dir,
                before_snapshot=before_snapshot, problem_headings=("Gap", "Insight"),
                started_at=started_at,
            )

        if mode == "manual_url":
            raise LiteratureAgentError("manual_url is not supported for literature because a single URL is not a bibliographic source. Use manual_text or configured_scan.")

        text = cleaned_text(payload["text"])
        title = cleaned_text(payload.get("title") or "Manual Literature Signal")
        paper = Paper(
            title=title,
            summary=text,
            url=cleaned_text(payload.get("source") or f"manual://literature/{re.sub(r'[^a-zA-Z0-9]+', '-', title.lower()).strip('-')}"),
            published=None,  # Manual entry — publication date unknown
            authors=["Unknown"],
            source="Manual",
            source_id="manual:" + re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-"),
            doi=None,
            venue=None,
            retrieval_timestamp=started_at.isoformat(),
        )
        response = finalize_collection_agent_response(
        funding_context=funding_context,
            funding_contexts=funding_contexts,
        run_id=(input_data or {}).get("run_id"),
        agent=agent_name, layer=layer, mode=mode, area=area,
            documents=[paper], output_dir=output_dir, started_at=started_at,
            default_source=paper.url,
        )
        return response
    except LiteratureAgentError as exc:
        return create_error_response(
            agent=agent_name, layer=layer, mode=mode, area=area, errors=[str(exc)],
            started_at=started_at, finished_at=datetime.now(UTC),
        )
    except Exception as exc:
        LOGGER.exception("Literature agent failed")
        return create_error_response(
            agent=agent_name, layer=layer, mode=mode, area=area, errors=[str(exc)],
            started_at=started_at, finished_at=datetime.now(UTC),
        )


if __name__ == "__main__":
    raise SystemExit(main())
