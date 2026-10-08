from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from core.intelligence_quality import clean, corroboration_profile, evidence_text, problem_signature, source_key, tokenize
from core.semantic_retrieval import semantic_scores
from core.event_lineage import detect_derivative_relationships
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import urlparse

from core.normalization import build_normalized_collection_records, save_normalized_collection_records
from core.quality_metrics import generate_and_save_quality_metrics
from core.promotion import apply_promotion_overrides, build_promotion_index, build_promotion_summary, compute_promotion_decision
from core.recursive_collection import ensure_recursive_review_log
from core.schemas import ALLOWED_RESEARCH_AREAS, normalize_area
from core.source_registry import (
    SOURCE_REGISTRY_PATH,
    build_source_review_queue,
    export_source_health_summary,
    load_source_registry,
    save_source_discovery_candidates,
)


def _tokenize(text: str) -> set[str]:
    tokens = re.findall(r"[a-zA-Z0-9]+", (text or "").lower())
    return {token for token in tokens if len(token) > 2}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    overlap = len(left & right)
    union = len(left | right)
    return overlap / union if union else 0.0


def _parse_markdown_sections(path: Path) -> Dict[str, str]:
    text = path.read_text(encoding="utf-8")
    sections: Dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def _parse_bullet_map(section_text: str) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for raw_line in (section_text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        key, value = line[2:].split(":", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _first_source_url(source_value: str) -> str:
    lines = (source_value or "").splitlines()
    if not lines:
        return ""
    return lines[0].strip()


def extract_intermediate_index_entry(path: Path) -> Dict[str, Any]:
    sections = _parse_markdown_sections(path)
    shared_header = _parse_bullet_map(sections.get("Shared Metadata", ""))
    metadata = sections.get("Machine Metadata", "")
    provenance = _parse_bullet_map(sections.get("Provenance", ""))
    source_value = shared_header.get("source", "") or sections.get("Source", "")
    problem = sections.get("Problem Preview", "") or sections.get("Problem", "")
    title = shared_header.get("title") or path.stem.replace("-", " ").title()
    research_area = normalize_area(shared_header.get("research_area", "")) or "Unscoped"
    tokens = _tokenize(f"{title} {problem} {sections.get('Raw Content', '')}")
    return {
        "file_path": str(path.resolve()),
        "title": title,
        "layer": shared_header.get("layer", "Unknown"),
        "research_area": research_area,
        "source_url": _first_source_url(source_value),
        "created_at": shared_header.get("collected_at", provenance.get("collected_at", "")),
        "synthesis_status": "pending",
        "kind": "intermediate",
        "problem": problem,
        "metadata": metadata,
        "tokens": sorted(tokens),
    }


def extract_synthesis_index_entry(path: Path) -> Dict[str, Any]:
    sections = _parse_markdown_sections(path)
    title = path.stem.replace("-", " ").title()
    heading_line = path.read_text(encoding="utf-8").splitlines()[0].strip()
    if heading_line.startswith("# Synthesis: "):
        title = heading_line.replace("# Synthesis: ", "", 1).strip()
    source_inputs = [line[2:].strip() for line in sections.get("Source Inputs", "").splitlines() if line.startswith("- ")]
    provenance_lines = sections.get("Provenance", "").splitlines()
    source_urls: list[str] = []
    created_at = ""
    for line in provenance_lines:
        stripped = line.strip()
        if stripped.startswith("- synthesized_at:"):
            created_at = stripped.split(":", 1)[1].strip()
        if stripped.startswith("- source_urls:"):
            continue
        if stripped.startswith("- http") or stripped.startswith("- https"):
            source_urls.append(stripped[2:].strip())
        if stripped.startswith("- input_files:"):
            continue
        if stripped.startswith("- /home/"):
            continue
        if stripped.startswith("- notes:"):
            continue
        if stripped.startswith("- source_urls"):
            continue
    problem = sections.get("Problem", "")
    tokens = _tokenize(f"{title} {problem} {sections.get('Gap', '')} {sections.get('Idea', '')}")
    return {
        "file_path": str(path.resolve()),
        "title": title,
        "layer": ", ".join(line.strip() for line in sections.get("Layer or Layers", "").splitlines() if line.strip()) or sections.get("Layer or Layers", ""),
        "synthesis_scope": sections.get("Synthesis Scope", ""),
        "research_area": normalize_area(sections.get("Research Area", "")) or "Unscoped",
        "source_url": source_urls[0] if source_urls else "",
        "created_at": created_at,
        "synthesis_status": "synthesized",
        "kind": "synthesis",
        "problem": problem,
        "source_inputs": source_inputs,
        "actor": "",
        "tokens": sorted(tokens),
    }


def load_manifest_entries(manifest_dir: Path) -> list[Dict[str, Any]]:
    index_path = manifest_dir / "index.json"
    if not index_path.exists():
        return []
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in data if isinstance(item, dict)]


def load_source_registry_entries(path: Path = SOURCE_REGISTRY_PATH) -> list[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = data.get("entries", [])
    return [item for item in entries if isinstance(item, dict)]


def build_problem_fingerprint(entry: Dict[str, Any]) -> str:
    title_tokens = _tokenize(entry.get("title", ""))
    problem_tokens = _tokenize(entry.get("problem", ""))
    area = normalize_area(entry.get("research_area")) or "Unscoped"
    strongest = sorted(title_tokens | problem_tokens)[:8]
    return f"{area}::{'-'.join(strongest)}"


def _same_domain(left_url: str, right_url: str) -> bool:
    if not left_url or not right_url:
        return False
    return urlparse(left_url).netloc.lower() == urlparse(right_url).netloc.lower()


def _normalized_actor(entry: Dict[str, Any]) -> str:
    actor = str(entry.get("actor", "")).strip().lower()
    if actor and actor != "unknown":
        return actor
    return ""


def _make_relationship(
    relation_type: str,
    left: Dict[str, Any],
    right: Dict[str, Any],
    *,
    similarity: float,
    reason: str,
    fingerprint: str,
) -> Dict[str, Any]:
    return {
        "relation_type": relation_type,
        "reason": reason,
        "fingerprint": fingerprint,
        "similarity": round(similarity, 3),
        "left": {
            "file_path": left.get("file_path"),
            "title": left.get("title"),
            "layer": left.get("layer"),
            "research_area": left.get("research_area"),
            "source_url": left.get("source_url"),
            "actor": left.get("actor", ""),
            "kind": left.get("kind", ""),
        },
        "right": {
            "file_path": right.get("file_path"),
            "title": right.get("title"),
            "layer": right.get("layer"),
            "research_area": right.get("research_area"),
            "source_url": right.get("source_url"),
            "actor": right.get("actor", ""),
            "kind": right.get("kind", ""),
        },
    }


def detect_evidence_relationships(
    entries: Iterable[Dict[str, Any]],
    *,
    duplicate_threshold: float = 0.6,
    corroboration_threshold: float = 0.28,
) -> Dict[str, Any]:
    items = list(entries)
    exact_duplicates: list[Dict[str, Any]] = []
    soft_duplicates: list[Dict[str, Any]] = []
    corroborating_evidence: list[Dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for idx, left in enumerate(items):
        left_tokens = set(left.get("tokens", []))
        left_area = normalize_area(left.get("research_area"))
        left_actor = _normalized_actor(left)
        for right in items[idx + 1 :]:
            right_actor = _normalized_actor(right)
            same_source = bool(left.get("source_url")) and left.get("source_url") == right.get("source_url")
            same_title = str(left.get("title", "")).strip().lower() == str(right.get("title", "")).strip().lower()
            right_area = normalize_area(right.get("research_area"))
            similarity = _jaccard(left_tokens, set(right.get("tokens", [])))
            fingerprint = build_problem_fingerprint(left)
            key = tuple(sorted([str(left.get("file_path", "")), str(right.get("file_path", ""))]) + [fingerprint])
            if key in seen_keys:
                continue

            if same_source and same_title:
                exact_duplicates.append(
                    _make_relationship(
                        "exact_duplicate",
                        left,
                        right,
                        similarity=1.0,
                        reason="same_source_same_title",
                        fingerprint=fingerprint,
                    )
                )
                seen_keys.add(key)
                continue

            if left_area != right_area:
                # Cross-area matches are not duplicates, but exact source/title collisions
                # are already handled above.
                continue

            same_actor = bool(left_actor) and left_actor == right_actor
            same_domain = _same_domain(str(left.get("source_url", "")), str(right.get("source_url", "")))

            if similarity >= duplicate_threshold or (same_actor and same_domain and similarity >= 0.45):
                soft_duplicates.append(
                    _make_relationship(
                        "soft_duplicate",
                        left,
                        right,
                        similarity=similarity,
                        reason="high_content_similarity" if similarity >= duplicate_threshold else "same_actor_same_domain",
                        fingerprint=fingerprint,
                    )
                )
                seen_keys.add(key)
                continue

            if same_actor and left.get("layer") != right.get("layer") and (
                similarity >= corroboration_threshold or same_domain
            ):
                corroborating_evidence.append(
                    _make_relationship(
                        "corroborating_evidence",
                        left,
                        right,
                        similarity=similarity,
                        reason="same_actor_cross_layer",
                        fingerprint=fingerprint,
                    )
                )
                seen_keys.add(key)
                continue

            if same_domain and left.get("layer") != right.get("layer") and similarity >= corroboration_threshold:
                corroborating_evidence.append(
                    _make_relationship(
                        "corroborating_evidence",
                        left,
                        right,
                        similarity=similarity,
                        reason="same_domain_cross_layer_similarity",
                        fingerprint=fingerprint,
                    )
                )
                seen_keys.add(key)

    lineage = detect_derivative_relationships(items)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "counts": {
            "exact_duplicates": len(exact_duplicates),
            "soft_duplicates": len(soft_duplicates),
            "corroborating_evidence": len(corroborating_evidence),
            "likely_derivative_relationships": lineage["counts"]["likely_derivative_relationships"],
            "independent_corroborations": lineage["counts"]["independent_corroborations"],
            "derivative_groups": lineage["counts"]["derivative_groups"],
        },
        "exact_duplicates": exact_duplicates,
        "soft_duplicates": soft_duplicates,
        "corroborating_evidence": corroborating_evidence,
        "event_lineage": lineage,
    }


def _embedding_similarity(left: Dict[str, Any], right: Dict[str, Any]) -> float:
    """Semantic similarity for clustering; falls back safely through semantic_retrieval."""
    left_text = clean(evidence_text(left))
    right_text = clean(evidence_text(right))
    if not left_text or not right_text:
        return 0.0
    try:
        return float(semantic_scores(left_text, [right_text])[0])
    except Exception:
        return 0.0


from core.semantic_retrieval import compute_cluster_score
def _cluster_similarity(left: Dict[str, Any], right: Dict[str, Any], threshold: float) -> float:
    """Hybrid cluster score: embeddings are primary, lexical/facet signals are supporting."""
    if normalize_area(left.get("research_area")) != normalize_area(right.get("research_area")):
        return 0.0
    text1 = clean(evidence_text(left))
    text2 = clean(evidence_text(right))
    hash1 = left.get("content_hash", "")
    hash2 = right.get("content_hash", "")
    type1 = left.get("problem_type", "")
    type2 = right.get("problem_type", "")
    domain1 = left.get("layer", "")
    domain2 = right.get("layer", "")
    return compute_cluster_score(text1, hash1, type1, domain1, text2, hash2, type2, domain2)


def _cluster_llm_resolver(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any] | None:
    """Use the local LLM only for genuinely ambiguous cluster decisions."""
    try:
        from core.llm_provider import generate
    except Exception:
        return None
    prompt = f"""You are the final ambiguity resolver for a research-intelligence clustering system.
Decide whether DOCUMENT B describes the SAME UNDERLYING RESEARCH PROBLEM as DOCUMENT A.
Do not cluster merely because the domain, technology, or keywords are similar.
Cluster when the underlying problem, limitation, failure mode, or research question is substantially the same.
Keep separate when the underlying problem is materially different.
Return JSON only with keys: same_cluster, confidence, reason.
same_cluster must be true or false.

DOCUMENT A:
Area: {clean(left.get('research_area'))}
Title: {clean(left.get('title'))}
Problem: {clean(left.get('problem_statement') or left.get('problem'))}
Context: {clean(left.get('context_summary'))}
Tags: {clean(left.get('topic_tags'))} {clean(left.get('signal_type_tags'))}

DOCUMENT B:
Area: {clean(right.get('research_area'))}
Title: {clean(right.get('title'))}
Problem: {clean(right.get('problem_statement') or right.get('problem'))}
Context: {clean(right.get('context_summary'))}
Tags: {clean(right.get('topic_tags'))} {clean(right.get('signal_type_tags'))}
"""
    try:
        raw = generate(prompt)
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            return None
        data = json.loads(match.group(0))
        value = data.get("same_cluster")
        if isinstance(value, str):
            value = value.strip().lower() in {"true", "yes", "1"}
        if not isinstance(value, bool):
            return None
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
        return {"same_cluster": value, "confidence": confidence, "reason": clean(data.get("reason"))}
    except Exception:
        return None


def _decide_cluster_pair(
    left: Dict[str, Any],
    right: Dict[str, Any],
    *,
    threshold: float,
    llm_resolver: Any = _cluster_llm_resolver,
) -> Dict[str, Any]:
    score = _cluster_similarity(left, right, threshold)
    # Clear cases are deterministic. Only the middle band is sent to the LLM.
    high = max(0.72, threshold + 0.22)
    low = min(0.22, max(0.0, threshold - 0.13))
    if score >= high:
        return {"same_cluster": True, "score": score, "decision": "deterministic_accept", "llm_used": False}
    if score < low:
        return {"same_cluster": False, "score": score, "decision": "deterministic_reject", "llm_used": False}
    result = llm_resolver(left, right) if llm_resolver else None
    if result is None:
        # Offline/LLM-unavailable fallback: preserve the configured deterministic
        # threshold so existing RIF processing remains operational. This path is
        # explicitly marked as a fallback; on the real RIF machine ambiguous pairs
        # are resolved by the local LLM.
        return {
            "same_cluster": score >= threshold,
            "score": score,
            "decision": "ambiguous_deterministic_fallback",
            "llm_used": False,
        }
    return {
        "same_cluster": bool(result.get("same_cluster")) and float(result.get("confidence", 0)) >= 0.70,
        "score": score,
        "decision": "llm_accept" if result.get("same_cluster") else "llm_reject",
        "llm_used": True,
        "llm_confidence": float(result.get("confidence", 0)),
        "llm_reason": result.get("reason", ""),
    }


def _exact_record_fingerprint(record: Dict[str, Any]) -> str:
    """Fingerprint identical collected evidence without treating every repeat URL as a duplicate."""
    source = source_key(record)
    title = clean(record.get("title")).lower()
    evidence = re.sub(r"\s+", " ", evidence_text(record).lower()).strip()
    payload = f"{normalize_area(record.get('research_area')) or 'Unscoped'}|{clean(record.get('layer')).lower()}|{source}|{title}|{evidence}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cluster_signal_type(member: Dict[str, Any]) -> str:
    layer = str(member.get("layer", "")).strip().lower()
    if layer == "funding":
        return "funding_signal"
    if layer == "regulation":
        return "compliance_requirement"
    if layer == "failure":
        return "failure_mode"
    if layer == "hackathon":
        return "prototype_challenge"
    if layer == "company":
        return "market_need"
    if layer == "practitioner":
        return "practitioner_pain_point"
    if layer == "opensource":
        return "engineering_gap"
    if layer == "dataavailability":
        return "evaluation_support"
    if layer == "lab":
        return "research_activity"
    if layer == "literature":
        return "research_problem"
    if layer == "investment":
        return "capital_signal"
    return "general_signal"


def _cluster_problem_facets(members: list[Dict[str, Any]]) -> dict[str, list[str]]:
    signal_types = sorted({_cluster_signal_type(member) for member in members})
    evidence_types = sorted(
        {
            str(member.get("evidence_type", "")).strip()
            for member in members
            if str(member.get("evidence_type", "")).strip() and str(member.get("evidence_type", "")).strip().lower() != "unknown"
        }
    )
    actors = sorted(
        {
            str(member.get("actor", "")).strip()
            for member in members
            if str(member.get("actor", "")).strip() and str(member.get("actor", "")).strip().lower() != "unknown"
        }
    )
    return {
        "signal_types": signal_types,
        "evidence_types": evidence_types,
        "actors": actors,
    }


def build_problem_clusters(
    entries: Iterable[Dict[str, Any]],
    threshold: float = 0.35,
    *,
    llm_resolver: Any = _cluster_llm_resolver,
) -> list[Dict[str, Any]]:
    # Remove byte-for-byte/content-equivalent collection repeats before clustering.
    # This prevents repeated scheduled runs from inflating member counts.
    unique_items: list[Dict[str, Any]] = []
    seen_fingerprints: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        fingerprint = _exact_record_fingerprint(item)
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        unique_items.append(dict(item))

    items = unique_items
    # Event-level provenance is applied before clustering so derivative coverage
    # can never inflate evidence strength. The resolver is conservative and
    # falls back safely when the local LLM is unavailable.
    if items and not any(clean(item.get("event_lineage_group")) for item in items):
        # Keep normal pipeline clustering deterministic. When an LLM resolver is
        # explicitly supplied, adapt its clustering-shaped result to the
        # provenance resolver contract; otherwise provenance detection remains
        # fully deterministic and performs no hidden LLM calls.
        provenance_resolver = None
        if llm_resolver is not None:
            def provenance_resolver(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any] | None:
                result = llm_resolver(left, right)
                if not isinstance(result, dict):
                    return None
                if "relation" in result:
                    return result
                if "same_cluster" in result:
                    same = bool(result.get("same_cluster"))
                    confidence = max(0.0, min(1.0, float(result.get("confidence", 0.0) or 0.0)))
                    return {
                        "relation": "derivative" if same else "unrelated",
                        "confidence": confidence,
                        "reason": clean(result.get("reason", "")),
                    }
                return None
        lineage = detect_derivative_relationships(items, llm_resolver=provenance_resolver)
        lineage_map = lineage.get("group_by_record_id", {})
        for item in items:
            item_id = str(item.get("record_id") or item.get("file_path") or "")
            if item_id in lineage_map:
                item["event_lineage_group"] = lineage_map[item_id]

    clusters: list[Dict[str, Any]] = []
    visited: set[int] = set()
    for idx, entry in enumerate(items):
        if idx in visited:
            continue
        queue = [idx]
        member_indices: set[int] = set()
        anchor_idx = idx
        while queue:
            current = queue.pop()
            if current in member_indices:
                continue
            member_indices.add(current)
            current_entry = items[current]
            for other_idx, other in enumerate(items):
                if other_idx in member_indices or other_idx in visited:
                    continue
                if normalize_area(other.get("research_area")) != normalize_area(current_entry.get("research_area")):
                    continue
                # Prevent transitive chaining: a candidate must match the cluster
                # anchor, not merely one loosely related member.
                anchor = items[anchor_idx]
                same_source = bool(source_key(anchor)) and source_key(anchor) == source_key(other)
                same_title = clean(anchor.get("title")).lower() == clean(other.get("title")).lower()
                decision = _decide_cluster_pair(
                    anchor, other, threshold=threshold, llm_resolver=llm_resolver
                )
                similarity = decision["score"]
                if same_source and same_title and similarity < threshold * 1.5:
                    continue
                if decision["same_cluster"]:
                    queue.append(other_idx)
        visited.update(member_indices)
        members = [items[item_idx] for item_idx in sorted(member_indices)]
        token_counter = Counter(token for member in members for token in tokenize(evidence_text(member)))
        top_terms = [token for token, _ in token_counter.most_common(10) if len(token) > 3][:8]
        facets = _cluster_problem_facets(members)
        representative = max(members, key=lambda member: len(clean(member.get("problem_statement") or member.get("problem") or member.get("title"))))
        representative_problem = clean(representative.get("problem_statement") or representative.get("problem") or representative.get("title"))
        supporting_layers = sorted({clean(member.get("layer")) for member in members if clean(member.get("layer"))})
        evidence_links = sorted({clean(member.get("file_path")) for member in members if clean(member.get("file_path"))})
        profile = corroboration_profile(members)
        
        # Aggregate funding calls
        cluster_funding_calls = set()
        for member in members:
            call_id = member.get("funding_call_id")
            if call_id:
                cluster_funding_calls.add(call_id)
            for c_id in member.get("funding_call_ids", []):
                if c_id:
                    cluster_funding_calls.add(c_id)
                    
        clusters.append({
            "cluster_id": f"{normalize_area(members[0].get('research_area')) or 'unscoped'}-{idx + 1}",
            "funding_call_id": members[0].get("funding_call_id"),
            "funding_call_ids": sorted(list(cluster_funding_calls)),
            "area": normalize_area(members[0].get("research_area")) or "Unscoped",
            "representative_problem": representative_problem,
            "problem_signature": problem_signature(representative),
            "supporting_layers": supporting_layers,
            "signal_types": facets["signal_types"],
            "evidence_types": facets["evidence_types"],
            "actors": facets["actors"],
            "independent_source_count": profile["independent_sources"],
            "independent_evidence_unit_count": profile["independent_evidence_units"],
            "independent_layer_count": profile["independent_layers"],
            "research_evidence_layer_count": profile["research_evidence_layers"],
            "independence_ratio": profile["independence_ratio"],
            "members": [
                {
                    "record_id": member.get("record_id"),
                    "funding_call_id": member.get("funding_call_id"),
                    "funding_call_ids": member.get("funding_call_ids", []),
                    "title": member.get("title"),
                    "file_path": member.get("file_path"),
                    "layer": member.get("layer"),
                    "actor": member.get("actor", ""),
                    "source_url": member.get("source_url", ""),
                    "evidence_type": member.get("evidence_type", ""),
                    "problem_statement": member.get("problem_statement", "") or member.get("problem", ""),
                    "source_key": source_key(member),
                    "event_lineage_group": member.get("event_lineage_group", ""),
                }
                for member in members
            ],
            "top_terms": top_terms,
            "evidence_links": evidence_links,
            "deduped_member_count": len(members),
            "clustering_method": "embedding_hybrid_deterministic" if llm_resolver is None else "embedding_hybrid_with_llm_ambiguity_resolution",
        })
    return clusters


def compute_confidence(entry: Dict[str, Any]) -> Dict[str, Any]:
    score = 0
    breakdown: Dict[str, int] = {}

    layers = entry.get("layers_covered") or []
    normalized_layers = {str(layer).strip().lower() for layer in layers} if isinstance(layers, list) else set()
    layer_count = len(normalized_layers) if normalized_layers else 1
    base_layer_support = min(layer_count * 20, 40)
    # Commercial/emerging-demand signals must not masquerade as research evidence.
    research_layers = normalized_layers - {"investment", "hackathon"}
    if normalized_layers and not research_layers:
        base_layer_support = min(base_layer_support, 10)
    elif normalized_layers and "investment" in normalized_layers and len(research_layers) == 1:
        base_layer_support = min(base_layer_support, 30)
    breakdown["layer_support"] = base_layer_support
    score += breakdown["layer_support"]

    input_files = entry.get("input_files") or []
    input_count = len(input_files) if isinstance(input_files, list) else 0
    breakdown["input_support"] = min(input_count * 10, 20)
    score += breakdown["input_support"]

    source_urls = entry.get("source_urls") or []
    url_count = len(source_urls) if isinstance(source_urls, list) else 0
    breakdown["source_support"] = min(url_count * 10, 20)
    score += breakdown["source_support"]

    method = str(entry.get("synthesis_method", "")).strip().lower()
    method_points = {"manual_chat": 15, "local_llm": 10, "fallback": 5}.get(method, 0)
    breakdown["method_quality"] = method_points
    score += method_points

    synthesis_date = str(entry.get("synthesis_date", "")).strip()
    recency_points = _score_recency(synthesis_date)
    breakdown["recency"] = recency_points
    score += recency_points

    label = "Low"
    if score >= 70:
        label = "High"
    elif score >= 45:
        label = "Medium"

    if normalized_layers and not research_layers:
        score = min(score, 40)
        label = "Low"
        breakdown["evidence_cap"] = 40
    elif "investment" in normalized_layers and len(research_layers) <= 1:
        score = min(score, 70)
        if score >= 70:
            label = "Medium"
        breakdown["evidence_cap"] = 70

    return {"score": score, "label": label, "breakdown": breakdown}


def _score_recency(synthesis_date: str) -> int:
    recency_points = 0
    if synthesis_date:
        try:
            parsed = datetime.fromisoformat(synthesis_date.replace("Z", "+00:00"))
            age_days = max((datetime.now(UTC) - parsed.astimezone(UTC)).days, 0)
            if age_days <= 30:
                recency_points = 10
            elif age_days <= 180:
                recency_points = 7
            elif age_days <= 365:
                recency_points = 4
        except ValueError:
            recency_points = 0
    return recency_points


def _average_source_trust(entry: Dict[str, Any], registry_entries: Iterable[Dict[str, Any]]) -> float:
    source_urls = {str(item).strip() for item in entry.get("source_urls", []) if str(item).strip()}
    if not source_urls:
        return 0.6
    matches = [float(item.get("trust_score", 0.7) or 0.7) for item in registry_entries if str(item.get("url", "")).strip() in source_urls]
    if not matches:
        return 0.6
    return sum(matches) / len(matches)


def _specificity_score(sections: Dict[str, str], entry: Dict[str, Any]) -> int:
    score = 0
    problem = sections.get("Problem", "")
    gap = sections.get("Gap", "")
    idea = sections.get("Idea", "")
    tags = sections.get("Tags", "")
    evidence_input_count = int(entry.get("evidence_input_count", 0) or 0)

    if len(problem.split()) >= 20:
        score += 7
    elif len(problem.split()) >= 10:
        score += 4

    if len(gap.split()) >= 20:
        score += 5
    elif len(gap.split()) >= 10:
        score += 3

    if len(idea.split()) >= 20:
        score += 5
    elif len(idea.split()) >= 10:
        score += 3

    tag_count = len([item for item in tags.split() if item.startswith("#")])
    if tag_count >= 5:
        score += 2
    elif tag_count >= 3:
        score += 1

    score += min(evidence_input_count, 4)
    return min(score, 20)


def _feasibility_score(feasibility_text: str) -> int:
    lowered = feasibility_text.strip().lower()
    if not lowered:
        return 5
    if "high" in lowered and "moderate" in lowered:
        return 12
    if "high" in lowered:
        return 15
    if "moderate" in lowered:
        return 10
    if "low" in lowered:
        return 4
    return 7


def _novelty_score(entry: Dict[str, Any], all_entries: Iterable[Dict[str, Any]], current_sections: Dict[str, str]) -> int:
    current_area = normalize_area(entry.get("area"))
    current_text = " ".join(
        [
            str(entry.get("title", "")),
            current_sections.get("Problem", ""),
            current_sections.get("Gap", ""),
            current_sections.get("Idea", ""),
        ]
    )
    current_tokens = _tokenize(current_text)
    max_similarity = 0.0
    for other in all_entries:
        if other is entry:
            continue
        if normalize_area(other.get("area")) != current_area:
            continue
        other_path = Path(str(other.get("synthesized_file", "")).strip())
        if not other_path.exists():
            continue
        other_sections = _parse_markdown_sections(other_path)
        other_text = " ".join(
            [
                str(other.get("title", "")),
                other_sections.get("Problem", ""),
                other_sections.get("Gap", ""),
                other_sections.get("Idea", ""),
            ]
        )
        similarity = _jaccard(current_tokens, _tokenize(other_text))
        if similarity > max_similarity:
            max_similarity = similarity

    if max_similarity <= 0.1:
        return 15
    if max_similarity <= 0.2:
        return 12
    if max_similarity <= 0.35:
        return 9
    if max_similarity <= 0.5:
        return 6
    return 3


def compute_problem_ranking(
    entry: Dict[str, Any],
    *,
    registry_entries: Iterable[Dict[str, Any]],
    all_entries: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    synthesized_file = Path(str(entry.get("synthesized_file", "")).strip())
    sections = _parse_markdown_sections(synthesized_file) if synthesized_file.exists() else {}
    priority_readiness = entry.get("priority_readiness", {}) if isinstance(entry.get("priority_readiness"), dict) else {}

    layers = entry.get("layers_covered") or []
    layer_count = len(set(layers)) if isinstance(layers, list) else 1
    layer_support = min(layer_count * 20, 20)

    source_trust = round(_average_source_trust(entry, registry_entries) * 20)
    specificity = _specificity_score(sections, entry)
    recency = _score_recency(str(entry.get("synthesis_date", "")).strip())
    feasibility = _feasibility_score(sections.get("Feasibility", ""))
    novelty = _novelty_score(entry, all_entries, sections)

    score = layer_support + source_trust + specificity + recency + feasibility + novelty
    label = "Low"
    if score >= 75:
        label = "High"
    elif score >= 50:
        label = "Medium"

    ready_for_high_priority = bool(priority_readiness.get("ready_for_high_priority", False))
    if not priority_readiness:
        source_urls = entry.get("source_urls", []) or []
        evidence_input_count = int(entry.get("evidence_input_count", 0) or 0)
        ready_for_high_priority = layer_count >= 2 and len(source_urls) >= 2 and evidence_input_count >= 3
        priority_readiness = {
            "ready_for_high_priority": ready_for_high_priority,
            "layer_count": layer_count,
            "source_count": len(source_urls),
            "evidence_input_count": evidence_input_count,
            "rationale": "Inferred during ranking because explicit synthesis readiness metadata was not present.",
        }

    priority_band = "low-priority"
    if label == "High" and ready_for_high_priority:
        priority_band = "high-priority"
    elif label in {"High", "Medium"}:
        priority_band = "evidence-pending" if not ready_for_high_priority else "medium-priority"

    return {
        "score": score,
        "label": label,
        "priority_band": priority_band,
        "ready_for_high_priority": ready_for_high_priority,
        "breakdown": {
            "layer_support": layer_support,
            "source_trust": source_trust,
            "specificity": specificity,
            "recency": recency,
            "prototype_feasibility": feasibility,
            "novelty": novelty,
        },
        "priority_readiness": priority_readiness,
    }


def build_problem_ranking_index(entries: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    ranked = [entry for entry in entries if isinstance(entry, dict)]
    ranked.sort(
        key=lambda item: (
            -int(item.get("ranking", {}).get("score", 0)),
            str(item.get("area", "")),
            str(item.get("title", "")),
        )
    )
    return ranked


def build_structured_output_index(outputs_root: Path) -> list[Dict[str, Any]]:
    entries: list[Dict[str, Any]] = []
    intermediate_root = outputs_root / "intermediate"
    if intermediate_root.exists():
        for path in sorted(intermediate_root.rglob("*.md")):
            entries.append(extract_intermediate_index_entry(path))
    synthesis_root = outputs_root / "synthesis"
    if synthesis_root.exists():
        for path in sorted(synthesis_root.rglob("*.md")):
            if "manifest" in path.parts:
                continue
            entries.append(extract_synthesis_index_entry(path))
    entries.sort(key=lambda item: (item.get("kind", ""), item.get("research_area", ""), item.get("title", "")))
    return entries


def build_batch_workflow_status(outputs_root: Path, structured_index: Iterable[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    entries = list(structured_index or build_structured_output_index(outputs_root))
    intermediate = [entry for entry in entries if entry.get("kind") == "intermediate"]
    synthesis = [entry for entry in entries if entry.get("kind") == "synthesis"]

    referenced_inputs: set[str] = set()
    manifest_index = outputs_root / "synthesis" / "manifest" / "index.json"
    if manifest_index.exists():
        try:
            manifest_entries = json.loads(manifest_index.read_text(encoding="utf-8"))
            for item in manifest_entries:
                if isinstance(item, dict):
                    for file_path in item.get("input_files", []):
                        referenced_inputs.add(str(file_path))
        except (OSError, json.JSONDecodeError):
            referenced_inputs = set()

    pending_synthesis = [entry["file_path"] for entry in intermediate if entry.get("file_path") not in referenced_inputs]

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "counts": {
            "intermediate": len(intermediate),
            "synthesis": len(synthesis),
        },
        "pending_synthesis": pending_synthesis,
        "areas_present": sorted(
            {
                normalize_area(entry.get("research_area")) or "Unscoped"
                for entry in entries
                if normalize_area(entry.get("research_area")) in ALLOWED_RESEARCH_AREAS or entry.get("research_area") == "Unscoped"
            }
        ),
    }


def build_operational_views(outputs_root: Path, structured_index: Iterable[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    entries = list(structured_index or build_structured_output_index(outputs_root))
    workflow_root = outputs_root / "workflow"
    refresh_plan_path = workflow_root / "source_refresh_plan.json"
    discovery_path = workflow_root / "source_discovery_candidates.json"
    review_queue_path = workflow_root / "source_review_queue.json"
    recursive_review_path = workflow_root / "recursive_expansion_review.json"
    source_health_summary_path = workflow_root / "source_health_summary.json"
    promotion_path = outputs_root / "synthesis" / "manifest" / "promotion.json"
    status = build_batch_workflow_status(outputs_root, entries)

    registry_entries = load_source_registry(SOURCE_REGISTRY_PATH)
    status_counts: Dict[str, int] = {}
    stale_source_count = 0
    changed_source_count = 0
    never_checked_count = 0
    due_for_refresh_count = 0
    for entry in registry_entries:
        source_status = str(entry.get("status", "active")).strip().lower() or "active"
        status_counts[source_status] = status_counts.get(source_status, 0) + 1
        if source_status == "stale":
            stale_source_count += 1
        last_checked = str(entry.get("last_checked", "")).strip()
        last_changed = str(entry.get("last_changed", "")).strip()
        if not last_checked:
            never_checked_count += 1
        if last_changed and (not last_checked or last_changed > last_checked):
            changed_source_count += 1

    if refresh_plan_path.exists():
        try:
            refresh_payload = json.loads(refresh_plan_path.read_text(encoding="utf-8"))
            due_for_refresh_count = int(refresh_payload.get("stats", {}).get("due_entries", 0) or 0)
        except (OSError, json.JSONDecodeError):
            due_for_refresh_count = 0

    pending_recursive_expansions = 0
    candidate_source_count = 0
    pending_review_count = 0
    recursive_review_counts: Dict[str, int] = {}
    source_health_counts: Dict[str, int] = {}
    promotion_counts: Dict[str, int] = {}
    if discovery_path.exists():
        try:
            discovery_payload = json.loads(discovery_path.read_text(encoding="utf-8"))
            discovery_entries = [item for item in discovery_payload.get("entries", []) if isinstance(item, dict)]
            candidate_source_count = len(discovery_entries)
            pending_recursive_expansions = sum(
                1 for item in discovery_entries if str(item.get("discovery_method", "")).strip() == "linked_evidence_candidate"
            )
        except (OSError, json.JSONDecodeError):
            pending_recursive_expansions = 0
            candidate_source_count = 0
    if review_queue_path.exists():
        try:
            review_payload = json.loads(review_queue_path.read_text(encoding="utf-8"))
            pending_review_count = int(review_payload.get("stats", {}).get("pending_entries", 0) or 0)
        except (OSError, json.JSONDecodeError):
            pending_review_count = 0
    if recursive_review_path.exists():
        try:
            recursive_payload = json.loads(recursive_review_path.read_text(encoding="utf-8"))
            recursive_review_counts = dict(recursive_payload.get("stats", {}))
        except (OSError, json.JSONDecodeError):
            recursive_review_counts = {}
    if source_health_summary_path.exists():
        try:
            health_payload = json.loads(source_health_summary_path.read_text(encoding="utf-8"))
            source_health_counts = dict(health_payload.get("counts", {}))
        except (OSError, json.JSONDecodeError):
            source_health_counts = {}
    if promotion_path.exists():
        try:
            promotion_payload = json.loads(promotion_path.read_text(encoding="utf-8"))
            promotion_counts = dict(promotion_payload.get("counts", {}))
        except (OSError, json.JSONDecodeError):
            promotion_counts = {}

    failed_source_fetches: list[Dict[str, Any]] = []
    failure_terms = ("fetch", "http", "429", "requests", "beautifulsoup", "arxiv", "unable to reach")
    for report_path in sorted(workflow_root.glob("*report*.json")):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for task in report.get("tasks", []):
            if not isinstance(task, dict):
                continue
            messages = [str(item) for item in task.get("errors", [])] + [str(item) for item in task.get("warnings", [])]
            joined = " ".join(messages).lower()
            if not any(term in joined for term in failure_terms):
                continue
            failed_source_fetches.append(
                {
                    "report": str(report_path.resolve()),
                    "task_id": task.get("task_id", ""),
                    "agent_name": task.get("agent_name", ""),
                    "area": task.get("area", ""),
                    "messages": messages,
                }
            )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_freshness": {
            "total_sources": len(registry_entries),
            "status_counts": status_counts,
            "never_checked_count": never_checked_count,
            "due_for_refresh_count": due_for_refresh_count,
            "stale_source_count": stale_source_count,
            "changed_source_count": changed_source_count,
        },
        "queues": {
            "pending_synthesis_count": len(status.get("pending_synthesis", [])),
            "pending_synthesis_queue": status.get("pending_synthesis", []),
            "pending_recursive_expansions": pending_recursive_expansions,
            "candidate_source_count": candidate_source_count,
            "pending_review_count": pending_review_count,
        },
        "recursive_expansion_review": recursive_review_counts,
        "source_health_summary": source_health_counts,
        "promotion": promotion_counts,
        "failures": {
            "failed_source_fetch_count": len(failed_source_fetches),
            "failed_source_fetches": failed_source_fetches[:25],
        },
    }


def refresh_intelligence_views(outputs_root: Path) -> Dict[str, Path]:
    outputs_root.mkdir(parents=True, exist_ok=True)
    normalized_outputs = save_normalized_collection_records(outputs_root, outputs_root)
    normalized_records = build_normalized_collection_records(outputs_root)
    source_discovery = save_source_discovery_candidates(normalized_records)
    source_review_queue = build_source_review_queue()
    recursive_review_path = Path(ensure_recursive_review_log())
    source_health_summary = export_source_health_summary()
    structured_index = build_structured_output_index(outputs_root)
    index_path = outputs_root / "index.json"
    index_path.write_text(json.dumps(structured_index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    manifest_dir = outputs_root / "synthesis" / "manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_entries = load_manifest_entries(manifest_dir)
    registry_entries = load_source_registry_entries()
    for entry in manifest_entries:
        entry["confidence"] = compute_confidence(entry)
    for entry in manifest_entries:
        entry["ranking"] = compute_problem_ranking(entry, registry_entries=registry_entries, all_entries=manifest_entries)
    for entry in manifest_entries:
        synthesized_file = Path(str(entry.get("synthesized_file", "")).strip())
        sections = _parse_markdown_sections(synthesized_file) if synthesized_file.exists() else {}
        entry["promotion"] = compute_promotion_decision(entry, sections)
    manifest_entries = apply_promotion_overrides(manifest_entries)
    if manifest_entries:
        (manifest_dir / "index.json").write_text(json.dumps(manifest_entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    ranking_index = build_problem_ranking_index(manifest_entries)
    ranking_path = manifest_dir / "ranking.json"
    ranking_path.write_text(json.dumps(ranking_index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    promotion_index = build_promotion_index(manifest_entries)
    promotion_summary = build_promotion_summary(promotion_index)
    promotion_summary["generated_at"] = datetime.now(UTC).isoformat()
    promotion_path = manifest_dir / "promotion.json"
    promotion_path.write_text(json.dumps(promotion_summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    synthesis_entries = [entry for entry in structured_index if entry.get("kind") == "synthesis"]
    dedup_inputs: list[dict[str, Any]] = [
        {
            "file_path": record.get("file_path"),
            "title": record.get("title"),
            "layer": record.get("layer"),
            "research_area": record.get("research_area"),
            "source_url": record.get("source_url"),
            "actor": record.get("actor"),
            "source_type": record.get("source_type"),
            "evidence_type": record.get("evidence_type"),
            "focus": record.get("focus"),
            "problem": record.get("problem_statement"),
            "problem_statement": record.get("problem_statement"),
            "tokens": record.get("tokens", []),
            "kind": "normalized_intermediate",
        }
        for record in normalized_records
    ] + synthesis_entries

    dedup_report = detect_evidence_relationships(dedup_inputs)
    dedup_path = outputs_root / "synthesis" / "manifest" / "dedup_report.json"
    dedup_path.parent.mkdir(parents=True, exist_ok=True)
    dedup_path.write_text(json.dumps(dedup_report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lineage_map = dedup_report.get("event_lineage", {}).get("group_by_record_id", {})
    for item in dedup_inputs:
        item_id = str(item.get("record_id") or item.get("file_path") or "")
        if item_id in lineage_map:
            item["event_lineage_group"] = lineage_map[item_id]

    clusters = build_problem_clusters(dedup_inputs)
    cluster_path = outputs_root / "synthesis" / "clusters" / "index.json"
    cluster_path.parent.mkdir(parents=True, exist_ok=True)
    cluster_path.write_text(json.dumps(clusters, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    workflow_status = build_batch_workflow_status(outputs_root, structured_index)
    workflow_path = outputs_root / "workflow" / "status.json"
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(json.dumps(workflow_status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    operational_views = build_operational_views(outputs_root, structured_index)
    operational_views_path = outputs_root / "workflow" / "operational_views.json"
    operational_views_path.parent.mkdir(parents=True, exist_ok=True)
    operational_views_path.write_text(json.dumps(operational_views, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    quality_metrics_path = generate_and_save_quality_metrics(outputs_root)

    return {
        "structured_index": index_path,
        "normalized_records": normalized_outputs["records"],
        "normalized_by_area": normalized_outputs["by_area"],
        "normalized_by_layer": normalized_outputs["by_layer"],
        "source_discovery_candidates": Path(source_discovery.discovery_path),
        "source_review_queue": Path(source_review_queue.review_queue_path),
        "recursive_expansion_review": recursive_review_path,
        "source_health_summary": Path(source_health_summary.summary_path),
        "manifest_index": manifest_dir / "index.json",
        "ranking_index": ranking_path,
        "promotion_index": promotion_path,
        "dedup_report": dedup_path,
        "cluster_index": cluster_path,
        "workflow_status": workflow_path,
        "operational_views": operational_views_path,
        "quality_metrics": quality_metrics_path,
    }


def detect_contradictions(entries: list[dict]) -> dict:
    supporting = []
    contradicting = []
    qualifying = []
    unknown = []
    
    polarity_map = {
        "increase": 1, "increases": 1, "decrease": -1, "decreases": -1,
        "successfully": 1, "cannot": -1, "fails": -1, "works": 1,
        "available": 1, "unavailable": -1,
        "scales": 1, "bottleneck": -1,
        "supports": 1, "lacks": -1,
        "sustains": 1, "insufficient": -1,
        "stable": -1, "exceeds": 1, "below": -1
    }
    
    qualifiers = {"only", "limited", "partially", "sometimes", "depends", "constrained"}
    stopwords = {"the", "a", "an", "is", "are", "was", "were", "and", "or", "but", "with", "for", "to", "in", "of", "on", "system", "platform", "company", "new", "round", "cannot"}
    
    if not entries:
        return {"supporting_evidence": [], "contradicting_evidence": [], "qualifying_evidence": [], "unknown_evidence": []}

    def get_polarity(text):
        words = set(re.findall(r'\b\w+\b', text.lower()))
        score = sum(val for w, val in polarity_map.items() if w in words)
        if score > 0: return 1
        if score < 0: return -1
        return 0
        
    def extract_numbers(text):
        import re
        return [float(n) for n in re.findall(r'\b\d+(?:\.\d+)?\b', text)]

    reference_text = entries[0].get("problem_statement", "").lower()
    ref_words = set(re.findall(r'\b\w+\b', reference_text))
    ref_entities = ref_words - stopwords
    ref_nums = extract_numbers(reference_text)
    ref_pol = get_polarity(reference_text)
            
    supporting.append({
        "text": entries[0].get("problem_statement", ""),
        "provenance": "EVIDENCE",
        "source_url": entries[0].get("source_url", "")
    })
    
    for i in range(1, len(entries)):
        entry = entries[i]
        text = entry.get("problem_statement", "").lower()
        source_url = entry.get("source_url", "")
        
        words = set(re.findall(r'\b\w+\b', text))
        entities = words - stopwords
        nums = extract_numbers(text)
        
        claim_obj = {
            "text": entry.get("problem_statement", ""),
            "provenance": "EVIDENCE",
            "source_url": source_url
        }
        
        overlap = len(ref_entities.intersection(entities))
        if overlap == 0:
            unknown.append(claim_obj)
            continue
            
        pol = get_polarity(text)
        has_qualifier = any(q in words for q in qualifiers)
        
        num_contradiction = False
        if ref_nums and nums:
            # Simple heuristic: if numbers are entirely different and polarities differ, it's a contradiction.
            # e.g., "remains below 100" vs "exceeds 200"
            if len(set(ref_nums).intersection(set(nums))) == 0:
                if ref_pol != 0 and pol != 0 and ref_pol != pol:
                    num_contradiction = True

        if has_qualifier:
            qualifying.append(claim_obj)
        elif num_contradiction or (pol != 0 and ref_pol != 0 and pol != ref_pol):
            contradicting.append(claim_obj)
        else:
            supporting.append(claim_obj)
            
    return {
        "supporting_evidence": supporting,
        "contradicting_evidence": contradicting,
        "qualifying_evidence": qualifying,
        "unknown_evidence": unknown
    }


