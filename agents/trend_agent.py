from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import textwrap
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.intelligence import build_problem_clusters
from core.normalization import ensure_normalized_collection_records, save_normalized_collection_records
from core.intelligence_quality import corroboration_profile, parse_date, source_key, temporal_buckets
from core.schemas import create_error_response, create_partial_response, create_success_response, normalize_area
from core.llm_provider import generate as llm_generate

LOGGER = logging.getLogger("rif.trend_agent")

DEFAULT_OUTPUT_DIR = Path("outputs/processed/trends")
DEFAULT_OUTPUTS_ROOT = Path("outputs")
LAYER_NAME = "Processing"
AGENT_NAME = "trend"

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
}


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def cleaned_text(value: str) -> str:
    return " ".join((value or "").split()).strip()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "item"


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
    clean = cleaned_text(text)
    tokens = sorted({token for token in re.findall(r"[a-zA-Z0-9]+", clean.lower()) if len(token) > 2})
    return {
        "record_id": slugify(f"{title}-{normalized_area}")[:24],
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


def load_cluster_artifacts(outputs_root: Path) -> list[dict[str, Any]]:
    clusters_path = outputs_root / "processed" / "clusters" / "clusters.json"
    if clusters_path.exists():
        try:
            data = json.loads(clusters_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    records = load_normalized_records(outputs_root)
    return build_problem_clusters(records, threshold=0.35)


def detect_topics(record: dict[str, Any]) -> list[str]:
    text = cleaned_text(
        " ".join(
            [
                str(record.get("title", "")),
                str(record.get("problem_statement", "")),
                str(record.get("context_summary", "")),
                " ".join(record.get("evidence_snippets", [])),
                " ".join(record.get("excerpt_windows", [])),
                str(record.get("focus", "")),
                str(record.get("source_type", "")),
                str(record.get("evidence_type", "")),
            ]
        )
    ).lower()
    topics: list[str] = []
    normalized_text = re.sub(r"[-_/]+", " ", text)
    normalized_text = re.sub(r"\s+", " ", normalized_text).strip()
    for topic, keywords in TOPIC_KEYWORDS.items():
        matched = False
        for keyword in keywords:
            normalized_keyword = re.sub(r"[-_/]+", " ", keyword.lower()).strip()
            if re.search(rf"(?<![a-z0-9]){re.escape(normalized_keyword)}(?![a-z0-9])", normalized_text):
                matched = True
                break
        if matched:
            topics.append(topic)
    return topics


def _expand_months(months: list[str]) -> list[str]:
    """Return a continuous YYYY-MM range so collection gaps are explicit."""
    if not months:
        return []
    start = datetime.strptime(months[0], "%Y-%m")
    end = datetime.strptime(months[-1], "%Y-%m")
    expanded: list[str] = []
    cursor = start
    while cursor <= end:
        expanded.append(cursor.strftime("%Y-%m"))
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)
    return expanded


def _month_metrics(month_records: list[dict[str, Any]]) -> dict[str, Any]:
    profile = corroboration_profile(month_records)
    return {
        "records": len(month_records),
        "independent_sources": profile["independent_sources"],
        "independent_evidence_units": profile["independent_evidence_units"],
        "independent_layers": profile["independent_layers"],
        "research_evidence_layers": profile["research_evidence_layers"],
        "independence_ratio": profile["independence_ratio"],
    }


def _trend_direction(values: list[float]) -> tuple[str, dict[str, float], bool]:
    """Classify direction from history, including growth and acceleration."""
    if len(values) < 3:
        return "insufficient_history", {"growth_rate": 0.0, "acceleration": 0.0, "recent_vs_baseline": 0.0}, False

    previous = values[-2]
    recent = values[-1]
    prior = values[-3]
    baseline = sum(values[:-1]) / max(len(values) - 1, 1)
    recent_growth = (recent - previous) / max(previous, 1.0)
    prior_growth = (previous - prior) / max(prior, 1.0)
    # Acceleration measures the change in absolute monthly activity, normalized
    # by the prior level; this distinguishes 1→2→4→8 from steady 1→2→3 growth.
    acceleration = ((recent - previous) - (previous - prior)) / max(previous, 1.0)
    recent_vs_baseline = (recent - baseline) / max(baseline, 1.0)

    if recent_growth >= 0.50 and acceleration >= 0.25:
        direction = "accelerating"
    elif recent_growth >= 0.25 and recent_vs_baseline >= 0.25 and acceleration >= 0.10:
        direction = "rising"
    elif recent_growth <= -0.25 and acceleration <= -0.10:
        direction = "declining"
    elif abs(recent_growth) <= 0.15 and abs(recent_vs_baseline) <= 0.20:
        direction = "stable"
    else:
        direction = "ambiguous"

    signals = {
        "growth_rate": round(recent_growth, 3),
        "previous_growth_rate": round(prior_growth, 3),
        "acceleration": round(acceleration, 3),
        "recent_vs_baseline": round(recent_vs_baseline, 3),
    }
    return direction, signals, True


def _trend_confidence(*, direction: str, values: list[float], independence: float, layer_support: float) -> float:
    if len(values) < 3:
        return 0.0
    previous = values[-2]
    recent = values[-1]
    baseline = sum(values[:-1]) / max(len(values) - 1, 1)
    growth_strength = min(1.0, abs(recent - previous) / max(previous, 1.0))
    recency_strength = min(1.0, abs(recent - baseline) / max(baseline, 1.0))
    history_strength = min(1.0, len(values) / 8.0)
    direction_bonus = 1.0 if direction in {"accelerating", "rising", "declining", "stable"} else 0.35
    score = (
        0.25 * growth_strength
        + 0.20 * recency_strength
        + 0.20 * max(0.0, min(1.0, independence))
        + 0.20 * max(0.0, min(1.0, layer_support))
        + 0.15 * history_strength
    ) * direction_bonus
    return round(max(0.0, min(1.0, score)), 3)


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


def _llm_trend_review(topic: str, series: list[dict[str, Any]], signals: dict[str, float], preliminary: str) -> dict[str, Any] | None:
    prompt = textwrap.dedent(
        f"""
        You are reviewing a research-intelligence trend decision. Do not invent data.
        Topic: {topic}
        Monthly series: {json.dumps(series, ensure_ascii=False)}
        Calculated signals: {json.dumps(signals, ensure_ascii=False)}
        Preliminary direction: {preliminary}

        Decide whether the observed evidence supports a genuine trend direction.
        Distinguish real movement from one-month spikes, collection gaps, duplicate/derivative reporting,
        or weak single-layer evidence. Return ONLY JSON:
        {{"decision":"rising|accelerating|declining|stable|uncertain",
          "confidence":0.0,
          "reason":"short evidence-based reason"}}
        """
    ).strip()
    try:
        result = _parse_llm_json(llm_generate(prompt))
    except Exception as exc:
        LOGGER.info("Trend LLM review unavailable for %s: %s", topic, exc)
        return None
    if not result:
        return None
    decision = str(result.get("decision", "uncertain")).strip().lower()
    if decision not in {"rising", "accelerating", "declining", "stable", "uncertain"}:
        return None
    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return {"decision": decision, "confidence": round(confidence, 3), "reason": str(result.get("reason", "")).strip()}


def _build_topic_trend(topic: str, buckets: dict[str, list[dict[str, Any]]], months: list[str]) -> dict[str, Any]:
    series: list[dict[str, Any]] = []
    for month in months:
        month_records = [r for r in buckets.get(month, []) if topic in detect_topics(r)]
        metrics = _month_metrics(month_records)
        series.append({"month": month, **metrics})

    values = [float(point["independent_evidence_units"]) for point in series]
    direction, signals, enough_history = _trend_direction(values)
    recent_point = series[-1] if series else {}
    independence = float(recent_point.get("independence_ratio", 0.0))
    layer_support = min(1.0, float(recent_point.get("research_evidence_layers", 0)) / 3.0)
    confidence = _trend_confidence(
        direction=direction,
        values=values,
        independence=independence,
        layer_support=layer_support,
    )
    llm_review = None
    if enough_history and direction == "ambiguous":
        llm_review = _llm_trend_review(topic, series, signals, direction)
        if llm_review and llm_review["confidence"] >= 0.70:
            direction = llm_review["decision"]
            confidence = round(max(confidence, llm_review["confidence"]), 3)
        else:
            direction = "uncertain"

    return {
        "topic": topic,
        "series": series,
        "recent": int(values[-1]) if values else 0,
        "previous": int(values[-2]) if len(values) > 1 else 0,
        "velocity": signals.get("recent_vs_baseline", 0.0),
        "direction": direction,
        "signals": signals,
        "confidence": confidence,
        "decision": "accepted" if direction in {"accelerating", "rising", "declining", "stable"} and confidence >= 0.50 else "uncertain",
        "llm_review": llm_review,
        "history_months": len(months),
        "observed_months": sum(1 for point in series if point["records"] > 0),
    }


def build_trend_report(records: list[dict[str, Any]], clusters: list[dict[str, Any]]) -> dict[str, Any]:
    area_counts = Counter(normalize_area(record.get("research_area")) or "Unscoped" for record in records)
    layer_counts = Counter(cleaned_text(str(record.get("layer", ""))) or "Unknown" for record in records)
    actor_counts = Counter(cleaned_text(str(record.get("actor", ""))) for record in records if cleaned_text(str(record.get("actor", ""))) and cleaned_text(str(record.get("actor", ""))).lower() != "unknown")
    source_type_counts = Counter(cleaned_text(str(record.get("source_type", ""))) or "Unknown" for record in records)
    evidence_type_counts = Counter(cleaned_text(str(record.get("evidence_type", ""))) or "Unknown" for record in records)
    topic_counts = Counter(topic for record in records for topic in detect_topics(record))
    cluster_area_counts = Counter(cluster.get("area", "Unscoped") for cluster in clusters)
    cluster_signal_counts = Counter(signal for cluster in clusters for signal in cluster.get("signal_types", []))
    cluster_layer_counts = Counter(layer for cluster in clusters for layer in cluster.get("supporting_layers", []))

    buckets = temporal_buckets(records)
    months = _expand_months(sorted(buckets))
    topic_trends = [_build_topic_trend(topic, buckets, months) for topic in topic_counts]
    topic_trends.sort(key=lambda item: (item["confidence"], abs(item["velocity"]), item["recent"]), reverse=True)

    cluster_trends = []
    for cluster in clusters:
        members = cluster.get("members", [])
        profile = corroboration_profile(members)
        member_buckets = temporal_buckets(members)
        cluster_months = _expand_months(sorted(member_buckets))
        cluster_series = []
        for month in cluster_months:
            metrics = _month_metrics(member_buckets.get(month, []))
            cluster_series.append({"month": month, **metrics})
        values = [float(point["independent_evidence_units"]) for point in cluster_series]
        direction, signals, enough_history = _trend_direction(values)
        recent_point = cluster_series[-1] if cluster_series else {}
        independence = float(recent_point.get("independence_ratio", 0.0))
        layer_support = min(1.0, float(recent_point.get("research_evidence_layers", 0)) / 3.0)
        confidence = _trend_confidence(direction=direction, values=values, independence=independence, layer_support=layer_support)
        llm_review = None
        if enough_history and direction == "ambiguous":
            llm_review = _llm_trend_review(str(cluster.get("representative_problem", "cluster")), cluster_series, signals, direction)
            if llm_review and llm_review["confidence"] >= 0.70:
                direction = llm_review["decision"]
                confidence = round(max(confidence, llm_review["confidence"]), 3)
            else:
                direction = "uncertain"
        evidence_units = profile.get("independent_evidence_units", profile["independent_sources"])
        evidence_strength = min(100, evidence_units * 12 + profile["research_evidence_layers"] * 15)
        if evidence_strength >= 70 and profile["research_evidence_layers"] >= 2:
            maturity = "strong_cross_layer"
        elif profile["independent_sources"] >= 3:
            maturity = "established"
        elif profile["independent_sources"] >= 2:
            maturity = "emerging"
        else:
            maturity = "single_source"
        cluster_trends.append({
            "cluster_id": cluster.get("cluster_id"),
            "area": cluster.get("area"),
            "representative_problem": cluster.get("representative_problem"),
            "independent_sources": profile["independent_sources"],
            "independent_evidence_units": evidence_units,
            "independent_layers": profile["independent_layers"],
            "research_evidence_layers": profile["research_evidence_layers"],
            "evidence_strength": evidence_strength,
            "maturity": maturity,
            "series": cluster_series,
            "direction": direction,
            "signals": signals,
            "confidence": confidence,
            "decision": "accepted" if direction in {"accelerating", "rising", "declining", "stable"} and confidence >= 0.50 else "uncertain",
            "llm_review": llm_review,
        })

    latest_records = sorted(records, key=lambda record: parse_date(record.get("collected_at") or record.get("date")) or datetime.min.replace(tzinfo=UTC), reverse=True)[:10]
    
    # Aggregate funding call ids from records
    funding_call_ids_set = set()
    primary_call_id = None
    for r in records:
        if r.get("funding_call_id"):
            primary_call_id = r.get("funding_call_id")
            funding_call_ids_set.add(r.get("funding_call_id"))
        for cid in r.get("funding_call_ids", []):
            if cid:
                funding_call_ids_set.add(cid)
    
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "funding_call_id": primary_call_id,
        "funding_call_ids": sorted(list(funding_call_ids_set)),
        "counts": {"records": len(records), "clusters": len(clusters)},
        "area_counts": dict(area_counts),
        "layer_counts": dict(layer_counts),
        "actor_counts": dict(actor_counts.most_common(15)),
        "source_type_counts": dict(source_type_counts),
        "evidence_type_counts": dict(evidence_type_counts),
        "topic_counts": dict(topic_counts.most_common(20)),
        "cluster_area_counts": dict(cluster_area_counts),
        "cluster_signal_counts": dict(cluster_signal_counts),
        "cluster_layer_counts": dict(cluster_layer_counts),
        "topic_trends": topic_trends[:20],
        "cluster_trends": sorted(cluster_trends, key=lambda x: (-x["confidence"], -(x["evidence_strength"]), x["cluster_id"] or ""))[:30],
        "time_buckets": {month: len(items) for month, items in buckets.items()},
        "repeated_problem_patterns": [
            {
                "cluster_id": cluster.get("cluster_id"),
                "area": cluster.get("area"),
                "representative_problem": cleaned_text(str(cluster.get("representative_problem", "")))[:280],
                "supporting_layers": cluster.get("supporting_layers", []),
                "member_count": len(cluster.get("members", [])),
                "independent_source_count": cluster.get("independent_source_count", 0),
                "signal_types": cluster.get("signal_types", []),
            }
            for cluster in clusters[:20]
        ],
        "latest_records": [
            {"title": record.get("title"), "area": record.get("research_area"), "layer": record.get("layer"), "source_url": record.get("source_url"), "collected_at": record.get("collected_at")}
            for record in latest_records
        ],
    }

