from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import textwrap
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.normalization import ensure_normalized_collection_records, save_normalized_collection_records
from core.intelligence_quality import clean, evidence_text, problem_signature
from core.semantic_retrieval import semantic_scores
from core.schemas import create_error_response, create_partial_response, create_success_response, normalize_area, score_research_areas
from core.llm_provider import generate as llm_generate

LOGGER = logging.getLogger("rif.tagging_agent")

DEFAULT_OUTPUT_DIR = Path("outputs/processed/tags")
DEFAULT_NORMALIZED_ROOT = Path("outputs")
AGENT_NAME = "tagging"
LAYER_NAME = "Processing"
AGENT_VERSION = "v1"
ALLOWED_SIGNAL_TAGS = {
    "Funding": "#signal-funding",
    "Company": "#signal-company",
    "Regulation": "#signal-regulation",
    "OpenSource": "#signal-opensource",
    "Practitioner": "#signal-practitioner",
    "Investment": "#signal-investment",
    "Failure": "#signal-failure",
    "DataAvailability": "#signal-data-availability",
    "Hackathon": "#signal-hackathon",
    "Literature": "#signal-literature",
    "Lab": "#signal-lab",
}
TOPIC_KEYWORDS = {
    "oracle": ("oracle", "data feed", "proof of reserve"),
    "compliance": ("compliance", "reporting", "disclosure", "assurance"),
    "identity": ("identity", "credential", "did", "authentication"),
    "privacy": ("privacy", "zero-knowledge", "zk", "verifier"),
    "settlement": ("settlement", "payment rails", "payments"),
    "stablecoin": ("stablecoin", "reserve", "depeg"),
    "liquidity": ("liquidity", "amm", "pool"),
    "carbon": ("carbon", "mrv", "emissions", "registry"),
    "depin": ("depin", "wireless", "sensor", "storage"),
    "mev": ("mev", "block builder", "transaction ordering", "relay"),
    "prototype": ("prototype", "builder", "hackathon", "demo"),
    "benchmark": ("benchmark", "simulator", "dataset", "test suite", "evaluation"),
    "security": ("exploit", "vulnerability", "incident", "postmortem", "audit"),
    "governance": ("governance", "tokenomics", "incentive", "proposal"),
    "interoperability": ("interoperability", "cross-chain", "integration"),
    "scalability": ("scalability", "throughput", "latency", "high volume", "bottleneck", "transaction volume"),
}
SIGNAL_TYPE_KEYWORDS = {
    "problem-statement": ("problem statement", "challenge", "gap"),
    "requirement": ("requirement", "required", "must", "shall"),
    "roadmap": ("roadmap", "proposal", "milestone", "release"),
    "pain-point": ("pain point", "friction", "constraint"),
    "funding": ("grant", "funding", "award", "rfp"),
    "failure": ("incident", "exploit", "hack", "depeg"),
    "benchmark": ("benchmark", "dataset", "simulator", "test suite"),
    "investment": ("investment", "portfolio", "capital flow", "funding round"),
}


TAG_THRESHOLDS = {
    "explicit": 0.60,
    "extracted": 0.65,
    "inferred": 0.75,
}
TAG_PROTOTYPES = {
    "oracle": "oracle data feeds proof of reserve external data verification",
    "compliance": "regulatory compliance reporting disclosure assurance requirements",
    "identity": "digital identity credentials decentralized identity authentication",
    "privacy": "privacy preserving zero knowledge confidentiality secure verification",
    "settlement": "payments settlement payment rails transaction settlement",
    "stablecoin": "stablecoins reserves depeg stablecoin payments settlement",
    "liquidity": "liquidity automated market maker pools liquidity provision",
    "carbon": "carbon emissions MRV carbon registry environmental reporting",
    "depin": "decentralized physical infrastructure wireless sensors storage",
    "mev": "maximal extractable value block building transaction ordering relay",
    "prototype": "prototype builder hackathon demonstration demo implementation",
    "benchmark": "benchmark dataset simulator test suite evaluation metrics",
    "security": "security exploit vulnerability incident postmortem audit",
    "governance": "governance tokenomics incentives proposals governance mechanisms",
    "interoperability": "interoperability cross chain integration communication between systems",
    "scalability": "scalability throughput latency high volume transaction bottleneck performance",
}
SIGNAL_PROTOTYPES = {
    key: " ".join(values) for key, values in SIGNAL_TYPE_KEYWORDS.items()
}
NEGATION_TERMS = ("not", "no", "without", "does not", "doesn't", "do not", "don't", "never", "unlikely", "unlikely to")

