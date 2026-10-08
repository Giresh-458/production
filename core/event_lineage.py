from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Callable

from core.intelligence_quality import clean, parse_date, source_key, tokenize
from core.semantic_retrieval import semantic_scores


EVENT_WORDS = {
    "launch", "launched", "release", "released", "announce", "announced", "announcement",
    "deploy", "deployed", "deployment", "acquire", "acquired", "acquisition", "fund",
    "funded", "funding", "award", "awarded", "grant", "partnership", "partnered",
    "integrate", "integrated", "integration", "publish", "published", "report", "reports",
    "invest", "invested", "investment", "filed", "approval", "approved", "incident",
    "breach", "outage", "release", "introduce", "introduced",
}


def _date_key(record: dict[str, Any]) -> str:
    dt = parse_date(record.get("published_at") or record.get("date") or record.get("collected_at"))
    return dt.strftime("%Y-%m-%d") if dt else ""


def _event_tokens(record: dict[str, Any]) -> set[str]:
    text = " ".join(
        clean(record.get(key))
        for key in ("title", "problem_statement", "problem", "context_summary", "focus", "search_text")
        if clean(record.get(key))
    )
    return tokenize(text)


def _event_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    texts = [
        " ".join(sorted(_event_tokens(left))),
        " ".join(sorted(_event_tokens(right))),
    ]
    if not all(texts):
        return 0.0
    try:
        return float(semantic_scores(texts[0], [texts[1]])[0])
    except Exception:
        a, b = set(texts[0].split()), set(texts[1].split())
        return len(a & b) / max(len(a | b), 1)


def _date_distance_days(left: dict[str, Any], right: dict[str, Any]) -> int | None:
    l = parse_date(left.get("published_at") or left.get("date") or left.get("collected_at"))
    r = parse_date(right.get("published_at") or right.get("date") or right.get("collected_at"))
    if not l or not r:
        return None
    return abs((l - r).days)


def _looks_like_event_pair(left: dict[str, Any], right: dict[str, Any], similarity: float) -> bool:
    if clean(left.get("research_area")).lower() != clean(right.get("research_area")).lower():
        return False
    if source_key(left) and source_key(left) == source_key(right):
        return False
    distance = _date_distance_days(left, right)
    left_tokens = _event_tokens(left)
    right_tokens = _event_tokens(right)
    event_overlap = len((left_tokens & right_tokens) & EVENT_WORDS)
    shared = len(left_tokens & right_tokens)
    # Candidate generation is intentionally conservative. The LLM is only
    # asked about pairs that already have strong topical/event evidence.
    if distance is not None and distance > 14:
        return False
    # Shared entities/event language make a pair worth asking the LLM about.
    # Candidate generation is intentionally broader than the final decision.
    if distance is not None and distance <= 3 and shared >= 2:
        return True
    return similarity >= 0.20 and shared >= 3 and (event_overlap >= 1 or similarity >= 0.45)


def _default_llm_resolver(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any] | None:
    try:
        from core.llm_provider import generate
    except Exception:
        return None

    prompt = f"""You are an evidence provenance classifier for a research-intelligence system.
Decide whether DOCUMENT B is primarily derivative reporting of the same underlying real-world event described by DOCUMENT A.
Do not treat merely similar topics as the same event. If B adds independent investigation or evidence, prefer corroborating.
Return JSON only with keys: relation, confidence, reason.
Allowed relation values: derivative, corroborating, unrelated.

DOCUMENT A:
Title: {clean(left.get('title'))}
Date: {_date_key(left)}
Source: {clean(left.get('source_url'))}
Actor: {clean(left.get('actor'))}
Text: {clean(left.get('problem_statement') or left.get('problem') or left.get('context_summary'))}

DOCUMENT B:
Title: {clean(right.get('title'))}
Date: {_date_key(right)}
Source: {clean(right.get('source_url'))}
Actor: {clean(right.get('actor'))}
Text: {clean(right.get('problem_statement') or right.get('problem') or right.get('context_summary'))}
"""
    try:
        raw = generate(prompt)
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            return None
        data = json.loads(match.group(0))
        relation = str(data.get("relation", "")).strip().lower()
        confidence = float(data.get("confidence", 0))
        if relation not in {"derivative", "corroborating", "unrelated"}:
            return None
        return {"relation": relation, "confidence": max(0.0, min(1.0, confidence)), "reason": clean(data.get("reason"))}
    except Exception:
        # Provenance detection must never break the collection/processing run.
        return None


def detect_derivative_relationships(
    entries: list[dict[str, Any]],
    *,
    llm_resolver: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None] | None = _default_llm_resolver,
    min_similarity: float = 0.38,
) -> dict[str, Any]:
    """Find likely same-event derivative coverage without collapsing independent evidence.

    This is deliberately conservative: exact duplicates remain the job of normal
    deduplication. Event-level grouping is only applied when candidate documents
    have strong semantic/event overlap and, when ambiguous, the local LLM agrees.
    """
    items = [x for x in entries if isinstance(x, dict)]
    relationships: list[dict[str, Any]] = []
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, left in enumerate(items):
        left_id = str(left.get("record_id") or left.get("file_path") or f"entry-{i}")
        parent.setdefault(left_id, left_id)
        for j in range(i + 1, len(items)):
            right = items[j]
            right_id = str(right.get("record_id") or right.get("file_path") or f"entry-{j}")
            parent.setdefault(right_id, right_id)
            similarity = _event_similarity(left, right)
            if not _looks_like_event_pair(left, right, similarity):
                continue
            # If two records are already in the same confirmed derivative group,
            # do not waste another LLM call on the transitive pair.
            if find(left_id) == find(right_id):
                continue

            result = llm_resolver(left, right) if llm_resolver else None
            if result is None:
                # Conservative fallback: only call something derivative when
                # there is very high semantic overlap and a close event date.
                distance = _date_distance_days(left, right)
                if similarity >= 0.82 and (distance is None or distance <= 3):
                    relation = "derivative"
                    confidence = similarity
                    reason = "high_semantic_same_event_fallback"
                else:
                    continue
            else:
                relation = result["relation"]
                confidence = result["confidence"]
                reason = result.get("reason") or "llm_event_provenance"

            if relation == "unrelated":
                continue
            relationships.append({
                "relation_type": "likely_derivative" if relation == "derivative" else "independent_corroboration",
                "confidence": round(confidence, 3),
                "similarity": round(similarity, 3),
                "reason": reason,
                "left_record_id": left_id,
                "right_record_id": right_id,
                "left_source": source_key(left),
                "right_source": source_key(right),
            })
            if relation == "derivative" and confidence >= 0.70:
                union(left_id, right_id)

    groups: dict[str, list[str]] = defaultdict(list)
    for item_id in parent:
        root = find(item_id)
        groups[root].append(item_id)

    derivative_groups = [sorted(ids) for ids in groups.values() if len(ids) > 1]
    group_by_id = {
        item_id: f"event-{idx + 1:04d}"
        for idx, ids in enumerate(derivative_groups)
        for item_id in ids
    }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "counts": {
            "candidate_relationships": len(relationships),
            "likely_derivative_relationships": sum(r["relation_type"] == "likely_derivative" for r in relationships),
            "independent_corroborations": sum(r["relation_type"] == "independent_corroboration" for r in relationships),
            "derivative_groups": len(derivative_groups),
        },
        "relationships": relationships,
        "derivative_groups": derivative_groups,
        "group_by_record_id": group_by_id,
    }