def build_markdown(report: dict[str, Any], area: str | None) -> str:
    lines = [
        "# Trend Insights",
        "",
        "## Problem",
        "Identify repeated and emerging themes across collected and processed RIF records.",
        "",
        "## Source",
        "Normalized records and clustering outputs",
        "",
        "## Layer",
        "Trend",
        "",
        "## Funding Call IDs",
        ", ".join(report.get("funding_call_ids", [])) or str(report.get("funding_call_id") or "Unknown"),
        "",
        "## Research Area",
        area or "Multi-Area",
        "",
        "## Why Important",
        "Trend analysis helps surface recurring ecosystem needs, repeated actors, and rising technical topics before deeper synthesis and proposal generation.",
        "",
        "## Existing Solutions",
        "Normalized records, tagging, and clustering already preserve structured evidence, but trend summaries make repeated movement visible over the aggregate dataset.",
        "",
        "## Gap",
        "Without explicit trend views, repeated patterns stay buried across many records and clusters.",
        "",
        "## Idea",
        "Use trend summaries to prioritize which clusters should move into synthesis, idea generation, and proposal drafting.",
        "",
        "## Feasibility",
        "High",
        "",
        "## Record Counts",
        f"- records: {report['counts']['records']}",
        f"- clusters: {report['counts']['clusters']}",
        "",
        "## Area Distribution",
    ]
    for key, value in sorted(report["area_counts"].items()):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Layer Distribution"])
    for key, value in sorted(report["layer_counts"].items()):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Top Topics"])
    for key, value in sorted(report["topic_counts"].items(), key=lambda item: (-item[1], item[0]))[:12]:
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Top Actors"])
    for key, value in sorted(report["actor_counts"].items(), key=lambda item: (-item[1], item[0]))[:12]:
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Topic Trends"])
    for item in report.get("topic_trends", [])[:10]:
        lines.append(f"- {item['topic']} | {item['direction']} | velocity={item['velocity']} | recent_sources={item['recent']}")
    lines.extend(["", "## Cluster Evidence Strength"])
    for item in report.get("cluster_trends", [])[:10]:
        lines.append(f"- {item['cluster_id']} | {item['maturity']} | independent_sources={item['independent_sources']} | research_layers={item['research_evidence_layers']}")
    lines.extend(["", "## Cluster Signal Types"])
    for key, value in sorted(report["cluster_signal_counts"].items(), key=lambda item: (-item[1], item[0]))[:12]:
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Repeated Problem Patterns"])
    patterns = report.get("repeated_problem_patterns", [])
    if patterns:
        for pattern in patterns[:10]:
            layers = ", ".join(pattern.get("supporting_layers", []))
            signals = ", ".join(pattern.get("signal_types", []))
            lines.append(
                f"- {pattern.get('cluster_id')} | {pattern.get('area')} | members={pattern.get('member_count')} | "
                f"layers={layers or 'n/a'} | signals={signals or 'n/a'} | {pattern.get('representative_problem')}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Latest Records"])
    latest = report.get("latest_records", [])
    if latest:
        for record in latest[:10]:
            lines.append(
                f"- {record.get('collected_at')} | {record.get('area')} | {record.get('layer')} | {record.get('title')}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Tags", "#Processing #Trend #Insights"])
    return "\n".join(lines) + "\n"