AMBIGUOUS_SEMANTIC_THRESHOLD = 0.78
LLM_TAG_MIN_CONFIDENCE = 0.70

def _llm_tag_decision(*, tag: str, text: str, evidence: list[str], category: str, semantic_score: float) -> dict[str, Any]:
    """Use the local Ollama LLM only for ambiguous tag meaning.

    The LLM is a bounded reasoner: it cannot invent a tag, change the taxonomy,
    or override missing evidence. It only decides whether the supplied evidence
    supports the already-selected candidate tag.
    """
    prompt = (
        f"You are RIF's tagging verifier. Decide whether the supplied evidence supports an already-selected candidate tag.\n"
        "Return JSON only with keys: supported (boolean), confidence (number 0-1), reason (string).\n"
        "Do not invent tags. Do not use outside knowledge. If evidence is insufficient or only a passing mention, return supported=false.\n\n"
        f"Candidate tag: {tag}\n"
        f"Category: {category}\n"
        f"Semantic similarity score: {semantic_score:.3f}\n"
        "Evidence:\n"
        + "\n".join(f"- {item}" for item in evidence[:3])
        + f"\n\nDocument context:\n{text[:4000]}"
    )
    try:
        raw = llm_generate(prompt)
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise ValueError("LLM response did not contain a JSON object")
        parsed = json.loads(match.group(0))
        supported = bool(parsed.get("supported", False))
        confidence = float(parsed.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
        reason = cleaned_text(str(parsed.get("reason", "")))
        return {"supported": supported and confidence >= LLM_TAG_MIN_CONFIDENCE, "confidence": confidence, "reason": reason, "raw": raw}
    except Exception as exc:
        LOGGER.warning("Ambiguous tag LLM verification failed for %s: %s", tag, exc)
        return {"supported": False, "confidence": 0.0, "reason": f"LLM verification unavailable: {exc}", "raw": ""}

def _contains_negation(text: str, hit: str) -> bool:
    normalized = re.sub(r"\s+", " ", (text or "").lower()).strip()
    normalized_hit = re.sub(r"[-_/]+", " ", hit.lower()).strip()
    for match in re.finditer(rf"(?<![a-z0-9]){re.escape(normalized_hit)}(?![a-z0-9])", normalized):
        window = normalized[max(0, match.start()-45):match.start()]
        if any(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", window) for term in NEGATION_TERMS):
            return True
    return False

def _evidence_strength(excerpts: list[str], *, explicit: bool, inferred: bool) -> float:
    if not excerpts:
        return 0.0
    base = 0.45 + min(0.30, 0.10 * len(excerpts))
    if explicit:
        base += 0.20
    elif inferred:
        base += 0.05
    return min(1.0, base)

def _source_quality(record: dict[str, Any]) -> float:
    source_type = cleaned_text(str(record.get("source_type", ""))).lower()
    evidence_type = cleaned_text(str(record.get("evidence_type", ""))).lower()
    strong_sources = {"literature", "lab", "regulation", "funding", "company", "opensource", "failure", "expert", "practitioner", "investment", "dataavailability", "hackathon"}
    score = 0.55
    if source_type.replace(" ", "") in strong_sources:
        score += 0.20
    if evidence_type and evidence_type not in {"unknown", "manual"}:
        score += 0.10
    if record.get("source_url"):
        score += 0.10
    return min(1.0, score)

def _fallback_evidence_excerpts(record: dict[str, Any], limit: int = 2) -> list[str]:
    candidates: list[str] = []
    for key in ("problem_statement", "context_summary", "evidence_snippets", "excerpt_windows"):
        value = record.get(key, [])
        values = value if isinstance(value, list) else [value]
        candidates.extend(" ".join(str(item).split()) for item in values if str(item).strip())
    return list(dict.fromkeys(candidates))[:limit]


def _semantic_tag_scores(text: str) -> dict[str, float]:
    if not text.strip():
        return {}
    names = list(TAG_PROTOTYPES)
    docs = [TAG_PROTOTYPES[name] for name in names]
    try:
        scores = semantic_scores(text, docs)
        return {name: float(score) for name, score in zip(names, scores)}
    except Exception:
        return {name: 0.0 for name in names}

def _semantic_signal_scores(text: str) -> dict[str, float]:
    if not text.strip():
        return {}
    names = list(SIGNAL_PROTOTYPES)
    docs = [SIGNAL_PROTOTYPES[name] for name in names]
    try:
        scores = semantic_scores(text, docs)
        return {name: float(score) for name, score in zip(names, scores)}
    except Exception:
        return {name: 0.0 for name in names}


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def cleaned_text(value: str) -> str:
    return " ".join((value or "").split()).strip()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "item"


def normalize_tag(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return f"#{text}" if text else ""


def load_normalized_records(outputs_root: Path) -> list[dict[str, Any]]:
    records_path = outputs_root / "normalized" / "records.json"
    ensure_normalized_collection_records(outputs_root)
    if not records_path.exists():
        return []
    try:
        data = json.loads(records_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def build_manual_record(*, text: str, title: str, source: str, area: str | None) -> dict[str, Any]:
    normalized_area = normalize_area(area) or "Unscoped"
    combined = cleaned_text(text)
    area_scores = score_research_areas(f"{title}\n{combined}")
    record_id = slugify(f"{title}-{normalized_area}")[:24]
    tokens = sorted(set(re.findall(r"[a-zA-Z0-9]+", combined.lower())))
    return {
        "record_id": record_id,
        "file_path": "",
        "record_kind": "manual_normalized",
        "agent_name": "manual",
        "layer": "Unknown",
        "research_area": normalized_area,
        "title": title or "Manual Record",
        "source_url": source,
        "collected_at": datetime.now(UTC).isoformat(),
        "collection_mode": "manual_text",
        "source_type": "Manual",
        "evidence_type": "Manual",
        "actor": "Manual",
        "focus": normalized_area,
        "problem_statement": combined[:280],
        "context_summary": combined[:1200],
        "evidence_snippets": [combined[:320]] if combined else [],
        "excerpt_windows": [combined[:500]] if combined else [],
        "named_entities": [],
        "linked_urls": [source] if source else [],
        "keywords": [token for token in tokens if len(token) > 2][:20],
        "agent_specific_fields": {},
        "machine_metadata": {"manual_area_scores": area_scores},
        "search_text": combined,
        "tokens": [token for token in tokens if len(token) > 2],
    }


def filter_records(records: list[dict[str, Any]], *, area: str | None = None, source_url: str | None = None) -> list[dict[str, Any]]:
    normalized_area = normalize_area(area) if area else None
    filtered: list[dict[str, Any]] = []
    for record in records:
        if normalized_area and normalize_area(record.get("research_area")) != normalized_area:
            continue
        if source_url and cleaned_text(str(record.get("source_url", ""))) != cleaned_text(source_url):
            continue
        filtered.append(record)
    return filtered


def _keyword_hits(text: str, keywords: tuple[str, ...] | list[str]) -> list[str]:
    normalized_text = re.sub(r"[-_/]+", " ", (text or "").lower())
    normalized_text = re.sub(r"\s+", " ", normalized_text).strip()
    hits: list[str] = []
    for keyword in keywords:
        normalized_keyword = re.sub(r"[-_/]+", " ", keyword.lower()).strip()
        if not normalized_keyword:
            continue
        pattern = rf"(?<![a-z0-9]){re.escape(normalized_keyword)}(?![a-z0-9])"
        if re.search(pattern, normalized_text):
            hits.append(keyword)
    return hits


def _evidence_excerpts(record: dict[str, Any], hits: list[str], limit: int = 3) -> list[str]:
    candidates = []
    for key in ("problem_statement", "context_summary", "evidence_snippets", "excerpt_windows"):
        value = record.get(key, [])
        values = value if isinstance(value, list) else [value]
        candidates.extend(" ".join(str(item).split()) for item in values if str(item).strip())
    excerpts: list[str] = []
    for candidate in candidates:
        lowered = candidate.lower()
        if any(re.search(rf"(?<![a-z0-9]){re.escape(re.sub(r'[-_/]+', ' ', hit.lower()))}(?![a-z0-9])", re.sub(r"[-_/]+", " ", lowered)) for hit in hits):
            if candidate not in excerpts:
                excerpts.append(candidate[:320])
        if len(excerpts) >= limit:
            break
    return excerpts


def build_tags_for_record(record: dict[str, Any]) -> dict[str, Any]:
    title = cleaned_text(str(record.get("title", "")))
    layer = cleaned_text(str(record.get("layer", ""))) or "Unknown"
    research_area = normalize_area(record.get("research_area")) or "Unscoped"
    text = evidence_text(record)
    area_scores = score_research_areas(text)
    best_area = research_area
    if area_scores:
        candidate = max(area_scores, key=area_scores.get)
        if area_scores.get(candidate, 0) > max(area_scores.get(research_area, 0), 0) + 1:
            best_area = candidate

    research_area_tags = [normalize_tag(best_area)] if best_area and best_area != "Unscoped" else []
    layer_tag = normalize_tag(layer)
    signal_tag = ALLOWED_SIGNAL_TAGS.get(layer, "#signal-unknown")
    layer_tags = list(dict.fromkeys([layer_tag, signal_tag]))

    topic_tags: list[str] = []
    signal_type_tags: list[str] = []
    evidence_spans: dict[str, list[str]] = {}
    tag_decisions: dict[str, dict[str, Any]] = {}
    semantic_topics = _semantic_tag_scores(text)
    semantic_signals = _semantic_signal_scores(text)

    def evaluate_tag(tag: str, hits: list[str], *, category: str, semantic_score: float = 0.0) -> None:
        normalized_hits = list(dict.fromkeys(hits))
        excerpts = _evidence_excerpts(record, normalized_hits) if normalized_hits else _fallback_evidence_excerpts(record)
        if normalized_hits and not excerpts:
            # A keyword can occur only in metadata/title while the supporting
            # evidence lives in the body fields. Fall back to body evidence,
            # but do not treat the keyword alone as proof.
            excerpts = _fallback_evidence_excerpts(record)
        negated = any(_contains_negation(text, hit) for hit in normalized_hits)
        evidence_exists = bool(excerpts)
        explicit = bool(normalized_hits) and not negated
        extracted = explicit and category in {"topic", "signal_type"} and len(normalized_hits) > 0
        origin = "explicit" if explicit and category == "topic" else ("extracted" if extracted else "inferred")

        # Decision gate: if there is evidence but the meaning is not sufficiently
        # clear from explicit matching/semantic similarity, ask the local LLM.
        meaning_clear = explicit or (semantic_score >= AMBIGUOUS_SEMANTIC_THRESHOLD and evidence_exists and not negated)
        llm_check: dict[str, Any] | None = None
        if not evidence_exists:
            inferred = False
            llm_check = {"used": False, "supported": False, "confidence": 0.0, "reason": "No supporting evidence found."}
        elif negated:
            inferred = False
            llm_check = {"used": False, "supported": False, "confidence": 0.0, "reason": "Evidence is negated."}
        elif not meaning_clear:
            llm_check = _llm_tag_decision(tag=tag, text=text, evidence=excerpts, category=category, semantic_score=semantic_score)
            llm_check["used"] = True
            inferred = bool(llm_check["supported"])
            if not inferred:
                origin = "inferred"
        else:
            inferred = not explicit
            llm_check = {"used": False, "supported": True, "confidence": semantic_score if inferred else 1.0, "reason": "Meaning sufficiently clear without LLM."}

        evidence_strength = _evidence_strength(excerpts, explicit=explicit, inferred=inferred)
        explicitness = 1.0 if explicit else 0.35 if inferred else 0.0
        context_relevance = min(1.0, 0.55 + (0.15 if record.get("problem_statement") else 0.0) + (0.10 if record.get("context_summary") else 0.0))
        source_quality = _source_quality(record)
        confidence = (
            0.30 * evidence_strength
            + 0.25 * max(semantic_score, 0.0)
            + 0.20 * explicitness
            + 0.15 * context_relevance
            + 0.10 * source_quality
        )
        if llm_check and llm_check.get("used", False):
            # LLM reasoning is a bounded supporting signal, never the sole source
            # of truth. Blend it into the evidence-based score.
            confidence = 0.50 * confidence + 0.50 * float(llm_check.get("confidence", 0.0))
        if origin == "explicit":
            confidence = min(1.0, confidence + 0.05)
        elif origin == "extracted":
            confidence = min(1.0, confidence + 0.03)
        threshold = TAG_THRESHOLDS[origin]
        taxonomy_valid = tag in {f"#topic-{name}" for name in TOPIC_KEYWORDS} or tag in {f"#signal-type-{name}" for name in SIGNAL_TYPE_KEYWORDS}
        evidence_valid = evidence_exists and not negated
        llm_supported = bool(llm_check and llm_check.get("supported", False))
        meaning_supported = meaning_clear or llm_supported
        accepted = taxonomy_valid and evidence_valid and meaning_supported and confidence >= threshold
        decision = "accepted" if accepted else "rejected"
        if excerpts:
            evidence_spans[tag] = excerpts
        tag_decisions[tag] = {
            "decision": decision,
            "origin": origin,
            "confidence": round(confidence, 3),
            "threshold": threshold,
            "semantic_score": round(max(0.0, min(1.0, semantic_score)), 3),
            "evidence_strength": round(evidence_strength, 3),
            "evidence_span": excerpts,
            "negated": negated,
            "taxonomy_valid": taxonomy_valid,
            "evidence_valid": evidence_valid,
            "meaning_clear": meaning_clear,
            "llm_used": bool(llm_check and llm_check.get("used", False)),
            "llm_supported": llm_supported,
            "llm_confidence": round(float(llm_check.get("confidence", 0.0)), 3) if llm_check else 0.0,
            "llm_reason": llm_check.get("reason", "") if llm_check else "",
        }
        if accepted:
            if category == "topic":
                topic_tags.append(tag)
            else:
                signal_type_tags.append(tag)

    for topic, keywords in TOPIC_KEYWORDS.items():
        hits = _keyword_hits(text, keywords)
        semantic_score = semantic_topics.get(topic, 0.0)
        if hits or semantic_score >= 0.58:
            evaluate_tag(f"#topic-{topic}", hits, category="topic", semantic_score=semantic_score)

    for signal_type, keywords in SIGNAL_TYPE_KEYWORDS.items():
        hits = _keyword_hits(text, keywords)
        semantic_score = semantic_signals.get(signal_type, 0.0)
        if hits or semantic_score >= 0.58:
            evaluate_tag(f"#signal-type-{signal_type}", hits, category="signal_type", semantic_score=semantic_score)

    # Deterministic system tags are not treated as inferred semantic claims.
    all_tags = list(dict.fromkeys(["#Processing", "#Tagging"] + research_area_tags + layer_tags + topic_tags + signal_type_tags))[:16]
    explicit_tags = set(str(x) for x in record.get("tags", []) if str(x).startswith("#"))
    inferred_tags = [tag for tag in all_tags if tag not in explicit_tags and tag not in {"#Processing", "#Tagging"}]
    for tag in explicit_tags:
        if tag not in tag_decisions:
            tag_decisions[tag] = {
                "decision": "accepted",
                "origin": "explicit",
                "confidence": 0.85,
                "threshold": TAG_THRESHOLDS["explicit"],
                "semantic_score": 1.0,
                "evidence_strength": 0.85,
                "evidence_span": [],
                "negated": False,
                "taxonomy_valid": True,
                "evidence_valid": True,
            }

    accepted_scores = [info["confidence"] for info in tag_decisions.values() if info["decision"] == "accepted"]
    confidence_score = round(sum(accepted_scores) / len(accepted_scores), 3) if accepted_scores else 0.0
    confidence_label = "High" if confidence_score >= 0.80 else "Medium" if confidence_score >= 0.55 else "Low"
    signature = problem_signature({**record, "research_area": best_area, "topic_tags": topic_tags, "signal_type_tags": signal_type_tags})
    return {
        "record_id": record.get("record_id"),
        "funding_call_id": record.get("funding_call_id"),
        "funding_call_ids": record.get("funding_call_ids", []),
        "title": title or "Untitled",
        "source_url": cleaned_text(str(record.get("source_url", ""))) or "Unknown",
        "original_layer": layer,
        "original_research_area": research_area,
        "resolved_research_area": best_area,
        "layer_tags": layer_tags,
        "research_area_tags": research_area_tags,
        "topic_tags": topic_tags,
        "signal_type_tags": signal_type_tags,
        "all_tags": all_tags,
        "tagging_confidence": confidence_label,
        "tagging_confidence_score": confidence_score,
        "tag_origin": {"explicit": sorted(explicit_tags), "inferred": inferred_tags},
        "tag_decisions": tag_decisions,
        "tag_evidence": evidence_spans,
        "problem_signature": signature,
        "why_tagged": cleaned_text(
            "; ".join(filter(None, [
                f"resolved area: {best_area}",
                f"layer: {layer}",
                f"signal types: {', '.join(signal_type_tags)}" if signal_type_tags else "",
                f"topics: {', '.join(topic_tags)}" if topic_tags else "",
            ]))
        ),
        "problem": cleaned_text(str(record.get("problem_statement", ""))) or title,
        "search_snapshot": cleaned_text(str(record.get("search_text", "")))[:600],
        "file_path": cleaned_text(str(record.get("file_path", ""))),
        "record": record,
    }


def build_markdown(tagged_record: dict[str, Any]) -> str:
    return textwrap.dedent(
        f"""\
        # Tagged Signal: {tagged_record['title']}

        ## Problem
        {tagged_record['problem']}

        ## Source
        {tagged_record['source_url']}

        ## Funding Call IDs
        {', '.join(tagged_record.get('funding_call_ids', [])) or str(tagged_record.get('funding_call_id') or 'Unknown')}

        ## Layer
        Tagging

        ## Research Area
        {tagged_record['resolved_research_area']}

        ## Why Important
        This processing artifact improves discoverability and downstream synthesis by making research-area, layer, topic, and signal-type tags explicit.

        ## Existing Solutions
        Existing normalized record from layer {tagged_record['original_layer']}.

        ## Gap
        The raw normalized record did not yet expose a consistent processing-layer tag bundle.

        ## Idea
        Use this tagged record as a cleaner input for clustering, trend detection, and cross-layer synthesis.

        ## Feasibility
        High

        ## Layer Tags
        {' '.join(tagged_record['layer_tags']) or '#none'}

        ## Research Area Tags
        {' '.join(tagged_record['research_area_tags']) or '#none'}

        ## Topic Tags
        {' '.join(tagged_record['topic_tags']) or '#none'}

        ## Signal-Type Tags
        {' '.join(tagged_record['signal_type_tags']) or '#none'}

        ## Tagging Confidence
        {tagged_record['tagging_confidence']} ({tagged_record.get('tagging_confidence_score', 0)})

        ## Tag Evidence
        {json.dumps(tagged_record.get('tag_evidence', {}), ensure_ascii=False)}

        ## Why Tagged
        {tagged_record['why_tagged'] or 'Heuristic tagging based on normalized content.'}

        ## Source Record
        {tagged_record['file_path'] or 'Manual record'}

        ## Search Text Snapshot
        {tagged_record['search_snapshot'] or 'No search snapshot available.'}

        ## Tags
        {' '.join(tagged_record['all_tags'])}
        """
    )


def save_tagged_record(tagged_record: dict[str, Any], output_dir: Path) -> Path:
    area_dir = slugify(tagged_record["resolved_research_area"] or "unscoped")
    target_dir = output_dir / area_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{slugify(tagged_record['title'])}-{tagged_record['record_id']}.md"
    path.write_text(build_markdown(tagged_record), encoding="utf-8")
    return path


def save_summary(tagged_records: list[dict[str, Any]], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "tagged_records.json"
    summary_path = output_dir / "insights.md"
    json_payload = [
        {
            "record_id": item.get("record_id"),
            "funding_call_id": item.get("funding_call_id"),
            "funding_call_ids": item.get("funding_call_ids", []),
            "title": item["title"],
            "resolved_research_area": item["resolved_research_area"],
            "original_layer": item["original_layer"],
            "layer_tags": item["layer_tags"],
            "research_area_tags": item["research_area_tags"],
            "topic_tags": item["topic_tags"],
            "signal_type_tags": item["signal_type_tags"],
            "tagging_confidence": item["tagging_confidence"],
            "tagging_confidence_score": item["tagging_confidence_score"],
            "tag_origin": item["tag_origin"],
            "tag_decisions": item.get("tag_decisions", {}),
            "tag_evidence": item["tag_evidence"],
            "problem_signature": item["problem_signature"],
            "source_url": item["source_url"],
            "file_path": item["file_path"],
        }
        for item in tagged_records
    ]
    json_path.write_text(json.dumps(json_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    area_counts = Counter(item["resolved_research_area"] for item in tagged_records)
    topic_counts = Counter(tag for item in tagged_records for tag in item["topic_tags"])
    signal_counts = Counter(tag for item in tagged_records for tag in item["signal_type_tags"])
    layer_counts = Counter(item["original_layer"] for item in tagged_records)

    lines = [
        "# Tagging Insights",
        "",
        "## Problem",
        "Provide explicit processing-layer tags for normalized RIF records.",
        "",
        "## Source",
        str(json_path.resolve()),
        "",
        "## Layer",
        "Tagging",
        "",
        "## Research Area",
        "Multi-Area",
        "",
        "## Why Important",
        "Consistent tags improve search, clustering, trend detection, and downstream synthesis quality.",
        "",
        "## Existing Solutions",
        "Normalized records already preserve structured evidence, but explicit processor-level tag bundles were missing.",
        "",
        "## Gap",
        "Cross-agent topic and signal-type tagging was not yet materialized as reusable artifacts.",
        "",
        "## Idea",
        "Use the tagged record set as the primary input for clustering, trend, and synthesis agents.",
        "",
        "## Feasibility",
        "High",
        "",
        "## Area Distribution",
    ]
    for area, count in sorted(area_counts.items()):
        lines.append(f"- {area}: {count}")
    lines.extend(["", "## Layer Distribution"])
    for layer, count in sorted(layer_counts.items()):
        lines.append(f"- {layer}: {count}")
    lines.extend(["", "## Top Topic Tags"])
    for tag, count in topic_counts.most_common(12):
        lines.append(f"- {tag}: {count}")
    lines.extend(["", "## Top Signal-Type Tags"])
    for tag, count in signal_counts.most_common(12):
        lines.append(f"- {tag}: {count}")
    lines.extend(["", "## Tags", "#Processing #Tagging #Insights"])
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path, json_path


def run_agent(
    mode: str,
    area: str | None = None,
    input_data: dict[str, Any] | None = None,
    funding_context=None
) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    payload = input_data or {}
    output_dir = Path(payload.get("output_dir", DEFAULT_OUTPUT_DIR))
    outputs_root = Path(payload.get("outputs_root", DEFAULT_NORMALIZED_ROOT))

    LOGGER.info("Starting tagging agent run: mode=%s area=%s", mode, area)
    try:
        if mode == "configured_scan":
            records = load_normalized_records(outputs_root)
            filtered = filter_records(records, area=area)
        elif mode == "manual_url":
            records = load_normalized_records(outputs_root)
            url = cleaned_text(str(payload.get("url", "")))
            if not url:
                return create_error_response(
                    agent=AGENT_NAME,
                    layer=LAYER_NAME,
                    mode=mode,
                    area=area,
                    errors=["manual_url mode requires input_data['url']"],
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                )
            filtered = filter_records(records, area=area, source_url=url)
        elif mode == "manual_text":
            text = cleaned_text(str(payload.get("text", "")))
            if not text:
                return create_error_response(
                    agent=AGENT_NAME,
                    layer=LAYER_NAME,
                    mode=mode,
                    area=area,
                    errors=["manual_text mode requires input_data['text']"],
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                )
            title = cleaned_text(str(payload.get("title", ""))) or "Manual Tagging Input"
            source = cleaned_text(str(payload.get("source", ""))) or "manual://tagging/manual-input"
            filtered = [build_manual_record(text=text, title=title, source=source, area=area)]
        else:
            return create_error_response(
                agent=AGENT_NAME,
                layer=LAYER_NAME,
                mode=mode,
                area=area,
                errors=["mode must be one of configured_scan, manual_url, manual_text"],
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        if not filtered:
            return create_partial_response(
                agent=AGENT_NAME,
                layer=LAYER_NAME,
                mode=mode,
                area=area,
                items_processed=0,
                items_saved=0,
                outputs=[],
                warnings=["No normalized records matched the tagging request."],
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        tagged_records = [build_tags_for_record(record) for record in filtered]
        outputs: list[dict[str, Any]] = []
        for tagged_record in tagged_records:
            path = save_tagged_record(tagged_record, output_dir)
            outputs.append(
                {
                    "title": tagged_record["title"],
                    "problem": tagged_record["problem"],
                    "problem_signature": f"{tagged_record['resolved_research_area']} | {tagged_record['original_layer']} | tagging",
                    "confidence": tagged_record["tagging_confidence"],
                    "source": tagged_record["source_url"],
                    "markdown_path": str(path.resolve()),
                }
            )

        summary_path, json_path = save_summary(tagged_records, output_dir)
        outputs.insert(
            0,
            {
                "title": "Tagging Insights",
                "problem": "Explicit processing-layer tagging for normalized RIF records.",
                "problem_signature": "multi-area | processing | tagging-summary",
                "confidence": "High",
                "source": str(json_path.resolve()),
                "markdown_path": str(summary_path.resolve()),
            },
        )

        response = create_success_response(
            agent=AGENT_NAME,
            layer=LAYER_NAME,
            mode=mode,
            area=area,
            items_processed=len(filtered),
            items_saved=len(outputs),
            outputs=outputs,
            warnings=["Tagging agent saved processor artifacts under outputs/processed/tags/."],
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        response["metadata"]["tagging_summary"] = {
            "records_tagged": len(tagged_records),
            "summary_markdown": str(summary_path.resolve()),
            "tagged_records_json": str(json_path.resolve()),
        }
        return response
    except Exception as exc:
        LOGGER.exception("Tagging agent failed during run_agent")
        return create_error_response(
            agent=AGENT_NAME,
            layer=LAYER_NAME,
            mode=mode,
            area=area,
            errors=[str(exc)],
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the tagging processing agent.")
    parser.add_argument("--mode", choices=["configured_scan", "manual_url", "manual_text"], default="configured_scan")
    parser.add_argument("--area")
    parser.add_argument("--url")
    parser.add_argument("--text")
    parser.add_argument("--title")
    parser.add_argument("--source")
    parser.add_argument("--outputs-root", default=str(DEFAULT_NORMALIZED_ROOT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)
    payload = {
        "outputs_root": args.outputs_root,
        "output_dir": args.output_dir,
    }
    if args.url:
        payload["url"] = args.url
    if args.text:
        payload["text"] = args.text
    if args.title:
        payload["title"] = args.title
    if args.source:
        payload["source"] = args.source
    response = run_agent(mode=args.mode, area=args.area, input_data=payload)
    print(json.dumps(response, indent=2, ensure_ascii=False))
    return 0 if response.get("status") != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
