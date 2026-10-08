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

from core.intelligence import build_problem_clusters
from core.normalization import ensure_normalized_collection_records, save_normalized_collection_records
from core.schemas import create_error_response, create_partial_response, create_success_response, normalize_area

LOGGER = logging.getLogger("rif.clustering_agent")

DEFAULT_OUTPUT_DIR = Path("outputs/processed/clusters")
DEFAULT_OUTPUTS_ROOT = Path("outputs")
AGENT_NAME = "clustering"
LAYER_NAME = "Processing"


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
    records = data if isinstance(data, list) else []
    tagged_path = outputs_root / "processed" / "tags" / "tagged_records.json"
    if tagged_path.exists():
        try:
            tagged = json.loads(tagged_path.read_text(encoding="utf-8"))
            tag_map = {str(item.get("record_id")): item for item in tagged if isinstance(item, dict) and item.get("record_id")}
            for record in records:
                item = tag_map.get(str(record.get("record_id")))
                if item:
                    record.update({k: item[k] for k in ("topic_tags", "signal_type_tags", "problem_signature") if k in item})
        except json.JSONDecodeError:
            pass
    return records


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
        "problem": clean[:320],
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


def build_cluster_markdown(cluster: dict[str, Any]) -> str:
    members = cluster.get("members", [])
    supporting_layers = cluster.get("supporting_layers", [])
    signal_types = cluster.get("signal_types", [])
    evidence_types = cluster.get("evidence_types", [])
    actors = cluster.get("actors", [])
    top_terms = cluster.get("top_terms", [])
    evidence_links = cluster.get("evidence_links", [])
    area = cluster.get("area", "Unscoped")
    representative_problem = cleaned_text(str(cluster.get("representative_problem", ""))) or "Representative problem pending."

    why_important = (
        f"This cluster groups {len(members)} related signals across {len(supporting_layers)} layer(s), "
        f"helping downstream synthesis focus on repeated ecosystem problems rather than isolated artifacts."
    )
    existing_solutions = "Evidence-aware clustering is available, but the grouped problem still needs synthesis and prioritization."
    gap = "The related records existed separately; they were not yet promoted into a reusable clustered problem artifact."
    idea = "Use this cluster as a direct input to the synthesis and trend agents."

    lines = [
        f"# Clustered Signal: {cluster.get('cluster_id', 'cluster')}",
        "",
        "## Problem",
        representative_problem,
        "",
        "## Source",
        "\n".join(f"- {link}" for link in evidence_links) or "No evidence links recorded.",
        "",
        "## Funding Call IDs",
        ", ".join(cluster.get("funding_call_ids", [])) or str(cluster.get("funding_call_id") or "Unknown"),
        "",
        "## Layer",
        "Clustering",
        "",
        "## Research Area",
        area,
        "",
        "## Why Important",
        why_important,
        "",
        "## Existing Solutions",
        existing_solutions,
        "",
        "## Gap",
        gap,
        "",
        "## Idea",
        idea,
        "",
        "## Feasibility",
        "High",
        "",
        "## Supporting Layers",
    ]
    lines.extend(f"- {layer}" for layer in supporting_layers) if supporting_layers else lines.append("- none")
    lines.extend(["", "## Signal Types"])
    lines.extend(f"- {value}" for value in signal_types) if signal_types else lines.append("- none")
    lines.extend(["", "## Evidence Types"])
    lines.extend(f"- {value}" for value in evidence_types) if evidence_types else lines.append("- none")
    lines.extend(["", "## Evidence Independence"])
    lines.append(f"- independent sources: {cluster.get('independent_source_count', 0)}")
    lines.append(f"- independent layers: {cluster.get('independent_layer_count', 0)}")
    lines.append(f"- research evidence layers: {cluster.get('research_evidence_layer_count', 0)}")
    lines.append(f"- independence ratio: {cluster.get('independence_ratio', 0)}")
    lines.append("")
    lines.extend(["", "## Actors"])
    lines.extend(f"- {value}" for value in actors) if actors else lines.append("- none")
    lines.extend(["", "## Top Terms"])
    lines.extend(f"- {value}" for value in top_terms) if top_terms else lines.append("- none")
    lines.extend(["", "## Members"])
    if members:
        for member in members:
            lines.append(
                f"- {member.get('title')} | {member.get('layer')} | {member.get('source_url') or member.get('file_path')}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Tags", f"#Processing #Clustering #{area.replace('-', '')}"])
    return "\n".join(lines) + "\n"