def save_outputs(report: dict[str, Any], output_dir: Path, area: str | None) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    scope = slugify(area or "multi-area")
    json_path = output_dir / f"{scope}-trends.json"
    md_path = output_dir / f"{scope}-insights.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(build_markdown(report, area), encoding="utf-8")
    return md_path, json_path


def run_agent(mode: str, area: str | None = None, input_data: dict[str, Any] | None = None, funding_context=None) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    payload = input_data or {}
    outputs_root = Path(payload.get("outputs_root", DEFAULT_OUTPUTS_ROOT))
    output_dir = Path(payload.get("output_dir", DEFAULT_OUTPUT_DIR))

    LOGGER.info("Starting trend agent run: mode=%s area=%s", mode, area)
    try:
        if mode == "configured_scan":
            records = filter_records(load_normalized_records(outputs_root), area=area)
        elif mode == "manual_url":
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
            records = filter_records(load_normalized_records(outputs_root), area=area, source_url=url)
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
            title = cleaned_text(str(payload.get("title", ""))) or "Manual Trend Input"
            source = cleaned_text(str(payload.get("source", ""))) or "manual://trend/manual-input"
            records = [build_manual_record(text=text, title=title, source=source, area=area)]
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
                warnings=["No normalized records matched the trend request."],
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        clusters = load_cluster_artifacts(outputs_root)
        if area:
            normalized_area = normalize_area(area)
            clusters = [cluster for cluster in clusters if normalize_area(cluster.get("area")) == normalized_area]

        report = build_trend_report(records, clusters)
        md_path, json_path = save_outputs(report, output_dir, area)
        outputs = [
            {
                "title": f"{normalize_area(area) or 'Multi-Area'} Trend Insights",
                "problem": "Repeated themes and rising signals across normalized and clustered records.",
                "problem_signature": f"{normalize_area(area) or 'Multi-Area'} | processing | trends",
                "confidence": "High" if report["counts"]["records"] >= 5 else "Medium",
                "source": str(json_path.resolve()),
                "markdown_path": str(md_path.resolve()),
            }
        ]

        response = create_success_response(
            agent=AGENT_NAME,
            layer=LAYER_NAME,
            mode=mode,
            area=area,
            items_processed=len(records),
            items_saved=len(outputs),
            outputs=outputs,
            warnings=["Trend agent saved processor artifacts under outputs/processed/trends/."],
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        response["metadata"]["trend_summary"] = {
            "records_analyzed": len(records),
            "clusters_considered": len(clusters),
            "summary_markdown": str(md_path.resolve()),
            "trend_json": str(json_path.resolve()),
        }
        return response
    except Exception as exc:
        LOGGER.exception("Trend agent failed during run_agent")
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
    parser = argparse.ArgumentParser(description="Run the trend processing agent.")
    parser.add_argument("--mode", choices=["configured_scan", "manual_url", "manual_text"], default="configured_scan")
    parser.add_argument("--area")
    parser.add_argument("--url")
    parser.add_argument("--text")
    parser.add_argument("--title")
    parser.add_argument("--source")
    parser.add_argument("--outputs-root", default=str(DEFAULT_OUTPUTS_ROOT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)
    payload: dict[str, Any] = {
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
