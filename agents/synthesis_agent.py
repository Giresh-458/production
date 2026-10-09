from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.intelligence import build_problem_clusters
from core.normalization import ensure_normalized_collection_records, save_normalized_collection_records
from core.schemas import create_error_response, create_partial_response, create_success_response, normalize_area
from core.synthesis import build_provenance, build_synthesis_input_bundle, build_synthesis_payload, save_synthesis_markdown
from core.intelligence_quality import corroboration_profile, clean, source_key, tokenize, evidence_text
from core.llm_provider import generate as llm_generate

LOGGER = logging.getLogger("rif.synthesis_agent")

DEFAULT_OUTPUT_DIR = Path("outputs/synthesis/generated")
DEFAULT_OUTPUTS_ROOT = Path("outputs")
DEFAULT_MANIFEST_DIR = Path("outputs/synthesis/manifest")
AGENT_NAME = "synthesis"
LAYER_NAME = "Intelligence"

TOPIC_LABELS = {
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
}


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def cleaned_text(value: str) -> str:
    return " ".join((value or "").split()).strip()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "item"


def _hash_payload(value: Any) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


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


def load_clusters(outputs_root: Path, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clusters_path = outputs_root / "processed" / "clusters" / "clusters.json"
    if clusters_path.exists():
        try:
            data = json.loads(clusters_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    return build_problem_clusters(records, threshold=0.35)


def load_manifest_entries(manifest_dir: Path) -> list[dict[str, Any]]:
    index_path = manifest_dir / "index.json"
    if not index_path.exists():
        return []
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    entries = [item for item in data if isinstance(item, dict)]
    live_entries: list[dict[str, Any]] = []
    for entry in entries:
        synthesized_file = cleaned_text(str(entry.get("synthesized_file", "")))
        if synthesized_file and not Path(synthesized_file).exists():
            continue
        live_entries.append(entry)
    return live_entries


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


def filter_clusters(clusters: list[dict[str, Any]], *, area: str | None = None, source_url: str | None = None) -> list[dict[str, Any]]:
    normalized_area = normalize_area(area) if area else None
    filtered: list[dict[str, Any]] = []
    for cluster in clusters:
        if normalized_area and normalize_area(cluster.get("area")) != normalized_area:
            continue
        if source_url:
            members = cluster.get("members", [])
            if not any(cleaned_text(str(member.get("source_url", ""))) == cleaned_text(source_url) for member in members):
                continue
        filtered.append(cluster)
    return filtered


def build_manual_record(*, text: str, title: str, source: str, area: str | None) -> dict[str, Any]:
    normalized_area = normalize_area(area) or "Unscoped"
    clean = cleaned_text(text)
    tokens = sorted({token for token in re.findall(r"[a-zA-Z0-9]+", clean.lower()) if len(token) > 2})
    return {
        "record_id": slugify(f"{title}-{normalized_area}")[:24],
        "file_path": "",
        "record_kind": "manual_normalized",
        "agent_name": "manual",
        "layer": "Manual",
        "research_area": normalized_area,
        "title": title or "Manual Signal",
        "source_url": source,
        "collected_at": datetime.now(UTC).isoformat(),
        "collection_mode": "manual_text",
        "source_type": "Manual",
        "evidence_type": "Manual",
        "actor": "Manual",
        "focus": normalized_area,
        "problem_statement": clean[:320],
        "context_summary": clean[:1200],
        "evidence_snippets": [clean[:320]] if clean else [],
        "excerpt_windows": [clean[:500]] if clean else [],
        "named_entities": [],
        "linked_urls": [source] if source else [],
        "keywords": tokens[:20],
        "agent_specific_fields": {},
        "machine_metadata": {},
        "search_text": clean,
        "tokens": tokens,
    }


def build_single_record_cluster(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "cluster_id": f"{normalize_area(record.get('research_area')) or 'Unscoped'}-manual-1",
        "area": normalize_area(record.get("research_area")) or "Unscoped",
        "representative_problem": cleaned_text(str(record.get("problem_statement", ""))) or cleaned_text(str(record.get("title", ""))),
        "supporting_layers": [cleaned_text(str(record.get("layer", ""))) or "Unknown"],
        "signal_types": ["manual_signal"],
        "evidence_types": [cleaned_text(str(record.get("evidence_type", ""))) or "Manual"],
        "actors": [cleaned_text(str(record.get("actor", ""))) or "Manual"],
        "members": [
            {
                "title": record.get("title"),
                "file_path": record.get("file_path", ""),
                "layer": record.get("layer"),
                "actor": record.get("actor", ""),
                "source_url": record.get("source_url", ""),
                "evidence_type": record.get("evidence_type", ""),
                "problem_statement": record.get("problem_statement", ""),
            }
        ],
        "top_terms": list(record.get("keywords", []))[:8],
        "evidence_links": [record.get("file_path", "")] if record.get("file_path") else [],
    }


def build_cluster_signature(cluster: dict[str, Any]) -> str:
    normalized = {
        "cluster_id": cleaned_text(str(cluster.get("cluster_id", ""))),
        "area": normalize_area(cluster.get("area")) or "Unscoped",
        "representative_problem": cleaned_text(str(cluster.get("representative_problem", ""))),
        "supporting_layers": sorted(cleaned_text(str(item)) for item in cluster.get("supporting_layers", []) if cleaned_text(str(item))),
        "signal_types": sorted(cleaned_text(str(item)) for item in cluster.get("signal_types", []) if cleaned_text(str(item))),
        "evidence_types": sorted(cleaned_text(str(item)) for item in cluster.get("evidence_types", []) if cleaned_text(str(item))),
        "actors": sorted(cleaned_text(str(item)) for item in cluster.get("actors", []) if cleaned_text(str(item))),
        "evidence_links": sorted(cleaned_text(str(item)) for item in cluster.get("evidence_links", []) if cleaned_text(str(item))),
        "members": [
            {
                "title": cleaned_text(str(member.get("title", ""))),
                "layer": cleaned_text(str(member.get("layer", ""))),
                "actor": cleaned_text(str(member.get("actor", ""))),
                "source_url": cleaned_text(str(member.get("source_url", ""))),
                "evidence_type": cleaned_text(str(member.get("evidence_type", ""))),
                "problem_statement": cleaned_text(str(member.get("problem_statement", ""))),
            }
            for member in cluster.get("members", [])
        ],
    }
    return _hash_payload(normalized)


def infer_topic_tags(text: str) -> list[str]:
    lowered = text.lower()
    tags: list[str] = []
    for topic, keywords in TOPIC_LABELS.items():
        if any(keyword in lowered for keyword in keywords):
            tags.append(f"#{topic}")
    return tags


def _bundle_records_text(bundle: dict[str, Any]) -> str:
    parts: list[str] = []
    for record in bundle.get("normalized_records", []):
        parts.extend(
            [
                cleaned_text(str(record.get("title", ""))),
                cleaned_text(str(record.get("problem_statement", ""))),
                cleaned_text(str(record.get("context_summary", ""))),
            ]
        )
    return " ".join(part for part in parts if part)


def build_evidence_assessment(cluster: dict[str, Any], evidence_inputs: list[dict[str, Any]]) -> dict[str, Any]:
    members = cluster.get("members", [])
    profile = corroboration_profile(members)
    research_layers = profile["research_evidence_layers"]
    independent_sources = profile["independent_sources"]
    independent_evidence_units = profile.get("independent_evidence_units", independent_sources)
    texts = [clean(m.get("problem_statement") or "") for m in members if clean(m.get("problem_statement") or "")]
    positive = [t for t in texts if any(k in t.lower() for k in ("solved", "works reliably", "effective", "addresses the gap"))]
    negative = [t for t in texts if any(k in t.lower() for k in ("unresolved", "limitation", "fails", "bottleneck", "gap", "constraint", "remains"))]
    contradiction_status = "potential_tension" if positive and negative else "no_explicit_tension_detected"

    claims = [
        {
            "claim": "The problem recurs across collected evidence.",
            "support": independent_evidence_units,
            "confidence": "High" if independent_evidence_units >= 3 else "Medium" if independent_evidence_units >= 2 else "Low",
            "evidence_basis": "independent evidence units after duplicate/derivative provenance handling",
        },
        {
            "claim": "The evidence has cross-layer support.",
            "support": research_layers,
            "confidence": "High" if research_layers >= 3 else "Medium" if research_layers >= 2 else "Low",
            "evidence_basis": "research-evidence layer count",
        },
        {
            "claim": "There are unresolved-limitation signals in the collected evidence.",
            "support": len(negative),
            "confidence": "Medium" if len(negative) >= 2 else "Low" if negative else "None",
            "evidence_basis": "problem statements containing limitation/bottleneck/gap language",
        },
    ]
    return {
        "independent_sources": independent_sources,
        "independent_evidence_units": independent_evidence_units,
        "independent_layers": profile["independent_layers"],
        "research_evidence_layers": research_layers,
        "independence_ratio": profile["independence_ratio"],
        "contradiction_status": contradiction_status,
        "positive_signal_count": len(positive),
        "negative_signal_count": len(negative),
        "claims": claims,
        "evidence_input_count": len(evidence_inputs),
        "novelty_status": "not_established",
    }



def _llm_synthesis_review(*, problem: str, evidence_texts: list[str], positive_count: int, negative_count: int) -> dict[str, Any]:
    """Review only ambiguous synthesis/gap cases with the local LLM.

    The LLM is an interpreter, not the source of evidence. It receives only the
    preserved evidence and must return strict JSON. Failure is represented as
    an uncertain review so synthesis never depends on the LLM being available.
    """
    prompt = f"""You are the RIF research-synthesis reviewer.
Decide whether the supplied evidence supports a defensible *provisional* research gap.
Do not claim novelty and do not invent facts. Use only the evidence below.
Return JSON only with keys: gap_supported (boolean), confidence (0.0-1.0),
status (supported|contested|insufficient|uncertain), reason (string).

Problem:
{problem}

Positive/solution signals: {positive_count}
Negative/limitation signals: {negative_count}

Evidence:
""" + "\n---\n".join(evidence_texts[:12])
    try:
        raw = llm_generate(prompt)
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise ValueError("LLM did not return a JSON object")
        data = json.loads(match.group(0))
        supported = bool(data.get("gap_supported", False))
        confidence = max(0.0, min(float(data.get("confidence", 0.0)), 1.0))
        status = str(data.get("status", "uncertain")).strip().lower()
        if status not in {"supported", "contested", "insufficient", "uncertain"}:
            status = "uncertain"
        if status == "supported" and not supported:
            status = "uncertain"
        return {
            "used": True,
            "available": True,
            "gap_supported": supported,
            "confidence": confidence,
            "status": status,
            "reason": cleaned_text(str(data.get("reason", ""))),
        }
    except Exception as exc:
        LOGGER.warning("Ambiguous synthesis LLM review unavailable: %s", exc)
        return {
            "used": False,
            "available": False,
            "gap_supported": False,
            "confidence": 0.0,
            "status": "uncertain",
            "reason": f"LLM review unavailable: {exc}",
        }


def _synthesis_decision(evidence_assessment: dict[str, Any], *, problem: str, evidence_inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministically decide clear cases; send only ambiguous cases to LLM."""
    positive = evidence_assessment["positive_signal_count"]
    negative = evidence_assessment["negative_signal_count"]
    independent = evidence_assessment["independent_evidence_units"]
    layers = evidence_assessment["research_evidence_layers"]
    texts = [cleaned_text(str(item.get("excerpt", "")))[:500] for item in evidence_inputs if str(item.get("excerpt", "")).strip()]

    # Clear insufficient case: no limitation signal at all.
    if negative == 0:
        return {
            "decision": "insufficient_evidence",
            "gap_supported": False,
            "confidence": 0.90 if independent <= 1 else 0.82,
            "llm_review": {"used": False, "reason": "No explicit unresolved-limitation signal was found."},
        }

    # Clear supported case: multiple independent units and no tension.
    if negative >= 2 and independent >= 2 and not (positive and negative):
        conf = min(0.95, 0.55 + 0.10 * min(independent, 3) + 0.05 * min(layers, 3))
        return {
            "decision": "provisional_gap_supported",
            "gap_supported": True,
            "confidence": conf,
            "llm_review": {"used": False, "reason": "Multiple independent evidence units contain unresolved-limitation signals without detected tension."},
        }

    # Ambiguous: contradictory signals, weak evidence, or mixed support.
    review = _llm_synthesis_review(problem=problem, evidence_texts=texts, positive_count=positive, negative_count=negative)
    if review.get("available"):
        status = review.get("status", "uncertain")
        if status == "supported" and review.get("gap_supported"):
            decision = "provisional_gap_supported"
        elif status == "contested":
            decision = "contested_gap"
        elif status == "insufficient":
            decision = "insufficient_evidence"
        else:
            decision = "uncertain"
    else:
        decision = "uncertain"
    return {
        "decision": decision,
        "gap_supported": decision == "provisional_gap_supported",
        "confidence": float(review.get("confidence", 0.0)),
        "llm_review": review,
    }

def _cluster_terms(cluster: dict[str, Any]) -> set[str]:
    text = " ".join([
        str(cluster.get("representative_problem", "")),
        " ".join(str(x) for x in cluster.get("top_terms", []) or []),
        " ".join(str(x) for x in cluster.get("signal_types", []) or []),
    ]).lower()
    return {t for t in re.findall(r"[a-z0-9]{4,}", text) if t not in {"research", "problem", "signal", "evidence"}}


def build_cross_cluster_groups(clusters: list[dict[str, Any]], max_groups: int = 12, group_size: int = 4) -> list[dict[str, Any]]:
    """Build bounded cross-cluster opportunity bundles without inventing evidence.

    Each bundle contains evidence from multiple independent clusters whenever the
    corpus permits it. This prevents the old one-cluster -> one-synthesis -> one-idea
    fan-out while preserving every underlying evidence link.
    """
    remaining = list(sorted(clusters, key=lambda c: len(c.get("members", [])), reverse=True))
    groups: list[dict[str, Any]] = []
    while remaining and len(groups) < max_groups:
        seed = remaining.pop(0)
        seed_terms = _cluster_terms(seed)
        scored = []
        for idx, candidate in enumerate(remaining):
            terms = _cluster_terms(candidate)
            union = seed_terms | terms
            similarity = len(seed_terms & terms) / max(1, len(union))
            layer_bonus = len(set(seed.get("supporting_layers", []) or []) & set(candidate.get("supporting_layers", []) or [])) * 0.05
            scored.append((similarity + layer_bonus, idx, candidate))
        scored.sort(key=lambda x: x[0], reverse=True)
        chosen = [seed]
        chosen_indices = []
        for _, idx, candidate in scored[: max(0, group_size - 1)]:
            chosen.append(candidate)
            chosen_indices.append(idx)
        for idx in sorted(chosen_indices, reverse=True):
            remaining.pop(idx)

        evidence_links = list(dict.fromkeys(
            str(link) for c in chosen for link in (c.get("evidence_links", []) or []) if cleaned_text(str(link))
        ))
        members = [m for c in chosen for m in (c.get("members", []) or [])]
        layers = list(dict.fromkeys(
            cleaned_text(str(layer)) for c in chosen for layer in (c.get("supporting_layers", []) or []) if cleaned_text(str(layer))
        ))
        actors = list(dict.fromkeys(
            cleaned_text(str(actor)) for c in chosen for actor in (c.get("actors", []) or []) if cleaned_text(str(actor))
        ))
        signals = list(dict.fromkeys(
            cleaned_text(str(signal)) for c in chosen for signal in (c.get("signal_types", []) or []) if cleaned_text(str(signal))
        ))
        top_terms = list(dict.fromkeys(
            cleaned_text(str(term)) for c in chosen for term in (c.get("top_terms", []) or []) if cleaned_text(str(term))
        ))[:12]
        area = normalize_area(seed.get("area")) or "Unscoped"
        funding_call_ids = list(dict.fromkeys(
            str(x) for c in chosen for x in (c.get("funding_call_ids", []) or []) if str(x).strip()
        ))

        # Only use a strong call-specific ID if it was explicitly present in the seed.
        # Do NOT invent strong call-specific ownership by picking from the array.
        funding_call_id = seed.get("funding_call_id")

        groups.append({
            "cluster_id": "cross-" + "-".join(cleaned_text(str(c.get("cluster_id", ""))) for c in chosen[:3]),
            "area": area,
            "representative_problem": " | ".join(cleaned_text(str(c.get("representative_problem", ""))) for c in chosen if cleaned_text(str(c.get("representative_problem", ""))))[:1200],
            "supporting_layers": layers,
            "signal_types": signals,
            "evidence_types": list(dict.fromkeys(str(x) for c in chosen for x in (c.get("evidence_types", []) or []))),
            "actors": actors,
            "top_terms": top_terms,
            "members": members,
            "evidence_links": evidence_links,
            "source_cluster_ids": [cleaned_text(str(c.get("cluster_id", ""))) for c in chosen],
            "funding_call_id": funding_call_id,
            "funding_call_ids": funding_call_ids,
        })
    # If more clusters remain than the group cap, merge their evidence into the
    # final group rather than silently dropping observations.
    if remaining and groups:
        tail = remaining
        target = groups[-1]
        for c in tail:
            target["source_cluster_ids"].append(cleaned_text(str(c.get("cluster_id", ""))))
            target["members"].extend(c.get("members", []) or [])
            target["evidence_links"] = list(dict.fromkeys(target["evidence_links"] + [str(x) for x in c.get("evidence_links", []) if cleaned_text(str(x))]))
            target["supporting_layers"] = list(dict.fromkeys(target["supporting_layers"] + [str(x) for x in c.get("supporting_layers", []) if cleaned_text(str(x))]))
            if "funding_call_ids" in target:
                target["funding_call_ids"] = list(dict.fromkeys(target["funding_call_ids"] + [str(x) for x in c.get("funding_call_ids", []) if str(x).strip()]))
    return groups


def build_payload_from_cluster(cluster: dict[str, Any], outputs_root: Path, *, note: str | None = None) -> dict[str, Any]:
    evidence_links = [item for item in cluster.get("evidence_links", []) if cleaned_text(str(item))]
    if not evidence_links:
        raise ValueError(f"Cluster {cluster.get('cluster_id')} has no evidence links to synthesize from.")

    bundle = build_synthesis_input_bundle(input_files=evidence_links, notes=note)
    area = normalize_area(cluster.get("area")) or bundle.get("research_area") or "Unscoped"
    layers = cluster.get("supporting_layers", []) or bundle.get("layers", [])
    actor_text = ", ".join(cluster.get("actors", [])[:4]) or ", ".join(bundle.get("actors", [])[:4]) or "ecosystem actors"
    signal_text = ", ".join(cluster.get("signal_types", [])[:4]) or "repeated signals"
    evidence_inputs = bundle.get("evidence_inputs", [])
    evidence_count = len(evidence_inputs)
    evidence_assessment = build_evidence_assessment(cluster, evidence_inputs)
    member_count = len(cluster.get("members", []))
    representative_problem = cleaned_text(str(cluster.get("representative_problem", "")))
    joined_text = _bundle_records_text(bundle)
    topic_tags = infer_topic_tags(f"{representative_problem} {joined_text}")

    title_bits = [area]
    top_terms = [cleaned_text(str(term)) for term in cluster.get("top_terms", []) if cleaned_text(str(term))]
    if top_terms:
        title_bits.append(" ".join(word.title() for word in top_terms[:3]))
    else:
        title_bits.append(cleaned_text(str(cluster.get("cluster_id", "Synthesis"))))
    title_bits.append("Research Synthesis")
    title = " ".join(part for part in title_bits if part).strip()

    why_important = (
        f"This synthesis is backed by {member_count} clustered signal(s) across {len(layers)} layer(s). "
        f"It reflects repeated ecosystem evidence rather than a single isolated artifact, which makes it a stronger candidate for research prioritization."
    )
    existing_solutions = (
        f"Observed solution/activity signals include {signal_text}, with activity associated with {actor_text}. "
        "These observations show that work or attention exists; they do not by themselves prove that an approach is complete or effective."
    )
    synthesis_decision = _synthesis_decision(
        evidence_assessment,
        problem=representative_problem,
        evidence_inputs=evidence_inputs,
    )
    if synthesis_decision["decision"] == "provisional_gap_supported":
        gap = (
            f"Provisional research gap: the collected evidence contains {evidence_assessment['negative_signal_count']} unresolved-limitation signal(s) "
            f"across {evidence_assessment['independent_evidence_units']} independent evidence unit(s). "
            "This is a candidate gap, not a novelty claim, and requires external literature/open-source validation."
        )
    elif synthesis_decision["decision"] == "contested_gap":
        gap = (
            "Contested research gap: the evidence contains both limitation and solution/effectiveness signals. "
            "The conflict must be resolved with stronger external evidence before treating this as a firm gap."
        )
    elif synthesis_decision["decision"] == "uncertain":
        gap = (
            "Uncertain research gap: the available evidence is mixed or ambiguous. "
            "Additional corroboration and existing-solution review are required before promoting this cluster as a gap."
        )
    else:
        gap = (
            "No defensible unresolved research gap can be established from the current evidence alone. "
            "Further corroboration and existing-solution review are required before promoting this cluster as a research gap."
        )
    idea = (
        f"Use this cluster as a research starting point: turn the repeated {signal_text} into a scoped prototype or evaluation plan, "
        f"then validate it against the evidence already preserved in the supporting layer artifacts."
    )
    synthesis_conf = float(synthesis_decision.get("confidence", 0.0))
    confidence = "High" if synthesis_conf >= 0.80 else "Medium" if synthesis_conf >= 0.60 else "Low"
    feasibility = "High" if evidence_count >= 4 else "Moderate to High" if evidence_count >= 2 else "Moderate"
    provenance = build_provenance(
        input_files=evidence_links,
        synthesis_method="heuristic_agent",
        source_urls=bundle.get("source_urls", []),
        notes=note or f"Synthesized from cluster {cluster.get('cluster_id')}",
    )
    provenance["cluster_id"] = cleaned_text(str(cluster.get("cluster_id", "")))
    provenance["cluster_signature"] = build_cluster_signature(cluster)
    tags = ["#Synthesis", "#Intelligence", f"#{area.replace('-', '')}"] + [f"#layer-{slugify(layer)}" for layer in layers] + topic_tags

    payload = build_synthesis_payload(
        title=title,
        problem=representative_problem or "Problem statement pending from cluster evidence.",
        source_inputs=evidence_links,
        layer_or_layers=layers,
        synthesis_scope=bundle.get("synthesis_scope", "single-layer"),
        research_area=area,
        why_important=why_important,
        existing_solutions=existing_solutions,
        gap=gap,
        idea=idea,
        feasibility=feasibility,
        confidence=confidence,
        tags=list(dict.fromkeys(tags)),
        provenance=provenance,
        evidence_inputs=evidence_inputs,
        funding_call_id=cluster.get("funding_call_id"),
        funding_call_ids=cluster.get("funding_call_ids", []),
        supporting_record_ids=[m.get("record_id") for m in cluster.get("members", []) if m.get("record_id")],
    )
    payload["cluster_id"] = cleaned_text(str(cluster.get("cluster_id", "")))
    payload["cluster_signature"] = provenance["cluster_signature"]
    evidence_assessment["synthesis_decision"] = synthesis_decision
    payload["evidence_assessment"] = evidence_assessment

    if synthesis_decision.get("decision") == "insufficient_evidence" or not synthesis_decision.get("gap_supported", False):
        if "priority_readiness" in payload:
            payload["priority_readiness"]["ready_for_high_priority"] = False
            payload["priority_readiness"]["rationale"] += " Synthesis marked as insufficient evidence or lacking gap support."

    return payload


def build_payload_from_manual_record(record: dict[str, Any], cluster: dict[str, Any], *, note: str | None = None) -> dict[str, Any]:
    area = normalize_area(record.get("research_area")) or "Unscoped"
    source_url = cleaned_text(str(record.get("source_url", "")))
    problem = cleaned_text(str(record.get("problem_statement", ""))) or cleaned_text(str(record.get("title", "")))
    context = cleaned_text(str(record.get("context_summary", "")))
    title = cleaned_text(str(record.get("title", ""))) or "Manual Synthesis Input"
    evidence_inputs = [
        {
            "input_file": "",
            "title": title,
            "layer": cleaned_text(str(record.get("layer", ""))) or "Manual",
            "research_area": area,
            "source_url": source_url,
            "excerpt": problem[:320],
        },
        {
            "input_file": "",
            "title": title,
            "layer": cleaned_text(str(record.get("layer", ""))) or "Manual",
            "research_area": area,
            "source_url": source_url,
            "excerpt": context[:320] or problem[:320],
        },
    ]
    tags = ["#Synthesis", "#Intelligence", f"#{area.replace('-', '')}"] + infer_topic_tags(f"{problem} {context}")
    provenance = build_provenance(
        input_files=[],
        synthesis_method="heuristic_agent",
        source_urls=[source_url] if source_url else [],
        notes=note or "Synthesized from manual input",
    )
    provenance["cluster_id"] = cleaned_text(str(cluster.get("cluster_id", "")))
    provenance["cluster_signature"] = build_cluster_signature(cluster)
    evidence_assessment = build_evidence_assessment(cluster, evidence_inputs)
    synthesis_decision = _synthesis_decision(
        evidence_assessment,
        problem=problem,
        evidence_inputs=evidence_inputs,
    )
    payload = build_synthesis_payload(
        title=f"{area} Manual Research Synthesis",
        problem=problem or "Problem statement pending from manual synthesis input.",
        source_inputs=[source_url] if source_url else ["manual://synthesis/manual-input"],
        layer_or_layers=["Manual"],
        synthesis_scope="single-layer",
        research_area=area,
        why_important=(
            "This synthesis was generated from a manually supplied high-signal problem statement. "
            "It is useful as a fast first-pass intelligence artifact before broader corroboration is collected."
        ),
        existing_solutions=(
            "The current baseline likely includes fragmented tooling, point solutions, or policy work, "
            "but the manual signal still suggests an unresolved practical gap."
        ),
        gap=(
            "The manual evidence points to a missing reusable pattern that could be prototyped and later validated "
            "against broader ecosystem evidence."
        ),
        idea=(
            "Turn the manual problem statement into a scoped prototype and evaluation plan, then use future collected evidence "
            "to either validate or refine the direction."
        ),
        feasibility="Moderate to High",
        confidence="Medium",
        tags=list(dict.fromkeys(tags)),
        provenance=provenance,
        evidence_inputs=evidence_inputs,
        funding_call_id=record.get("funding_call_id"),
        funding_call_ids=record.get("funding_call_ids", []),
        supporting_record_ids=[record.get("record_id")] if record.get("record_id") else [],
    )
    payload["cluster_id"] = cleaned_text(str(cluster.get("cluster_id", "")))
    payload["cluster_signature"] = provenance["cluster_signature"]
    evidence_assessment["synthesis_decision"] = synthesis_decision
    payload["evidence_assessment"] = evidence_assessment

    if synthesis_decision.get("decision") == "insufficient_evidence" or not synthesis_decision.get("gap_supported", False):
        if "priority_readiness" in payload:
            payload["priority_readiness"]["ready_for_high_priority"] = False
            payload["priority_readiness"]["rationale"] += " Synthesis marked as insufficient evidence or lacking gap support."

    return payload


def run_agent(mode: str, area: str | None = None, input_data: dict[str, Any] | None = None, funding_context=None) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    payload = input_data or {}
    outputs_root = Path(payload.get("outputs_root", DEFAULT_OUTPUTS_ROOT))
    output_dir = Path(payload.get("output_dir", DEFAULT_OUTPUT_DIR))
    manifest_dir = Path(payload.get("manifest_dir", DEFAULT_MANIFEST_DIR))

    LOGGER.info("Starting synthesis agent run: mode=%s area=%s", mode, area)
    try:
        outputs: list[dict[str, Any]] = []
        warnings: list[str] = []
        manifest_entries = load_manifest_entries(manifest_dir)
        existing_cluster_signatures = {
            cleaned_text(str(entry.get("cluster_signature", ""))): entry
            for entry in manifest_entries
            if cleaned_text(str(entry.get("cluster_signature", "")))
        }
        skipped_unchanged_clusters = 0

        if mode == "manual_text":
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
            title = cleaned_text(str(payload.get("title", ""))) or "Manual Synthesis Input"
            source = cleaned_text(str(payload.get("source", ""))) or "manual://synthesis/manual-input"
            record = build_manual_record(text=text, title=title, source=source, area=area)
            cluster = build_single_record_cluster(record)
            cluster_payloads = [build_payload_from_manual_record(record, cluster, note="Manual synthesis input")]
            items_processed = 1
        else:
            records = load_normalized_records(outputs_root)
            if mode == "manual_url":
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
                records = filter_records(records, area=area, source_url=url)
                clusters = filter_clusters(load_clusters(outputs_root, records), area=area, source_url=url)
            elif mode == "configured_scan":
                records = filter_records(records, area=area)
                clusters = filter_clusters(load_clusters(outputs_root, records), area=area)
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

            if not records:
                return create_partial_response(
                    agent=AGENT_NAME,
                    layer=LAYER_NAME,
                    mode=mode,
                    area=area,
                    items_processed=0,
                    items_saved=0,
                    outputs=[],
                    warnings=["No normalized records matched the synthesis request."],
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                )
            if not clusters:
                warnings.append("No clusters were available; synthesis requires clustered evidence and was skipped.")
                return create_partial_response(
                    agent=AGENT_NAME,
                    layer=LAYER_NAME,
                    mode=mode,
                    area=area,
                    items_processed=len(records),
                    items_saved=0,
                    outputs=[],
                    warnings=warnings,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                )

            cluster_payloads = []
            opportunity_groups = build_cross_cluster_groups(clusters)
            for cluster in opportunity_groups:
                try:
                    cluster_signature = build_cluster_signature(cluster)
                    existing_entry = existing_cluster_signatures.get(cluster_signature)
                    if existing_entry:
                        skipped_unchanged_clusters += 1
                        continue
                    cluster_payloads.append(build_payload_from_cluster(
                        cluster, outputs_root,
                        note=f"Cross-cluster synthesis over {len(cluster.get('source_cluster_ids', []))} source clusters."
                    ))
                except Exception as exc:
                    warnings.append(f"Skipped cross-cluster group {cluster.get('cluster_id')}: {exc}")
            items_processed = len(clusters)
            warnings.append(f"Collapsed {len(clusters)} clusters into {len(opportunity_groups)} bounded cross-cluster synthesis groups.")

        if not cluster_payloads:
            return create_partial_response(
                agent=AGENT_NAME,
                layer=LAYER_NAME,
                mode=mode,
                area=area,
                items_processed=0,
                items_saved=0,
                outputs=[],
                warnings=warnings or ["No synthesis payloads could be built."],
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        for synthesis_payload in cluster_payloads:
            area_dir = output_dir / slugify(normalize_area(synthesis_payload.get("research_area")) or "unscoped")
            md_path = save_synthesis_markdown(
                payload=synthesis_payload,
                output_dir=area_dir,
                manifest_dir=manifest_dir,
            )
            outputs.append(
                {
                    "title": synthesis_payload["title"],
                    "problem": synthesis_payload["problem"],
                    "problem_signature": f"{synthesis_payload['research_area']} | {','.join(synthesis_payload['layer_or_layers'])} | synthesis",
                    "confidence": synthesis_payload["confidence"],
                    "source": ", ".join(synthesis_payload["provenance"].get("source_urls", [])) or "Unknown",
                    "markdown_path": str(md_path.resolve()),
                }
            )

        warnings.append("Synthesis agent saved intelligence artifacts under outputs/synthesis/generated/.")
        response = create_success_response(
            agent=AGENT_NAME,
            layer=LAYER_NAME,
            mode=mode,
            area=area,
            items_processed=items_processed,
            items_saved=len(outputs),
            outputs=outputs,
            warnings=warnings,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        response["metadata"]["synthesis_summary"] = {
            "syntheses_created": len(outputs),
            "skipped_unchanged_clusters": skipped_unchanged_clusters,
            "manifest_dir": str(manifest_dir.resolve()),
            "output_dir": str(output_dir.resolve()),
        }
        return response
    except Exception as exc:
        LOGGER.exception("Synthesis agent failed during run_agent")
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
    parser = argparse.ArgumentParser(description="Run the synthesis intelligence agent.")
    parser.add_argument("--mode", choices=["configured_scan", "manual_url", "manual_text"], default="configured_scan")
    parser.add_argument("--area")
    parser.add_argument("--url")
    parser.add_argument("--text")
    parser.add_argument("--title")
    parser.add_argument("--source")
    parser.add_argument("--outputs-root", default=str(DEFAULT_OUTPUTS_ROOT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--manifest-dir", default=str(DEFAULT_MANIFEST_DIR))
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)
    payload: dict[str, Any] = {
        "outputs_root": args.outputs_root,
        "output_dir": args.output_dir,
        "manifest_dir": args.manifest_dir,
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