def save_cluster_markdown(cluster: dict[str, Any], output_dir: Path) -> Path:
    area_dir = slugify(cluster.get("area", "unscoped"))
    target_dir = output_dir / area_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{slugify(cluster.get('cluster_id', 'cluster'))}.md"
    path.write_text(build_cluster_markdown(cluster), encoding="utf-8")
    return path


def save_cluster_index(clusters: list[dict[str, Any]], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "clusters.json"
    summary_path = output_dir / "insights.md"
    json_path.write_text(json.dumps(clusters, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    area_counts = Counter(cluster.get("area", "Unscoped") for cluster in clusters)
    layer_counts = Counter(layer for cluster in clusters for layer in cluster.get("supporting_layers", []))
    signal_counts = Counter(signal for cluster in clusters for signal in cluster.get("signal_types", []))
    lines = [
        "# Clustering Insights",
        "",
        "## Problem",
        "Group related normalized RIF records into reusable evidence-aware problem clusters.",
        "",
        "## Source",
        str(json_path.resolve()),
        "",
        "## Layer",
        "Clustering",
        "",
        "## Research Area",
        "Multi-Area",
        "",
        "## Why Important",
        "Clusters compress repeated evidence into tractable synthesis candidates and make cross-layer corroboration easier to see.",
        "",
        "## Existing Solutions",
        "Normalized records and tagging outputs already preserve structured evidence, but cluster artifacts make repeated problems explicit.",
        "",
        "## Gap",
        "Without explicit cluster artifacts, downstream synthesis has to rediscover relationships repeatedly.",
        "",
        "## Idea",
        "Use these clusters as the main bridge between processing outputs and intelligence-agent synthesis.",
        "",
        "## Feasibility",
        "High",
        "",
        "## Area Distribution",
    ]
    for area, count in sorted(area_counts.items()):
        lines.append(f"- {area}: {count}")
    lines.extend(["", "## Supporting Layer Distribution"])
    for layer, count in sorted(layer_counts.items()):
        lines.append(f"- {layer}: {count}")
    lines.extend(["", "## Signal Type Distribution"])
    for signal, count in signal_counts.most_common(12):
        lines.append(f"- {signal}: {count}")
    lines.extend(["", "## Tags", "#Processing #Clustering #Insights"])
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path, json_path


def run_agent(mode: str, area: str | None = None, input_data: dict[str, Any] | None = None, funding_context=None) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    payload = input_data or {}
    outputs_root = Path(payload.get("outputs_root", DEFAULT_OUTPUTS_ROOT))
    output_dir = Path(payload.get("output_dir", DEFAULT_OUTPUT_DIR))
    threshold = float(payload.get("threshold", 0.35))

    LOGGER.info("Starting clustering agent run: mode=%s area=%s threshold=%s", mode, area, threshold)
    try:
        if mode == "configured_scan":
            records = filter_records(load_normalized_records(outputs_root), area=area)
        elif mode == "manual_url":
            url = cleaned_text(str(payload.get("url", "")))
            if not url:
                return create_error_response(
                    agent="clustering",
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
                    agent="clustering",
                    layer=LAYER_NAME,
                    mode=mode,
                    area=area,
                    errors=["manual_text mode requires input_data['text']"],
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                )
            title = cleaned_text(str(payload.get("title", ""))) or "Manual Cluster Input"
            source = cleaned_text(str(payload.get("source", ""))) or "manual://clustering/manual-input"
            records = [build_manual_record(text=text, title=title, source=source, area=area)]
        else:
            return create_error_response(
                agent="clustering",
                layer=LAYER_NAME,
                mode=mode,
                area=area,
                errors=["mode must be one of configured_scan, manual_url, manual_text"],
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        if not records:
            return create_partial_response(
                agent="clustering",
                layer=LAYER_NAME,
                mode=mode,
                area=area,
                items_processed=0,
                items_saved=0,
                outputs=[],
                warnings=["No normalized records matched the clustering request."],
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        # Live batch runs must not make clustering dependent on an unbounded
        # number of local-LLM ambiguity calls. The deterministic hybrid scorer
        # remains the default for pipeline execution; targeted callers/tests
        # may opt into LLM ambiguity resolution explicitly.
        use_llm_resolution = False # bool(payload.get("llm_ambiguity_resolution", True))
        resolver = None
        if use_llm_resolution:
            from core.intelligence import _cluster_llm_resolver
            resolver = _cluster_llm_resolver
        clusters = build_problem_clusters(
            records,
            threshold=threshold,
            llm_resolver=resolver,
        )
        if not clusters:
            return create_partial_response(
                agent="clustering",
                layer=LAYER_NAME,
                mode=mode,
                area=area,
                items_processed=len(records),
                items_saved=0,
                outputs=[],
                warnings=["Records were loaded, but no clusters were formed."],
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        outputs: list[dict[str, Any]] = []
        for cluster in clusters:
            path = save_cluster_markdown(cluster, output_dir)
            outputs.append(
                {
                    "title": cluster.get("cluster_id", "cluster"),
                    "problem": cleaned_text(str(cluster.get("representative_problem", ""))) or "Representative problem pending.",
                    "problem_signature": f"{cluster.get('area', 'Unscoped')} | {','.join(cluster.get('supporting_layers', []))} | cluster",
                    "confidence": "High" if len(cluster.get("supporting_layers", [])) >= 2 else "Medium",
                    "source": str(json.dumps(cluster.get("evidence_links", []))),
                    "markdown_path": str(path.resolve()),
                }
            )

        summary_path, json_path = save_cluster_index(clusters, output_dir)
        outputs.insert(
            0,
            {
                "title": "Clustering Insights",
                "problem": "Evidence-aware problem clusters built from normalized records.",
                "problem_signature": "multi-area | processing | clustering-summary",
                "confidence": "High",
                "source": str(json_path.resolve()),
                "markdown_path": str(summary_path.resolve()),
            },
        )

        response = create_success_response(
            agent="clustering",
            layer=LAYER_NAME,
            mode=mode,
            area=area,
            items_processed=len(records),
            items_saved=len(outputs),
            outputs=outputs,
            warnings=["Clustering agent saved processor artifacts under outputs/processed/clusters/."],
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        response["metadata"]["clustering_summary"] = {
            "cluster_count": len(clusters),
            "summary_markdown": str(summary_path.resolve()),
            "clusters_json": str(json_path.resolve()),
            "threshold": threshold,
        }
        return response
    except Exception as exc:
        LOGGER.exception("Clustering agent failed during run_agent")
        return create_error_response(
            agent="clustering",
            layer=LAYER_NAME,
            mode=mode,
            area=area,
            errors=[str(exc)],
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the clustering processing agent.")
    parser.add_argument("--mode", choices=["configured_scan", "manual_url", "manual_text"], default="configured_scan")
    parser.add_argument("--area")
    parser.add_argument("--url")
    parser.add_argument("--text")
    parser.add_argument("--title")
    parser.add_argument("--source")
    parser.add_argument("--outputs-root", default=str(DEFAULT_OUTPUTS_ROOT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--threshold", type=float, default=0.35)
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)
    payload: dict[str, Any] = {
        "outputs_root": args.outputs_root,
        "output_dir": args.output_dir,
        "threshold": args.threshold,
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
