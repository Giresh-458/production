from __future__ import annotations

import re
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from core.collection_schemas import get_collection_schema, ordered_agent_body_items, validate_agent_specific_body
from core.evidence import build_evidence_bundle
from core.quality import evaluate_collection_quality
from core.recursive_collection import update_recursive_review
from core.schemas import ALLOWED_MODES, ALLOWED_RESEARCH_AREAS, normalize_area, resolve_research_area, validate_response_structure
from core.schemas import create_partial_response, create_success_response

INTERMEDIATE_ARTIFACT_VERSION = "v1"
INTERMEDIATE_REQUIRED_FIELDS = (
    "raw_content",
    "evidence_bundle",
    "metadata",
    "provenance",
    "shared_header",
    "agent_specific_body",
)


from core.funding_selection import FundingCallContext

class BaseAgent(ABC):
    name: str
    layer: str

    @abstractmethod
    def run_agent(
        self,
        mode: str,
        area: Optional[str] = None,
        input_data: Optional[Dict[str, Any]] = None,
        funding_context: Optional[FundingCallContext] = None,
        funding_contexts: Optional[list[FundingCallContext]] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError

def validate_run_input(mode_or_agent: str, input_or_mode: Any = None, input_data: Optional[Dict[str, Any]] = None, funding_context: Optional[FundingCallContext] = None, funding_contexts: Optional[list[FundingCallContext]] = None) -> list[str]:
    # Backward compatibility: older agents call validate_run_input(mode, payload)
    if isinstance(input_or_mode, dict) or input_or_mode is None:
        mode = mode_or_agent
        payload = input_or_mode or {}
        agent_name = "unknown"
    else:
        agent_name = mode_or_agent
        mode = input_or_mode
        payload = input_data or {}

    errors: list[str] = []

    if mode not in ALLOWED_MODES:
        errors.append("mode must be one of configured_scan, manual_url, manual_text")
    if mode == "manual_url" and not payload.get("url"):
        errors.append("manual_url mode requires input_data['url']")
    if mode == "manual_text" and not payload.get("text"):
        errors.append("manual_text mode requires input_data['text']")

    from core.agent_registry import COLLECTION_AGENTS
    if mode == "configured_scan" and agent_name in COLLECTION_AGENTS and funding_context is None and not funding_contexts:
        if agent_name != "funding":
            errors.append("MISSING_FUNDING_CONTEXT")

    return errors


def validate_common_output(response: Dict[str, Any]) -> list[str]:
    return validate_response_structure(response)


def snapshot_markdown_files(output_dir: Path) -> Dict[str, float]:
    if not output_dir.exists():
        return {}
    snapshot: Dict[str, float] = {}
    for path in output_dir.rglob("*.md"):
        try:
            snapshot[str(path.resolve())] = path.stat().st_mtime
        except OSError:
            continue
    return snapshot


def infer_title_from_url(url: str, fallback: str) -> str:
    parsed = urlparse(url)
    tail = parsed.path.rstrip("/").split("/")[-1] if parsed.path else ""
    slug = tail or parsed.netloc or fallback
    cleaned = re.sub(r"[-_]+", " ", slug).strip()
    return cleaned.title() if cleaned else fallback


def infer_name_from_url(url: str, fallback: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc:
        host = parsed.netloc.split(":")[0]
        name = host.replace("www.", "").split(".")[0]
        if name:
            return re.sub(r"[-_]+", " ", name).title()
    return fallback


def default_intermediate_output_dir(agent: str) -> Path:
    return Path("outputs/intermediate") / agent


def validate_intermediate_artifact(payload: Dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field_name in INTERMEDIATE_REQUIRED_FIELDS:
        if field_name not in payload:
            errors.append(f"missing required intermediate field: {field_name}")

    if not isinstance(payload.get("metadata"), dict):
        errors.append("metadata must be a dictionary")
    if not isinstance(payload.get("evidence_bundle"), dict):
        errors.append("evidence_bundle must be a dictionary")
    if not isinstance(payload.get("provenance"), dict):
        errors.append("provenance must be a dictionary")
    if not isinstance(payload.get("shared_header"), dict):
        errors.append("shared_header must be a dictionary")
    if not isinstance(payload.get("agent_specific_body"), dict):
        errors.append("agent_specific_body must be a dictionary")

    raw_content = payload.get("raw_content")
    if not isinstance(raw_content, str) or not raw_content.strip():
        errors.append("raw_content must be a non-empty string")

    shared_header = payload.get("shared_header", {})
    if shared_header.get("collection_mode") not in ALLOWED_MODES:
        errors.append("collection_mode must be one of configured_scan, manual_url, manual_text")

    research_area = str(shared_header.get("research_area", "")).strip()
    if research_area != "Unscoped" and research_area not in ALLOWED_RESEARCH_AREAS:
        errors.append("research_area must be one of the canonical research areas or Unscoped")

    agent_name = str(shared_header.get("agent_name", "")).strip()
    body = payload.get("agent_specific_body", {})
    if agent_name and isinstance(body, dict):
        errors.extend(validate_agent_specific_body(agent_name, body))

    evidence_bundle = payload.get("evidence_bundle", {})
    if isinstance(evidence_bundle, dict):
        for field_name in (
            "page_title",
            "section_headings",
            "key_paragraphs",
            "bullet_lists",
            "quoted_passages",
            "links_found",
            "named_entities_or_programs",
            "evidence_snippets",
            "excerpt_windows",
        ):
            if field_name not in evidence_bundle:
                errors.append(f"evidence_bundle missing required field: {field_name}")

    return errors


def _slugify_filename(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "item"


def _document_value(document: Any, field_name: str, default: Any = "") -> Any:
    if isinstance(document, dict):
        return document.get(field_name, default)
    return getattr(document, field_name, default)


def _document_title(document: Any) -> str:
    for field_name in (
        "title",
        "challenge_name",
        "program_name",
        "company_name",
        "lab_name",
        "incident_name",
        "source_name",
        "project_name",
        "name",
        "organization",
    ):
        value = str(_document_value(document, field_name, "")).strip()
        if value:
            return value
    return "Collected Signal"


def _document_source(document: Any, default_source: Optional[str] = None) -> str:
    for field_name in ("url", "source", "repository_or_source", "data_source"):
        value = str(_document_value(document, field_name, "")).strip()
        if value:
            return value
    return default_source or "Manual"


def _document_area(document: Any, fallback_area: Optional[str]) -> Optional[str]:
    for field_name in ("area", "area_hint", "focus_area", "research_area"):
        value = _document_value(document, field_name, None)
        normalized = normalize_area(value)
        if normalized:
            return normalized
    return normalize_area(fallback_area)


def _document_content(document: Any) -> str:
    for field_name in ("content", "summary", "abstract", "text"):
        value = str(_document_value(document, field_name, "")).strip()
        if value:
            return value

    parts: list[str] = []
    if isinstance(document, dict):
        items = document.items()
    elif is_dataclass(document):
        items = asdict(document).items()
    else:
        items = vars(document).items() if hasattr(document, "__dict__") else ()
    for key, value in items:
        if key in {"title", "challenge_name", "program_name", "company_name", "lab_name", "name", "url", "source"}:
            continue
        if value is None:
            continue
        rendered = str(value).strip()
        if rendered:
            parts.append(f"{key}: {rendered}")
    return "\n".join(parts)


from core.problem_extraction import extract_problem_intelligence

def _document_metadata(document: Any) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    if isinstance(document, dict):
        items = document.items()
    elif is_dataclass(document):
        items = asdict(document).items()
    elif hasattr(document, "__dict__"):
        items = vars(document).items()
    else:
        return metadata

    for key, value in items:
        if key == "content" or value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            metadata[key] = value
        elif isinstance(value, list):
            metadata[key] = [str(item) for item in value]
        else:
            metadata[key] = str(value)
    return metadata


def _format_metadata_lines(metadata: Dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key in sorted(metadata):
        value = metadata[key]
        if isinstance(value, list):
            rendered = ", ".join(str(item) for item in value)
        else:
            rendered = str(value)
        lines.append(f"- {key}: {rendered}")
    return lines


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _format_list_block(values: list[Any], *, empty_fallback: str = "- none") -> list[str]:
    if not values:
        return [empty_fallback]
    return [f"- {_format_scalar(value)}" for value in values]


def _serialize_agent_body_value(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(f"- {_format_scalar(item)}" for item in value) if value else "- none"
    if isinstance(value, dict):
        return "\n".join(f"- {key}: {_format_scalar(item)}" for key, item in sorted(value.items())) if value else "- none"
    return _format_scalar(value)


def _body_section_title(field_name: str) -> str:
    return field_name.replace("_", " ").strip().title()


def _document_agent_specific_body(document: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    if isinstance(document, dict):
        items = document.items()
    elif is_dataclass(document):
        items = asdict(document).items()
    elif hasattr(document, "__dict__"):
        items = vars(document).items()
    else:
        return body

    excluded = {
        "content",
        "summary",
        "abstract",
        "text",
        "title",
        "challenge_name",
        "program_name",
        "company_name",
        "lab_name",
        "incident_name",
        "source_name",
        "project_name",
        "name",
        "url",
        "source",
        "source_mode",
        "area",
        "area_hint",
        "focus_area",
        "research_area",
    }
    for key, value in items:
        if key in excluded or value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        body[key] = value
    return body



def assign_funding_calls(title: str, content: str, funding_contexts: list[FundingCallContext]) -> list[str]:
    """Assigns a document to specific funding calls using strict provenance rules."""
    import re
    matched_ids = []
    raw = (str(content) + " " + str(title)).lower()

    for ctx in funding_contexts:
        # 1. Explicit ID
        if ctx.funding_call_id and re.search(rf"\b{re.escape(ctx.funding_call_id.lower())}\b", raw):
            matched_ids.append(ctx.funding_call_id)
            continue

        # 2. Exact URL
        if ctx.application_url and ctx.application_url.lower() in raw:
            matched_ids.append(ctx.funding_call_id)
            continue

        # 3. Exact Program / Call Identifier
        if ctx.call_id and len(ctx.call_id) > 4 and re.search(rf"\b{re.escape(ctx.call_id.lower())}\b", raw):
            matched_ids.append(ctx.funding_call_id)
            continue

        # 4. Strong textual match (e.g. exact title or program name if long enough)
        if ctx.call_title and len(ctx.call_title) > 15 and ctx.call_title.lower() in raw:
            matched_ids.append(ctx.funding_call_id)
            continue
        if ctx.program_name and len(ctx.program_name) > 15 and ctx.program_name.lower() in raw:
            matched_ids.append(ctx.funding_call_id)
            continue

    return matched_ids

def build_intermediate_artifact_payload(

    *,
    agent: str,
    layer: str,
    mode: str,
    area: Optional[str],
    title: str,
    source: str,
    content: str,
    metadata: Optional[Dict[str, Any]] = None,
    collected_at: Optional[datetime] = None,
    agent_version: str = INTERMEDIATE_ARTIFACT_VERSION,
    allow_llm_refinement: bool = False,
) -> tuple[Dict[str, Any], list[str]]:
    normalized_area, area_warnings, area_scores = resolve_research_area(
        explicit_area=area,
        title=title,
        raw_content=content,
        authoritative_explicit=bool(area),
    )
    machine_metadata = {
        "area_normalization": {
            "selected_area": normalized_area or "Unscoped",
            "keyword_scores": area_scores,
            "input_area_hint": normalize_area(area) or "None",
        },
        "artifact_schema": "shared-metadata-header-plus-agent-body-v1",
    }
    shared_header = {
        "title": title.strip() or "Collected Signal",
        "source": source.strip() or "Manual",
        "layer": layer,
        "research_area": normalized_area or "Unscoped",
        "collected_at": (collected_at or datetime.now(UTC)).isoformat(),
        "agent_name": agent,
        "agent_version": agent_version,
        "collection_mode": mode,
    }
    evidence_bundle = build_evidence_bundle(
        raw_content=content.strip(),
        title=shared_header["title"],
        research_area=shared_header["research_area"],
        metadata=metadata or {},
    )
    provenance = {
        "source_url": source.strip() or "Manual",
        "collected_at": shared_header["collected_at"],
        "collection_mode": mode,
        "agent_name": agent,
        "agent_version": agent_version,
    }
    if metadata:
        machine_metadata["source_hints"] = metadata
    schema = get_collection_schema(agent)
    if schema is not None:
        machine_metadata["agent_collection_schema"] = {
            "schema_name": schema.schema_name,
            "version": schema.version,
            "required_fields": [field.name for field in schema.fields if field.required],
        }
    quality_gate = evaluate_collection_quality(
        agent_name=agent,
        title=shared_header["title"],
        source=shared_header["source"],
        research_area=shared_header["research_area"],
        raw_content=content.strip(),
        evidence_bundle=evidence_bundle,
        area_scores=area_scores,
    )
    import hashlib
    content_hash = hashlib.sha256(content.strip().encode("utf-8")).hexdigest()
    doc_id_seed = f"{source.strip()}|{title.strip()}"
    document_identity = hashlib.sha256(doc_id_seed.encode("utf-8")).hexdigest()

    provenance["document_identity"] = document_identity
    provenance["content_hash"] = content_hash

    problem_intelligence = extract_problem_intelligence(
        content.strip(),
        shared_header["title"],
        evidence_bundle,
        allow_llm_refinement=allow_llm_refinement,
        research_area=shared_header["research_area"],
        agent_name=agent,
    )

    machine_metadata["quality_gate"] = quality_gate
    return {
        "shared_header": shared_header,
        "evidence_bundle": evidence_bundle,
        "metadata": machine_metadata,
        "provenance": provenance,
        "problem_intelligence": problem_intelligence,
        "agent_specific_body": {},
        "raw_content": content.strip(),
    }, area_warnings


def save_intermediate_markdown(
    *,
    payload: Dict[str, Any],
    output_dir: Path,
) -> Path:
    validation_errors = validate_intermediate_artifact(payload)
    if validation_errors:
        raise ValueError("; ".join(validation_errors))

    shared_header = payload["shared_header"]
    resolved_area = normalize_area(str(shared_header["research_area"]))
    area_dir = _slugify_filename(resolved_area or "unscoped")
    target_dir = output_dir / area_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    base_name = _slugify_filename(str(shared_header['title']))

    import hashlib
    import json

    source_url = str(shared_header.get('source', '')).strip()

    raw_content = payload.get("raw_content")
    if raw_content:
        actual_content_hash = hashlib.sha256(str(raw_content).encode("utf-8")).hexdigest()[:16]
    else:
        # Fallback: hash the semantic content fields while excluding volatile metadata
        # like timestamps ('collected_at'), generated metadata ('quality_gate'), and
        # 'problem_intelligence' which might be downstream derivations.
        # This keeps the document identity stable across repeat runs.
        semantic_payload = {
            "title": shared_header.get("title"),
            "source": shared_header.get("source"),
            "agent_name": shared_header.get("agent_name"),
            "research_area": shared_header.get("research_area"),
            "evidence_bundle": payload.get("evidence_bundle", {}),
            "agent_specific_body": payload.get("agent_specific_body", {})
        }
        actual_content_hash = hashlib.sha256(json.dumps(semantic_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]

    if source_url:
        url_hash = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:16]
        output_path = target_dir / f"{base_name}-{url_hash}-{actual_content_hash}.md"
    else:
        output_path = target_dir / f"{base_name}-{actual_content_hash}.md"

    problem_intelligence = payload.get("problem_intelligence", {})
    preview = problem_intelligence.get("selected_problem") or "No explicit problem statement found."
    intelligence_mode = problem_intelligence.get("intelligence_mode", "deterministic")
    tags = ["#Intermediate", f"#{str(shared_header['agent_name']).capitalize()}"]
    if resolved_area:
        tags.append(f"#{resolved_area.replace('-', '')}")

    lines = [
        f"# Intermediate Signal: {shared_header['title']}",
        "",
        "## Shared Metadata",
    ]
    lines.extend(_format_metadata_lines(shared_header))
    lines.extend(
        [
        "",
        "## Provenance",
    ]
    )
    lines.extend(_format_metadata_lines(payload["provenance"]))
    lines.extend(
        [
        "",
        "## Evidence Bundle",
        "### Page Title",
        str(payload["evidence_bundle"].get("page_title") or shared_header["title"]),
        "### Section Headings",
        "",
        ]
    )
    lines.extend(_format_list_block(list(payload["evidence_bundle"].get("section_headings", []))))
    for label, key in (("Key Paragraphs", "key_paragraphs"), ("Bullet Lists", "bullet_lists"), ("Quoted Passages", "quoted_passages"), ("Links Found", "links_found"), ("Named Entities Or Programs", "named_entities_or_programs"), ("Evidence Snippets", "evidence_snippets"), ("Excerpt Windows", "excerpt_windows")):
        lines.extend(["", f"### {label}"])
        lines.extend(_format_list_block(list(payload["evidence_bundle"].get(key, []))))
    lines.extend(["", "## Problem Intelligence",
        f"**Selected Problem**: {preview}",
        f"**Original Candidate**: {problem_intelligence.get('original_candidate', '')}",
        f"**Mode**: {intelligence_mode}",
        f"**Confidence**: {problem_intelligence.get('problem_confidence', 'Unknown')}",
        "",
        "### Problem Candidates"
        ]
    )

    for c in problem_intelligence.get("problem_candidates", []):
        lines.extend([
            f"- **Candidate**: {c['problem']}",
            f"  - Type: {c['problem_type']}",
            f"  - Scores: Specificity {c['specificity_score']}, Researchability {c['researchability_score']}, Boilerplate {c['boilerplate_score']}",
        ])
    lines.extend(["", "## Agent-Specific Body"])

    body = payload["agent_specific_body"]
    if body:
        for field_name, label, value in ordered_agent_body_items(str(shared_header["agent_name"]), body):
            lines.extend(
                [
                    f"### {label or _body_section_title(field_name)}",
                    _serialize_agent_body_value(value),
                    "",
                ]
            )
    else:
        lines.extend(["No agent-specific fields were preserved.", ""])

    lines.extend(
        [
        "",
        "## Tags",
        " ".join(tags),
        "",
        "## Machine Metadata",
    ]
    )

    metadata_lines = _format_metadata_lines(payload["metadata"])
    if metadata_lines:
        lines.extend(metadata_lines)
    else:
        lines.append("- none")

    lines.extend(
        [
            "",
            "## Machine-Readable Payload",
            "```json",
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            "```",
            "",
            "## Raw Content",
            str(payload["raw_content"]).strip() or "No content captured.",
            "",
        ]
    )
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def _estimate_recursive_expansion_count(metadata: Dict[str, Any]) -> int:
    for key in ("recursive_page_count", "expanded_page_count", "child_page_count"):
        value = metadata.get(key)
        if isinstance(value, int) and value > 0:
            return max(value - 1, 0)
    pages = metadata.get("pages")
    if isinstance(pages, list) and pages:
        return max(len(pages) - 1, 0)
    return 0


def _recursive_review_id(metadata: Dict[str, Any]) -> str:
    value = metadata.get("recursive_review_id")
    return str(value).strip() if value is not None else ""



def _rescue_required_agent_body(agent: str, document: Any, body: Dict[str, Any], area: Optional[str], source: str) -> Dict[str, Any]:
    """Populate only schema-required fields when a live configured source was fetched.

    Collection should preserve source evidence even when a typed agent object is sparse.
    These are explicit provenance/context placeholders, not fabricated analytical claims.
    """
    schema = get_collection_schema(agent)
    if schema is None:
        return body
    result = dict(body)
    value_map = {}
    try:
        raw = asdict(document) if is_dataclass(document) else vars(document)
    except Exception:
        raw = {}
    for name, value in (raw.items() if isinstance(raw, dict) else []):
        if value is not None and not (isinstance(value, str) and not value.strip()):
            value_map[name] = value

    defaults = {
        "source_type": value_map.get("source_type") or value_map.get("type") or "web_page",
        "focus": value_map.get("focus") or area or "configured research domain",
        "evidence_type": value_map.get("evidence_type") or "web_source",
        "issuing_body": value_map.get("issuing_body") or value_map.get("organization") or value_map.get("name") or "Source organization",
        "investor_or_organization": value_map.get("investor_or_organization") or value_map.get("organization") or value_map.get("name") or "Source organization",
        "sponsor_or_organizer": value_map.get("sponsor_or_organizer") or value_map.get("organization") or value_map.get("name") or "Source organization",
        "practitioner_role_hint": value_map.get("practitioner_role_hint") or "External practitioner / industry source",
        "expert_name_hint": value_map.get("expert_name_hint") or value_map.get("name") or "Named expert not extracted",
        "affiliation_hint": value_map.get("affiliation_hint") or value_map.get("organization") or value_map.get("name") or "Source affiliation",
        "role_hint": value_map.get("role_hint") or "Research / technical contributor",
    }
    for field in schema.fields:
        if not field.required or field.name in result:
            continue
        result[field.name] = defaults.get(field.name, area or "Preserved live source evidence")
    return result

def finalize_collection_agent_response(
    *,
    agent: str,
    layer: str,
    mode: str,
    area: Optional[str],
    documents: list[Any],
    output_dir: Path,
    started_at: datetime,
    warnings: Optional[list[str]] = None,
    default_source: Optional[str] = None,
    funding_context: Optional[FundingCallContext] = None,
    funding_contexts: Optional[list[FundingCallContext]] = None,
    run_id: Optional[str] = None,
    allow_llm_refinement: bool = False,
) -> Dict[str, Any]:
    outputs: list[Dict[str, Any]] = []
    collected_warnings = list(warnings or [])
    deduped_documents: Dict[tuple[str, str], tuple[Dict[str, Any], str]] = {}
    scanned_pages = 0
    rejected_pages = 0
    recursively_expanded_pages = 0

    for document in documents:
        scanned_pages += 1
        title = _document_title(document)
        source = _document_source(document, default_source=default_source)
        content = _document_content(document)
        resolved_area = _document_area(document, area)
        metadata = _document_metadata(document)
        recursively_expanded_pages += _estimate_recursive_expansion_count(metadata)
        agent_specific_body = _document_agent_specific_body(document)
        artifact_payload, area_warnings = build_intermediate_artifact_payload(
            agent=agent,
            layer=layer,
            mode=mode,
            area=resolved_area,
            title=title,
            source=source,
            content=content,
            metadata=metadata,
            collected_at=started_at,
            allow_llm_refinement=allow_llm_refinement,
        )

        if funding_contexts:
            doc_ids = getattr(document, "funding_call_ids", None)
            if doc_ids is None and isinstance(document, dict):
                doc_ids = document.get("funding_call_ids")

            if not doc_ids:
                # Single call explicitly passed to collector overrides everything
                if funding_context:
                    doc_ids = [funding_context.funding_call_id]
                else:
                    # Multi-call: strict provenance requirements
                    matched_ids = []
                    raw = (str(content) + " " + str(title)).lower()

                    doc_ids = assign_funding_calls(title, content, funding_contexts)

                artifact_payload["shared_header"]["funding_call_ids"] = doc_ids

        elif funding_context:
            artifact_payload["shared_header"]["funding_call_id"] = funding_context.funding_call_id
            artifact_payload["shared_header"]["funding_call_ids"] = [funding_context.funding_call_id]

        if run_id:
            artifact_payload["shared_header"]["run_id"] = run_id

        artifact_payload["agent_specific_body"] = agent_specific_body
        collected_warnings.extend(area_warnings)
        quality_gate = artifact_payload["metadata"].get("quality_gate", {})
        # Configured live scans are source collection, not final publication.
        # Preserve useful fetched evidence even when a page is structurally thin
        # or does not contain a literal "problem" phrase. The downstream stages
        # can weight the quality metadata rather than losing the evidence entirely.
        live_source_rescue = (
            mode == "configured_scan"
            and bool(source.strip().startswith(("http://", "https://")))
            and len(content.strip()) >= 180
            and agent != "adaptive_research"
        )
        if live_source_rescue and not quality_gate.get("passed", False):
            agent_specific_body = _rescue_required_agent_body(agent, document, agent_specific_body, resolved_area, source)
            artifact_payload["agent_specific_body"] = agent_specific_body

        if not quality_gate.get("passed", False) and not live_source_rescue:
            rejected_pages += 1
            review_id = _recursive_review_id(metadata)
            if review_id:
                update_recursive_review(
                    review_id,
                    outcome="quality_rejected",
                    outcome_reason=quality_gate.get("rejection_reason") or "failed shared collection quality checks",
                    artifact_title=artifact_payload["shared_header"]["title"],
                    artifact_source=artifact_payload["shared_header"]["source"],
                    selected_area=artifact_payload["shared_header"]["research_area"],
                )
            collected_warnings.append(
                f"Skipped '{artifact_payload['shared_header']['title']}' due to quality gate: "
                f"{quality_gate.get('rejection_reason') or 'failed shared collection quality checks'}."
            )
            continue
        if live_source_rescue and not quality_gate.get("passed", False):
            quality_gate["accepted_with_warnings"] = True
            quality_gate["acceptance_reason"] = "configured live source preserved for evidence despite a soft collection-quality gate"
            artifact_payload["metadata"]["quality_gate"] = quality_gate
            collected_warnings.append(
                f"Preserved '{artifact_payload['shared_header']['title']}' as live evidence despite soft quality gate: "
                f"{quality_gate.get('rejection_reason') or 'limited structure/signal'}."
            )

        dedupe_key = (
            artifact_payload["shared_header"]["title"].lower(),
            artifact_payload["shared_header"]["source"].lower(),
        )
        score_map = artifact_payload["metadata"].get("area_normalization", {}).get("keyword_scores", {})
        selected_area = artifact_payload["shared_header"]["research_area"]
        selected_score = score_map.get(selected_area, 0) if isinstance(score_map, dict) else 0
        existing = deduped_documents.get(dedupe_key)
        if existing:
            existing_payload, _ = existing
            existing_scores = existing_payload["metadata"].get("area_normalization", {}).get("keyword_scores", {})
            existing_area = existing_payload["shared_header"]["research_area"]
            existing_score = existing_scores.get(existing_area, 0) if isinstance(existing_scores, dict) else 0
            if selected_score > existing_score:
                collected_warnings.append(
                    f"Replaced weaker area assignment '{existing_area}' with '{selected_area}' for '{artifact_payload['shared_header']['title']}'."
                )
                deduped_documents[dedupe_key] = (artifact_payload, content)
            else:
                collected_warnings.append(
                    f"Skipped duplicate intermediate artifact for '{artifact_payload['shared_header']['title']}' with weaker or equal area evidence."
                )
            continue
        deduped_documents[dedupe_key] = (artifact_payload, content)

    for artifact_payload, content in deduped_documents.values():
        output_path = save_intermediate_markdown(payload=artifact_payload, output_dir=output_dir)
        review_id = _recursive_review_id(artifact_payload["metadata"].get("source_hints", {}))
        if review_id:
            update_recursive_review(
                review_id,
                outcome="saved",
                outcome_reason="recursive expansion produced a saved intermediate artifact",
                artifact_title=artifact_payload["shared_header"]["title"],
                artifact_source=artifact_payload["shared_header"]["source"],
                artifact_markdown_path=str(output_path.resolve()),
                selected_area=artifact_payload["shared_header"]["research_area"],
            )
        outputs.append(
            {
                "title": artifact_payload["shared_header"]["title"],
                "problem": artifact_payload.get("problem_intelligence", {}).get("selected_problem") or "No problem extracted",
                "problem_intelligence": artifact_payload.get("problem_intelligence", {}),
                "problem_signature": "pending-synthesis",
                "confidence": "Medium",
                "source": artifact_payload["shared_header"]["source"],
                "provenance_type": "real_source_evidence",
                "markdown_path": str(output_path.resolve()),
            }
        )

    collected_warnings.append("Collection-only mode saved intermediate artifacts; deeper synthesis is deferred.")

    finished_at = datetime.now(UTC)
    accepted_pages = len(outputs)
    collection_quality_metadata = {
        "scanned_pages": scanned_pages,
        "rejected_pages": rejected_pages,
        "accepted_pages": accepted_pages,
        "recursively_expanded_pages": recursively_expanded_pages,
    }
    if not documents:
        collected_warnings.append("No inputs were processed.")
        response = create_partial_response(
            agent=agent,
            layer=layer,
            mode=mode,
            area=area,
            items_processed=0,
            items_saved=0,
            outputs=[],
            warnings=collected_warnings,
            started_at=started_at,
            finished_at=finished_at,
        )
        response["metadata"]["collection_quality"] = collection_quality_metadata
        return response

    if not outputs:
        collected_warnings.append("All collected inputs were rejected by collection quality gates.")
        response = create_partial_response(
            agent=agent,
            layer=layer,
            mode=mode,
            area=area,
            items_processed=scanned_pages,
            items_saved=0,
            outputs=[],
            warnings=collected_warnings,
            started_at=started_at,
            finished_at=finished_at,
        )
        response["metadata"]["collection_quality"] = collection_quality_metadata
        return response

    response = create_success_response(
        agent=agent,
        layer=layer,
        mode=mode,
        area=area,
        items_processed=scanned_pages,
        items_saved=len(outputs),
        outputs=outputs,
        warnings=collected_warnings,
        started_at=started_at,
        finished_at=finished_at,
    )
    response["metadata"]["collection_quality"] = collection_quality_metadata
    return response


def _parse_markdown_sections(path: Path) -> tuple[str, Dict[str, str]]:
    title = path.stem.replace("-", " ").title()
    sections: Dict[str, list[str]] = {}
    current: Optional[str] = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if line.startswith("# "):
            title = line[2:].strip() or title
            current = None
            continue
        if line.startswith("## "):
            current = line[3:].strip()
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)

    flattened = {name: "\n".join(lines).strip() for name, lines in sections.items()}
    return title, flattened


def _first_section(sections: Dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = sections.get(name)
        if value:
            return value
    return ""


def _infer_area_from_path(path: Path) -> Optional[str]:
    for part in reversed(path.parts):
        normalized = normalize_area(part)
        if normalized in ALLOWED_RESEARCH_AREAS:
            return normalized
    return None


def parse_markdown_output(
    path: Path,
    *,
    problem_headings: tuple[str, ...],
    default_source: Optional[str] = None,
    default_confidence: str = "Medium",
) -> Dict[str, Any]:
    title, sections = _parse_markdown_sections(path)
    problem = _first_section(sections, problem_headings)
    signature = _first_section(sections, ("Problem Signature",))
    confidence = _first_section(sections, ("Confidence",)) or default_confidence
    source = _first_section(sections, ("Source",)) or (default_source or "Unknown")

    return {
        "title": title,
        "problem": problem,
        "problem_signature": signature,
        "confidence": confidence,
        "source": source,
        "markdown_path": str(path.resolve()),
        "area": _infer_area_from_path(path),
    }


def build_standard_file_outputs(
    output_dir: Path,
    before_snapshot: Dict[str, float],
    *,
    problem_headings: tuple[str, ...],
    default_source: Optional[str] = None,
    ) -> list[Dict[str, Any]]:
    if not output_dir.exists():
        return []

    outputs: list[Dict[str, Any]] = []
    for path in sorted(output_dir.rglob("*.md")):
        if path.name.lower() == "insights.md":
            continue
        resolved = str(path.resolve())
        try:
            current_mtime = path.stat().st_mtime
        except OSError:
            continue
        previous_mtime = before_snapshot.get(resolved)
        if previous_mtime is not None and current_mtime <= previous_mtime:
            continue
        outputs.append(
            parse_markdown_output(
                path,
                problem_headings=problem_headings,
                default_source=default_source,
            )
        )
    return outputs


def finalize_file_agent_response(
    *,
    agent: str,
    layer: str,
    mode: str,
    area: Optional[str],
    items_processed: int,
    items_saved: int,
    output_dir: Path,
    before_snapshot: Dict[str, float],
    problem_headings: tuple[str, ...],
    started_at: datetime,
    warnings: Optional[list[str]] = None,
    default_source: Optional[str] = None,
) -> Dict[str, Any]:
    outputs = build_standard_file_outputs(
        output_dir,
        before_snapshot,
        problem_headings=problem_headings,
        default_source=default_source,
    )
    response_area = normalize_area(area)
    if response_area is None and outputs:
        inferred_areas = {item.get("area") for item in outputs if item.get("area")}
        if len(inferred_areas) == 1:
            response_area = inferred_areas.pop()
    for item in outputs:
        item.pop("area", None)

    collected_warnings = list(warnings or [])
    if items_saved != len(outputs):
        collected_warnings.append(
            f"items_saved reported {items_saved}, but {len(outputs)} markdown output(s) were detected."
        )
    if items_saved == 0:
        collected_warnings.append("No new markdown outputs were saved.")
    if items_processed == 0:
        collected_warnings.append("No inputs were processed.")

    finished_at = datetime.now(UTC)
    if collected_warnings or items_saved < items_processed:
        return create_partial_response(
            agent=agent,
            layer=layer,
            mode=mode,
            area=response_area,
            items_processed=items_processed,
            items_saved=items_saved,
            outputs=outputs,
            warnings=collected_warnings,
            started_at=started_at,
            finished_at=finished_at,
        )

    return create_success_response(
        agent=agent,
        layer=layer,
        mode=mode,
        area=response_area,
        items_processed=items_processed,
        items_saved=items_saved,
        outputs=outputs,
        warnings=collected_warnings,
        started_at=started_at,
        finished_at=finished_at,
    )
