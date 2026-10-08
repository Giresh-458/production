from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from core.normalization import normalize_intermediate_artifact
from core.schemas import normalize_area

SYNTHESIS_ARTIFACT_VERSION = "v1"
SYNTHESIS_REQUIRED_FIELDS = (
    "title",
    "problem",
    "source_inputs",
    "layer_or_layers",
    "synthesis_scope",
    "research_area",
    "why_important",
    "existing_solutions",
    "gap",
    "idea",
    "feasibility",
    "confidence",
    "tags",
    "provenance",
)

PROVENANCE_REQUIRED_FIELDS = (
    "input_files",
    "source_urls",
    "synthesized_at",
    "synthesis_method",
    "artifact_version",
)
MANIFEST_ARTIFACT_VERSION = "v1"
SYNTHESIS_INPUT_ARTIFACT_VERSION = "v1"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "item"


def validate_synthesis_artifact(payload: Dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field_name in SYNTHESIS_REQUIRED_FIELDS:
        if field_name not in payload:
            errors.append(f"missing required synthesis field: {field_name}")

    if not isinstance(payload.get("source_inputs"), list) or not payload.get("source_inputs"):
        errors.append("source_inputs must be a non-empty list")
    if not isinstance(payload.get("layer_or_layers"), list) or not payload.get("layer_or_layers"):
        errors.append("layer_or_layers must be a non-empty list")
    if str(payload.get("synthesis_scope", "")).strip() not in {"single-layer", "cross-layer", "ecosystem-priority"}:
        errors.append("synthesis_scope must be one of single-layer, cross-layer, ecosystem-priority")
    if not isinstance(payload.get("tags"), list):
        errors.append("tags must be a list")
    if "evidence_inputs" in payload and not isinstance(payload.get("evidence_inputs"), list):
        errors.append("evidence_inputs must be a list")
    if not isinstance(payload.get("provenance"), dict):
        errors.append("provenance must be a dictionary")
    else:
        for field_name in PROVENANCE_REQUIRED_FIELDS:
            if field_name not in payload["provenance"]:
                errors.append(f"provenance must include {field_name}")
        if not isinstance(payload["provenance"].get("input_files"), list):
            errors.append("provenance.input_files must be a list")
        if not isinstance(payload["provenance"].get("source_urls"), list):
            errors.append("provenance.source_urls must be a list")

    confidence = str(payload.get("confidence", "")).strip()
    if confidence not in {"High", "Medium", "Low"}:
        errors.append("confidence must be one of High, Medium, Low")

    return errors


def _extract_markdown_section(text: str, heading: str) -> str:
    pattern = rf"^## {re.escape(heading)}\n(.*?)(?=\n## |\Z)"
    match = re.search(pattern, text, flags=re.MULTILINE | re.DOTALL)
    if not match:
        return ""
    return match.group(1).strip()


def extract_intermediate_source_url(path: Path) -> Optional[str]:
    try:
        normalized = normalize_intermediate_artifact(path)
        candidate = str(normalized.get("source_url", "")).strip()
        if candidate:
            return candidate
    except Exception:
        pass

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    section = _extract_markdown_section(text, "Source")
    candidate = section.splitlines()[0].strip() if section else ""
    return candidate or None


def build_provenance(
    *,
    input_files: Iterable[str | Path],
    synthesis_method: str,
    source_urls: Optional[Iterable[str]] = None,
    notes: Optional[str] = None,
    synthesized_at: Optional[datetime] = None,
    artifact_version: str = SYNTHESIS_ARTIFACT_VERSION,
) -> Dict[str, Any]:
    normalized_input_files = [str(Path(item).resolve()) for item in input_files]
    discovered_urls: list[str] = []
    if source_urls is not None:
        discovered_urls.extend(str(item).strip() for item in source_urls if str(item).strip())
    else:
        for item in normalized_input_files:
            extracted = extract_intermediate_source_url(Path(item))
            if extracted:
                discovered_urls.append(extracted)

    unique_urls = list(dict.fromkeys(discovered_urls))
    payload: Dict[str, Any] = {
        "input_files": normalized_input_files,
        "source_urls": unique_urls,
        "synthesized_at": (synthesized_at or datetime.now(UTC)).isoformat(),
        "synthesis_method": synthesis_method,
        "artifact_version": artifact_version,
    }
    if notes:
        payload["notes"] = notes
    return payload


def _unique_nonempty(values: Iterable[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = str(value).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        unique.append(cleaned)
    return unique


def _truncate(text: str, limit: int = 320) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _record_evidence_inputs(record: Dict[str, Any], *, max_items: int = 4) -> list[Dict[str, str]]:
    candidates: list[tuple[str, str]] = []
    for excerpt in record.get("evidence_snippets", []) or []:
        if str(excerpt).strip():
            candidates.append(("evidence_snippet", str(excerpt).strip()))
    for excerpt in record.get("excerpt_windows", []) or []:
        if str(excerpt).strip():
            candidates.append(("excerpt_window", str(excerpt).strip()))
    if str(record.get("problem_statement", "")).strip():
        candidates.append(("problem_statement", str(record.get("problem_statement", "")).strip()))
    if str(record.get("context_summary", "")).strip():
        candidates.append(("context_summary", str(record.get("context_summary", "")).strip()))

    evidence_inputs: list[Dict[str, str]] = []
    seen_texts: set[str] = set()
    for evidence_type, text in candidates:
        normalized_text = _truncate(text)
        if not normalized_text or normalized_text in seen_texts:
            continue
        seen_texts.add(normalized_text)
        evidence_inputs.append(
            {
                "input_file": str(record.get("file_path", "")).strip(),
                "layer": str(record.get("layer", "")).strip(),
                "research_area": str(record.get("research_area", "")).strip(),
                "actor": str(record.get("actor", "")).strip(),
                "source_url": str(record.get("source_url", "")).strip(),
                "evidence_type": evidence_type,
                "excerpt": normalized_text,
            }
        )
        if len(evidence_inputs) >= max_items:
            break
    return evidence_inputs


def classify_synthesis_scope(
    *,
    layers: Iterable[str],
    source_urls: Iterable[str],
    normalized_records: Iterable[Dict[str, Any]],
) -> str:
    normalized_layers = _unique_nonempty(layers)
    unique_source_urls = _unique_nonempty(source_urls)
    signal_types = {
        str(record.get("layer", "")).strip().lower()
        for record in normalized_records
        if str(record.get("layer", "")).strip()
    }
    if len(normalized_layers) >= 3 or (len(normalized_layers) >= 2 and len(unique_source_urls) >= 3):
        return "ecosystem-priority"
    if len(normalized_layers) >= 2 or len(signal_types) >= 2:
        return "cross-layer"
    return "single-layer"


def assess_priority_readiness(
    *,
    synthesis_scope: str,
    layer_or_layers: Iterable[str],
    source_urls: Iterable[str],
    evidence_inputs: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    layers = _unique_nonempty(layer_or_layers)
    urls = _unique_nonempty(source_urls)
    evidence_count = len([item for item in evidence_inputs if isinstance(item, dict)])

    ready = False
    rationale = "Insufficient multi-source support."
    if synthesis_scope == "ecosystem-priority":
        ready = len(layers) >= 2 and len(urls) >= 2 and evidence_count >= 4
        rationale = "Ecosystem-priority syntheses require multi-layer, multi-source support with several evidence excerpts."
    elif synthesis_scope == "cross-layer":
        ready = len(layers) >= 2 and evidence_count >= 3
        rationale = "Cross-layer syntheses require corroboration from multiple layers and multiple evidence excerpts."
    else:
        ready = len(urls) >= 1 and evidence_count >= 2
        rationale = "Single-layer syntheses can be promoted only when the evidence from that layer is still specific and well-supported."

    return {
        "ready_for_high_priority": ready,
        "required_scope": synthesis_scope,
        "layer_count": len(layers),
        "source_count": len(urls),
        "evidence_input_count": evidence_count,
        "rationale": rationale,
    }


def build_synthesis_input_bundle(
    *,
    input_files: Iterable[str | Path],
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_records: list[Dict[str, Any]] = []
    evidence_inputs: list[Dict[str, str]] = []

    normalized_input_files = [str(Path(item).resolve()) for item in input_files if str(item).strip()]
    for input_file in normalized_input_files:
        path = Path(input_file)
        if not path.exists():
            continue
        record = normalize_intermediate_artifact(path)
        normalized_records.append(record)
        evidence_inputs.extend(_record_evidence_inputs(record))

    research_areas = [normalize_area(record.get("research_area")) or "Unscoped" for record in normalized_records]
    dominant_area = "Unscoped"
    if research_areas:
        dominant_area = max(set(research_areas), key=research_areas.count)

    layers = _unique_nonempty(str(record.get("layer", "")).strip() for record in normalized_records)
    source_urls = _unique_nonempty(str(record.get("source_url", "")).strip() for record in normalized_records)
    synthesis_scope = classify_synthesis_scope(
        layers=layers,
        source_urls=source_urls,
        normalized_records=normalized_records,
    )

    return {
        "bundle_version": SYNTHESIS_INPUT_ARTIFACT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "input_files": normalized_input_files,
        "research_area": dominant_area,
        "layers": layers,
        "synthesis_scope": synthesis_scope,
        "titles": _unique_nonempty(str(record.get("title", "")).strip() for record in normalized_records),
        "actors": _unique_nonempty(str(record.get("actor", "")).strip() for record in normalized_records),
        "source_urls": source_urls,
        "normalized_records": normalized_records,
        "evidence_inputs": evidence_inputs,
        "priority_readiness": assess_priority_readiness(
            synthesis_scope=synthesis_scope,
            layer_or_layers=layers,
            source_urls=source_urls,
            evidence_inputs=evidence_inputs,
        ),
        "notes": notes or "",
    }


def build_synthesis_payload(
    *,
    title: str,
    problem: str,
    source_inputs: Iterable[str],
    layer_or_layers: Iterable[str],
    synthesis_scope: str,
    research_area: Optional[str],
    why_important: str,
    existing_solutions: str,
    gap: str,
    idea: str,
    feasibility: str,
    confidence: str,
    tags: Iterable[str],
    provenance: Dict[str, Any],
    evidence_inputs: Optional[Iterable[Dict[str, Any]]] = None,
    funding_call_id: Optional[str] = None,
    funding_call_ids: Optional[Iterable[str]] = None,
    supporting_record_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    normalized_tags = [tag if str(tag).startswith("#") else f"#{tag}" for tag in tags]
    normalized_area = normalize_area(research_area) or "Unscoped"
    evidence_payload = [dict(item) for item in (evidence_inputs or []) if isinstance(item, dict)]
    normalized_layers = [str(item).strip() for item in layer_or_layers if str(item).strip()]
    normalized_scope = synthesis_scope.strip() or classify_synthesis_scope(
        layers=normalized_layers,
        source_urls=provenance.get("source_urls", []),
        normalized_records=[],
    )
    priority_readiness = assess_priority_readiness(
        synthesis_scope=normalized_scope,
        layer_or_layers=normalized_layers,
        source_urls=provenance.get("source_urls", []),
        evidence_inputs=evidence_payload,
    )
    return {
        "title": title.strip(),
        "problem": problem.strip(),
        "funding_call_id": funding_call_id,
        "funding_call_ids": list(funding_call_ids) if funding_call_ids else [],
        "supporting_record_ids": list(supporting_record_ids) if supporting_record_ids else [],
        "source_inputs": [str(item).strip() for item in source_inputs if str(item).strip()],
        "layer_or_layers": normalized_layers,
        "synthesis_scope": normalized_scope,
        "research_area": normalized_area,
        "why_important": why_important.strip(),
        "existing_solutions": existing_solutions.strip(),
        "gap": gap.strip(),
        "idea": idea.strip(),
        "feasibility": feasibility.strip(),
        "confidence": confidence.strip(),
        "tags": normalized_tags,
        "provenance": provenance,
        "evidence_inputs": evidence_payload,
        "priority_readiness": priority_readiness,
    }


def build_synthesis_manifest_entry(
    *,
    payload: Dict[str, Any],
    markdown_path: Path,
) -> Dict[str, Any]:
    provenance = payload["provenance"]
    entry = {
        "title": payload["title"],
        "synthesized_file": str(markdown_path.resolve()),
        "area": payload["research_area"],
        "layers_covered": payload["layer_or_layers"],
        "synthesis_scope": payload["synthesis_scope"],
        "synthesis_date": provenance["synthesized_at"],
        "input_files": provenance["input_files"],
        "source_urls": provenance["source_urls"],
        "synthesis_method": provenance["synthesis_method"],
        "artifact_version": provenance["artifact_version"],
        "evidence_input_count": len(payload.get("evidence_inputs", []) or []),
        "priority_readiness": payload.get("priority_readiness", {}),
        "evidence_assessment": payload.get("evidence_assessment", {}),
        "status": "synthesized",
    }
    cluster_id = payload.get("cluster_id") or provenance.get("cluster_id")
    cluster_signature = payload.get("cluster_signature") or provenance.get("cluster_signature")
    if cluster_id:
        entry["cluster_id"] = str(cluster_id)
    if cluster_signature:
        entry["cluster_signature"] = str(cluster_signature)
    entry["funding_call_id"] = payload.get("funding_call_id")
    entry["funding_call_ids"] = payload.get("funding_call_ids", [])
    return entry


def save_synthesis_manifest_entry(
    *,
    entry: Dict[str, Any],
    manifest_dir: Path,
) -> Path:
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"{_slugify(entry['title'])}.json"
    manifest_path.write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest_path


def update_synthesis_manifest_index(
    *,
    entry: Dict[str, Any],
    manifest_dir: Path,
) -> Path:
    manifest_dir.mkdir(parents=True, exist_ok=True)
    index_path = manifest_dir / "index.json"
    entries: list[Dict[str, Any]] = []
    if index_path.exists():
        try:
            existing = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                entries = [
                    item
                    for item in existing
                    if isinstance(item, dict)
                    and (not item.get("synthesized_file") or Path(str(item.get("synthesized_file"))).exists())
                ]
        except (OSError, json.JSONDecodeError):
            entries = []

    entries = [item for item in entries if item.get("synthesized_file") != entry["synthesized_file"]]
    entries.append(entry)
    entries.sort(key=lambda item: (item.get("area", ""), item.get("title", "")))
    index_path.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return index_path


def save_synthesis_markdown(
    *,
    payload: Dict[str, Any],
    output_dir: Path,
    manifest_dir: Optional[Path] = None,
) -> Path:
    validation_errors = validate_synthesis_artifact(payload)
    if validation_errors:
        raise ValueError("; ".join(validation_errors))

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{_slugify(payload['title'])}.md"
    if output_path.exists():
        timestamp_suffix = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        output_path = output_dir / f"{_slugify(payload['title'])}-{timestamp_suffix}.md"

    provenance = payload["provenance"]
    lines = [
        f"# Synthesis: {payload['title']}",
        "",
        "## Problem",
        payload["problem"],
        "",
        "## Source Inputs",
    ]
    lines.extend(f"- {item}" for item in payload["source_inputs"])
    
    lines.extend([
        "",
        "## Funding Call IDs",
        ", ".join(payload.get("funding_call_ids", [])) or str(payload.get("funding_call_id") or "Unknown"),
        "",
    ])
    
    evidence_inputs = payload.get("evidence_inputs") or []
    if evidence_inputs:
        lines.extend(["", "## Evidence Inputs"])
        for item in evidence_inputs:
            excerpt = str(item.get("excerpt", "")).strip()
            if not excerpt:
                continue
            label_parts = [
                str(item.get("layer", "")).strip(),
                str(item.get("actor", "")).strip(),
                str(item.get("evidence_type", "")).strip(),
            ]
            label = " | ".join(part for part in label_parts if part)
            if label:
                lines.append(f"- {label}: {excerpt}")
            else:
                lines.append(f"- {excerpt}")
    lines.extend(
        [
            "",
            "## Layer or Layers",
            ", ".join(payload["layer_or_layers"]),
            "",
            "## Synthesis Scope",
            payload["synthesis_scope"],
            "",
            "## Research Area",
            payload["research_area"],
            "",
            "## Why Important",
            payload["why_important"],
            "",
            "## Existing Solutions",
            payload["existing_solutions"],
            "",
            "## Gap",
            payload["gap"],
            "",
            "## Idea",
            payload["idea"],
            "",
            "## Feasibility",
            payload["feasibility"],
            "",
            "## Confidence",
            payload["confidence"],
            "",
            "## Tags",
            " ".join(payload["tags"]),
            "",
            "## Priority Readiness",
            f"- ready_for_high_priority: {payload.get('priority_readiness', {}).get('ready_for_high_priority', False)}",
            f"- rationale: {payload.get('priority_readiness', {}).get('rationale', '')}",
            f"- layer_count: {payload.get('priority_readiness', {}).get('layer_count', 0)}",
            f"- source_count: {payload.get('priority_readiness', {}).get('source_count', 0)}",
            f"- evidence_input_count: {payload.get('priority_readiness', {}).get('evidence_input_count', 0)}",
            "",
            "## Provenance",
            f"- synthesized_at: {provenance.get('synthesized_at', datetime.now(UTC).isoformat())}",
            f"- synthesis_method: {provenance.get('synthesis_method', 'manual_chat')}",
            f"- artifact_version: {provenance.get('artifact_version', SYNTHESIS_ARTIFACT_VERSION)}",
        ]
    )

    assessment = payload.get("evidence_assessment", {})
    if assessment:
        lines.extend([
            "",
            "## Evidence Assessment",
            f"- independent sources: {assessment.get('independent_sources', 0)}",
            f"- independent layers: {assessment.get('independent_layers', 0)}",
            f"- research evidence layers: {assessment.get('research_evidence_layers', 0)}",
            f"- independence ratio: {assessment.get('independence_ratio', 0)}",
            f"- contradiction status: {assessment.get('contradiction_status', 'unknown')}",
        ])
        claims = assessment.get("claims", [])
        if claims:
            lines.extend(["", "## Claim Ledger"])
            for claim in claims:
                lines.append(f"- {claim.get('claim')} | support={claim.get('support', 0)} | confidence={claim.get('confidence', 'Low')}")

    input_files = provenance.get("input_files", [])
    if input_files:
        lines.append("- input_files:")
        lines.extend(f"  - {item}" for item in input_files)

    source_urls = provenance.get("source_urls", [])
    if source_urls:
        lines.append("- source_urls:")
        lines.extend(f"  - {item}" for item in source_urls)

    notes = provenance.get("notes")
    if notes:
        lines.append(f"- notes: {notes}")

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    resolved_manifest_dir = manifest_dir or output_dir.parent / "manifest"
    manifest_entry = build_synthesis_manifest_entry(payload=payload, markdown_path=output_path)
    save_synthesis_manifest_entry(entry=manifest_entry, manifest_dir=resolved_manifest_dir)
    update_synthesis_manifest_index(entry=manifest_entry, manifest_dir=resolved_manifest_dir)
    from core.intelligence import refresh_intelligence_views

    refresh_intelligence_views(output_dir.parent.parent)
    return output_path
