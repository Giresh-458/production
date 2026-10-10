from __future__ import annotations

import hashlib
import json
import re
import ast
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.schemas import normalize_area, rank_research_areas, ALLOWED_RESEARCH_AREAS
from core.evidence_ledger import (
    get_ledger_connection,
    upsert_canonical_evidence,
    upsert_claim,
    link_evidence_claim,
    upsert_entity,
    link_evidence_entity
)

NORMALIZED_OUTPUT_DIR = Path("outputs/normalized")

LAYER_EVIDENCE_WEIGHTS = {
    "literature": 1.00,
    "regulation": 1.00,
    "failure": 0.95,
    "opensource": 0.90,
    "practitioner": 0.85,
    "lab": 0.80,
    "company": 0.75,
    "funding": 0.70,
    "dataavailability": 0.70,
    "investment": 0.35,
    "hackathon": 0.25,
}

def evidence_strength_for_record(layer: str, evidence_type: str, agent_fields: dict[str, str]) -> float:
    normalized_layer = re.sub(r"[^a-z0-9]", "", (layer or "").lower())
    base = LAYER_EVIDENCE_WEIGHTS.get(normalized_layer, 0.50)
    explicit = agent_fields.get("Evidence Weight") or agent_fields.get("evidence_weight")
    if explicit:
        try:
            return max(0.0, min(1.0, float(explicit)))
        except ValueError:
            pass
    signal = " ".join(str(value) for value in agent_fields.values()).lower()
    if normalized_layer == "hackathon" or "emerging_demand" in signal:
        return min(base, 0.25)
    if normalized_layer == "investment" or "market validation" in signal:
        return min(base, 0.35)
    return base


def _tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z0-9]+", (text or "").lower())
    return sorted({token for token in tokens if len(token) > 2})


def _clean(text: str) -> str:
    return " ".join((text or "").split()).strip()


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        filtered_query = [(k, v) for k, v in query if not k.startswith("utm_")]

        # Rebuild URL
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        # Drop default ports
        if scheme == "http" and netloc.endswith(":80"):
            netloc = netloc[:-3]
        elif scheme == "https" and netloc.endswith(":443"):
            netloc = netloc[:-4]

        path = parsed.path
        if not path:
            path = "/"

        # Don't strip trailing slash arbitrarily as it might be semantically meaningful on some domains,
        # but let's just use it as is
        new_query = urllib.parse.urlencode(filtered_query)
        new_url = urllib.parse.urlunparse((scheme, netloc, path, parsed.params, new_query, parsed.fragment))
        return new_url
    except Exception:
        return url

def _parse_multilevel_markdown(path: Path) -> dict[str, Any]:
    title = path.stem.replace("-", " ").title()
    sections: dict[str, dict[str, Any]] = {}
    current_h2: str | None = None
    current_h3: str | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        # Some older/manual artifacts were written with indentation before
        # Markdown headings. Normalize only the heading prefix so those sections
        # (especially Focus Area) remain parseable without altering content.
        line = raw_line.rstrip()
        heading_line = line.lstrip()
        if heading_line.startswith("# "):
            title = heading_line[2:].strip() or title
            current_h2 = None
            current_h3 = None
            continue
        if heading_line.startswith("## "):
            current_h2 = heading_line[3:].strip()
            current_h3 = None
            sections.setdefault(current_h2, {"body": [], "subsections": {}})
            continue
        if heading_line.startswith("### ") and current_h2 is not None:
            current_h3 = heading_line[4:].strip()
            sections[current_h2]["subsections"].setdefault(current_h3, [])
            continue
        if current_h2 is None:
            continue
        if current_h3 is not None:
            sections[current_h2]["subsections"][current_h3].append(line)
        else:
            sections[current_h2]["body"].append(line)

    parsed: dict[str, Any] = {"title": title, "sections": {}}
    for name, payload in sections.items():
        parsed["sections"][name] = {
            "body": "\n".join(payload["body"]).strip(),
            "subsections": {sub_name: "\n".join(lines).strip() for sub_name, lines in payload["subsections"].items()},
        }
    return parsed


def _parse_bullet_map(section_text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_line in (section_text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("- ") or ":" not in line:
            continue
        key, value = line[2:].split(":", 1)
        # Normalize key to snake_case for consistency
        normalized_key = key.strip().replace(" ", "_").lower()
        parsed[normalized_key] = value.strip()
    return parsed


def _parse_possible_object(value: str) -> Any:
    text = (value or "").strip()
    if not text:
        return text
    if not ((text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]"))):
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return text


def _parse_list_block(text: str) -> list[str]:
    items: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("- "):
            value = line[2:].strip()
            if value and value.lower() != "none":
                items.append(value)
        elif line:
            items.append(line)
    return items


def _coalesce(*values: str) -> str:
    for value in values:
        cleaned = _clean(value)
        if cleaned:
            return cleaned
    return ""


def _body_field(body: dict[str, str], *names: str) -> str:
    lowered = {key.lower(): value for key, value in body.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return _clean(value)
    return ""

def generate_content_hash(title: str, source_type: str, problem_statement: str, context_summary: str) -> str:
    text = f"{_clean(title)}|{_clean(source_type)}|{_clean(problem_statement)}|{_clean(context_summary)}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def extract_source_identity(title: str, source_url: str, canonical_url: str, metadata: dict) -> str:
    # 1. DOI
    source_hints = metadata.get("source_hints", {})
    if isinstance(source_hints, dict):
        doi = source_hints.get("doi") or metadata.get("doi")
        if doi:
            return f"doi:{doi}"

    # 2. canonical URL
    if canonical_url:
        return f"url:{canonical_url}"
    if source_url:
        return f"url:{source_url}"

    # 3. Hash
    return f"hash:{hashlib.sha256(title.encode('utf-8')).hexdigest()[:16]}"


def extract_claims(evidence_snippets: list[str]) -> list[Dict[str, str]]:
    claims = []
    for snip in evidence_snippets:
        # VERY simple deterministic extraction: treat snippet as a claim text
        text = _clean(snip)
        if len(text) > 20:
            cid = hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
            claims.append({
                "claim_id": cid,
                "claim_text": text,
                "claim_type": "extracted_snippet",
                "problem_signature": "pending-synthesis"
            })
    return claims


def _normalize_intermediate_sections(path: Path) -> dict[str, Any]:
    parsed = _parse_multilevel_markdown(path)
    sections = parsed["sections"]

    shared_header = _parse_bullet_map(sections.get("Shared Metadata", {}).get("body", ""))
    if not shared_header:
        # Backward compatibility for older intermediate artifacts.
        for key in ("Title", "Problem", "Source", "Layer", "Research Area", "Collected At", "Agent Name", "Agent Version", "Collection Mode", "Funding Call Id", "Run Id"):
            legacy_value = sections.get(key, {}).get("body", "")
            if legacy_value:
                mapped_key = {
                    "Title": "title",
                    "Problem": "problem",
                    "Source": "source",
                    "Layer": "layer",
                    "Research Area": "research_area",
                    "Collected At": "collected_at",
                    "Agent Name": "agent_name",
                    "Agent Version": "agent_version",
                    "Collection Mode": "collection_mode",
                    "Funding Call Id": "funding_call_id",
                    "Run Id": "run_id"
                }.get(key, key.replace(" ", "_").lower())
                shared_header[mapped_key] = _clean(legacy_value)

    provenance = _parse_bullet_map(sections.get("Provenance", {}).get("body", ""))
    raw_machine_metadata = _parse_bullet_map(sections.get("Machine Metadata", {}).get("body", "")) or _parse_bullet_map(
        sections.get("Metadata", {}).get("body", "")
    )
    machine_metadata = {key: _parse_possible_object(value) for key, value in raw_machine_metadata.items()}

    evidence_subsections = sections.get("Evidence Bundle", {}).get("subsections", {})
    evidence_bundle = {
        "page_title": _coalesce(evidence_subsections.get("Page Title", ""), shared_header.get("title", "")),
        "section_headings": _parse_list_block(evidence_subsections.get("Section Headings", "")),
        "key_paragraphs": _parse_list_block(evidence_subsections.get("Key Paragraphs", "")),
        "bullet_lists": _parse_list_block(evidence_subsections.get("Bullet Lists", "")),
        "quoted_passages": _parse_list_block(evidence_subsections.get("Quoted Passages", "")),
        "links_found": _parse_list_block(evidence_subsections.get("Links Found", "")),
        "named_entities_or_programs": _parse_list_block(evidence_subsections.get("Named Entities Or Programs", "")),
        "evidence_snippets": _parse_list_block(evidence_subsections.get("Evidence Snippets", "")),
        "excerpt_windows": _parse_list_block(evidence_subsections.get("Excerpt Windows", "")),
    }

    body_subsections = sections.get("Agent-Specific Body", {}).get("subsections", {})
    agent_body = {name: _clean(value) for name, value in body_subsections.items() if _clean(value)}

    if not agent_body and isinstance(machine_metadata.get("source_hints"), dict):
        # Legacy fallback: if old artifact stored useful hints only in metadata.
        agent_body = {key.replace("_", " ").title(): str(value) for key, value in machine_metadata["source_hints"].items()}

    problem_intel_body = sections.get("Problem Intelligence", {}).get("body", "")
    selected_problem_match = re.search(r"\*\*Selected Problem\*\*:\s*(.*)", problem_intel_body)
    extracted_selected_problem = selected_problem_match.group(1).strip() if selected_problem_match else ""

    original_candidate_match = re.search(r"\*\*Original Candidate\*\*:\s*(.*)", problem_intel_body)
    extracted_original_candidate = original_candidate_match.group(1).strip() if original_candidate_match else ""

    def find_section(name: str) -> str:
        name_clean = re.sub(r'^\d+\.\s*', '', name).lower().replace(' ', '')
        for h2, payload in sections.items():
            h2_clean = re.sub(r'^\d+\.\s*', '', h2).lower().replace(' ', '')
            if name_clean in h2_clean or h2_clean in name_clean:
                if payload.get("body", "").strip():
                    return payload["body"]
            for h3, h3_body in payload.get("subsections", {}).items():
                h3_clean = re.sub(r'^\d+\.\s*', '', h3).lower().replace(' ', '')
                if name_clean in h3_clean or h3_clean in name_clean:
                    if h3_body.strip():
                        return h3_body
        return ""

    return {
        "title": parsed["title"],
        "shared_header": shared_header,
        "provenance": provenance,
        "machine_metadata": machine_metadata,
        "evidence_bundle": evidence_bundle,
        "agent_body": agent_body,
        "problem_preview": _coalesce(
            extracted_selected_problem,
            find_section("Problem Preview"),
            shared_header.get("problem", ""),
            find_section("Problem Statement"),
            find_section("Problem"),
        ),
        "original_candidate": extracted_original_candidate,
        "raw_content": _coalesce(find_section("Raw Content"), find_section("Problem Statement"), find_section("Problem")),
        "focus_area": _coalesce(find_section("Focus Area"), shared_header.get("focus_area", "")),
    }


def normalize_intermediate_artifact(path: Path) -> dict[str, Any]:
    artifact = _normalize_intermediate_sections(path)
    shared_header = artifact["shared_header"]
    provenance = artifact["provenance"]
    evidence = artifact["evidence_bundle"]
    body = artifact["agent_body"]
    machine_metadata = artifact["machine_metadata"]
    raw_content = artifact["raw_content"]
    source_hints = machine_metadata.get("source_hints", {}) if isinstance(machine_metadata.get("source_hints"), dict) else {}

    title = _coalesce(shared_header.get("title", ""), artifact["title"])
    agent_name = _clean(shared_header.get("agent_name", ""))
    if not agent_name:
        # Infer from directory: .../intermediate/<agent_name>/...
        try:
            parts = path.resolve().parts
            if "intermediate" in parts:
                idx = parts.index("intermediate")
                if idx + 1 < len(parts):
                    agent_name = parts[idx + 1]
        except Exception:
            pass
    agent_name = agent_name or "unknown"
    layer = _clean(shared_header.get("layer", "")) or agent_name.title()
    # Funding markdowns carry their canonical area in a dedicated Focus Area
    # section rather than inside Shared Metadata. Preserve that signal so a live
    # funding call is not normalized as Unscoped.
    area_candidates = [
        shared_header.get("research_area", ""),
        artifact.get("focus_area", ""),
    ]
    # LiteratureAgent historically wrote `# Research Gap: <AREA>` without a
    # Shared Metadata block. Recover the canonical routed area from that title
    # before falling back to content keyword inference.
    if not any(normalize_area(value) in {
        "RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"
    } for value in area_candidates if value):
        h1_title = _clean(artifact.get("title", ""))
        if ":" in h1_title:
            area_candidates.append(h1_title.split(":", 1)[1].strip())
    research_area = next((normalize_area(value) for value in area_candidates if normalize_area(value) in {
        "RWA", "ESG", "ZK-IoV", "DID", "DePIN", "MEV", "Stablecoins", "DigitalHealthCPS"
    }), "Unscoped")
    source_url = _coalesce(provenance.get("source_url", ""), shared_header.get("source", ""))
    canonical_url = canonicalize_url(source_url)
    collected_at = _coalesce(shared_header.get("collected_at", ""), provenance.get("collected_at", ""))
    collection_mode = _coalesce(shared_header.get("collection_mode", ""))

    funding_call_id = _clean(shared_header.get("funding_call_id", ""))
    raw_call_ids = shared_header.get("funding_call_ids", [])
    if isinstance(raw_call_ids, str):
        funding_call_ids = [c.strip() for c in raw_call_ids.split(",") if c.strip()]
    elif isinstance(raw_call_ids, list):
        funding_call_ids = [str(c).strip() for c in raw_call_ids if str(c).strip()]
    else:
        funding_call_ids = []
    if funding_call_id and funding_call_id not in funding_call_ids:
        funding_call_ids.append(funding_call_id)

    run_id = _clean(shared_header.get("run_id", ""))

    source_type = _coalesce(
        _body_field(body, "Source Type"),
        str(machine_metadata.get("source_type", "")),
        str(source_hints.get("source_type", "")),
    )
    evidence_type = _coalesce(
        _body_field(body, "Evidence Type"),
        str(machine_metadata.get("evidence_type", "")),
        str(source_hints.get("evidence_type", "")),
    )
    actor = _coalesce(
        _body_field(body, "Funding Organization", "Investor / Organization", "Sponsor / Organizer", "Issuing Body"),
        _body_field(body, "Source Name", "Company Name", "Project Name", "Lab Name"),
        str(source_hints.get("organization", "")),
        str(source_hints.get("company_name", "")),
        str(source_hints.get("source_name", "")),
        str(source_hints.get("issuing_body", "")),
        str(source_hints.get("investor_or_organization", "")),
        str(machine_metadata.get("organization", "")),
        str(machine_metadata.get("investor_or_organization", "")),
        str(machine_metadata.get("source_name", "")),
        str(machine_metadata.get("company_name", "")),
        str(machine_metadata.get("issuing_body", "")),
        source_url,
    )
    focus = _coalesce(
        _body_field(body, "Focus"),
        str(source_hints.get("focus", "")),
        str(machine_metadata.get("focus", "")),
        str(machine_metadata.get("source_hints", "")),
    )
    keywords = _tokenize(
        " ".join(
            [
                title,
                research_area,
                artifact["problem_preview"],
                " ".join(evidence.get("named_entities_or_programs", [])),
                " ".join(evidence.get("links_found", [])),
            ]
        )
    )[:20]

    normalized_problem = _coalesce(
        artifact["problem_preview"],
        "\n".join(evidence.get("evidence_snippets", [])),
        raw_content[:280],
    )
    context_summary = _coalesce(
        "\n".join(evidence.get("key_paragraphs", [])),
        "\n".join(evidence.get("excerpt_windows", [])),
        raw_content[:800],
    )
    entity_tokens = (evidence.get("named_entities_or_programs", []) or [actor, title])[:12]
    linked_urls = list(dict.fromkeys(([source_url] if source_url else []) + evidence.get("links_found", [])))[:20]

    content_hash = generate_content_hash(title, source_type, normalized_problem, context_summary)
    source_id = extract_source_identity(title, source_url, canonical_url, machine_metadata)

    # Needs review if lacking funding call id or source
    needs_review = (not funding_call_id and not funding_call_ids) or not source_url
    normalization_status = "NEEDS_REVIEW" if needs_review else "NORMALIZED"

    evidence_snippets_str = "\n".join(evidence.get("evidence_snippets", []))
    evidence_id = hashlib.sha256(f"{source_id}|{funding_call_id}|{evidence_snippets_str}".encode('utf-8')).hexdigest()[:16]

    # Domain relevance is evidence-derived and deliberately independent from
    # the routed research_area. This prevents routing metadata from overriding
    # what the source itself actually supports.
    domain_relevance = rank_research_areas(
        "\n".join(
            [
                title,
                source_type,
                evidence_type,
                focus,
                normalized_problem,
                context_summary,
                " ".join(evidence.get("named_entities_or_programs", [])),
                " ".join(evidence.get("evidence_snippets", [])),
            ]
        )
    )

    normalized_record = {
        "evidence_id": evidence_id,
        "record_id": hashlib.sha1(f"{path.resolve()}|{title}|{source_url}".encode("utf-8")).hexdigest()[:16],
        "funding_call_id": funding_call_id,
        "funding_call_ids": funding_call_ids,
        "run_id": run_id,
        "file_path": str(path.resolve()),
        "record_kind": "normalized_intermediate",
        "agent_name": agent_name,
        "layer": layer,
        "research_area": research_area,
        "title": title,
        "source_id": source_id,
        "source_url": source_url,
        "canonical_url": canonical_url,
        "collected_at": collected_at,
        "collection_mode": collection_mode,
        "source_type": source_type or "Unknown",
        "evidence_type": evidence_type or "Unknown",
        "actor": actor or "Unknown",
        "focus": focus,
        "problem_statement": normalized_problem,
        "original_candidate": artifact.get("original_candidate", ""),
        "context_summary": context_summary,
        "evidence_snippets": evidence.get("evidence_snippets", []),
        "excerpt_windows": evidence.get("excerpt_windows", []),
        "named_entities": entity_tokens,
        "linked_urls": linked_urls,
        "keywords": keywords,
        "agent_specific_fields": body,
        "machine_metadata": artifact["machine_metadata"],
        "evidence_strength": evidence_strength_for_record(layer, evidence_type, body),
        "research_evidence": True,

        "search_text": _clean(
            " ".join(
                [
                    title,
                    layer,
                    research_area,
                    source_type,
                    evidence_type,
                    actor,
                    focus,
                    normalized_problem,
                    context_summary,
                    " ".join(entity_tokens),
                ]
            )
        ),
        "content_hash": content_hash,
        "normalization_status": normalization_status,
        "routed_research_area": research_area,
        "domain_relevance": domain_relevance,
    }
    normalized_record["tokens"] = _tokenize(normalized_record["search_text"])
    return normalized_record

def build_normalized_collection_records(intermediate_root: Path | list[Path], target_funding_call_id: str | None = None, *, errors: list[str] | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    roots = [intermediate_root] if isinstance(intermediate_root, Path) else intermediate_root

    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            if path.name.lower() == "insights.md":
                continue
            try:
                record = normalize_intermediate_artifact(path)
                # Filter by target_funding_call_id if provided
                if target_funding_call_id:
                    call_ids = record.get("funding_call_ids", [])
                    explicit_id = record.get("funding_call_id")

                    if explicit_id == target_funding_call_id:
                        # Valid explicit ownership
                        pass
                    elif target_funding_call_id in call_ids:
                        # Do not treat appearance in a broad multi-ID list as proof of ownership
                        if len(call_ids) > 3:
                            # Ambiguous: leave evidence unassigned / skip for this call
                            continue
                        else:
                            # Legitimate shared evidence (e.g. 1-3 specific calls)
                            record["funding_call_id"] = target_funding_call_id
                    else:
                        continue

                records.append(record)
            except Exception as exc:
                if errors is not None:
                    errors.append(f"{path}: {exc}")
    return records


def _build_indexes(records: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    by_area: dict[str, list[dict[str, Any]]] = {}
    by_layer: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        raw_area = record.get("research_area")
        area = normalize_area(raw_area)
        if area not in ALLOWED_RESEARCH_AREAS:
            area = "Unscoped"
        by_area.setdefault(area, []).append(record)

        raw_layer = record.get("layer")
        if raw_layer:
            layer = str(raw_layer).strip()
            if layer:
                by_layer.setdefault(layer, []).append(record)
    return by_area, by_layer


def save_normalized_collection_records(intermediate_root: Path | list[Path], output_dir: Path, target_funding_call_id: str | None = None, filter_func=None) -> dict[str, Path]:
    errors: list[str] = []
    records = build_normalized_collection_records(intermediate_root, target_funding_call_id=target_funding_call_id, errors=errors)
    target_dir = output_dir / "normalized"
    normalized_root = target_dir
    deduped_records = {}
    for r in records:
        sid = r["evidence_id"]
        if sid not in deduped_records:
            r["observation_count"] = 1
            r["first_seen"] = r["collected_at"]
            r["last_seen"] = r["collected_at"]
            r["content_versions"] = [r["content_hash"]]
            deduped_records[sid] = r
        else:
            existing = deduped_records[sid]
            existing["observation_count"] += 1
            if r["collected_at"] < existing["first_seen"]:
                existing["first_seen"] = r["collected_at"]
            if r["collected_at"] > existing["last_seen"]:
                existing["last_seen"] = r["collected_at"]
                # keep latest data
                existing["problem_statement"] = r["problem_statement"]
                existing["context_summary"] = r["context_summary"]
                existing["content_hash"] = r["content_hash"]
                existing["evidence_snippets"] = r["evidence_snippets"]

            if r["content_hash"] not in existing["content_versions"]:
                existing["content_versions"].append(r["content_hash"])

    records = list(deduped_records.values())
    if filter_func:
        records = [r for r in records if filter_func(r)]

    import shutil
    if normalized_root.exists():
        shutil.rmtree(normalized_root)
    normalized_root.mkdir(parents=True, exist_ok=True)

    records_path = normalized_root / "records.json"
    records_path.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    by_area, by_layer = _build_indexes(records)

    by_area_path = normalized_root / "by_area.json"
    by_area_path.write_text(json.dumps(by_area, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    by_layer_path = normalized_root / "by_layer.json"
    by_layer_path.write_text(json.dumps(by_layer, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # DB Insertion Layer for Evidence Ledger
    try:
        db_path = normalized_root / "evidence_ledger.db"
        conn = get_ledger_connection(db_path)

        # We need to compute independence groups.
        # Deterministic clustering based on content_hash
        content_hash_to_group = {}
        for rec in records:
            if rec["content_hash"] not in content_hash_to_group:
                content_hash_to_group[rec["content_hash"]] = rec["source_id"]

        for record in records:
            if record["normalization_status"] == "NEEDS_REVIEW":
                independence_status = "UNKNOWN"
                independence_group = "UNKNOWN"
            else:
                independence_group = content_hash_to_group[record["content_hash"]]
                if independence_group == record["source_id"]:
                    independence_status = "INDEPENDENT"
                else:
                    independence_status = "DERIVED"

            # Flatten to canonical schema
            canonical_rec = {
                "evidence_id": record["evidence_id"],
                "record_id": record["record_id"],
                "funding_call_id": record["funding_call_id"],
                "run_id": record["run_id"],
                "agent": record["agent_name"],
                "layer": record["layer"],
                "source_id": record["source_id"],
                "source_url": record["source_url"],
                "canonical_url": record["canonical_url"],
                "source_type": record["source_type"],
                "source_name": record["actor"],
                "title": record["title"],
                "problem": record["problem_statement"],
                "problem_signature": "pending-synthesis",
                "evidence": "\n".join(record["evidence_snippets"]),
                "evidence_excerpt": "\n".join(record["excerpt_windows"]),
                "evidence_type": record["evidence_type"],
                "research_area": record["research_area"],
                "published_at": record["machine_metadata"].get("source_hints", {}).get("published_at"),
                "retrieved_at": record["collected_at"],
                "updated_at": None,
                "content_hash": record["content_hash"],
                "source_quality": max(0.0, min(1.0, float(record.get("evidence_strength", 0.0) or 0.0))),
                "evidence_strength": record["evidence_strength"],
                "confidence": max(0.0, min(1.0, float(record.get("evidence_strength", 0.0) or 0.0))),
                "independence_group": independence_group,
                "independence_status": independence_status,
                "normalization_status": record["normalization_status"],
                "provenance": {
                    "source_url": record["source_url"],
                    "source_id": record["source_id"],
                    "agent": record["agent_name"],
                    "funding_call_id": record["funding_call_id"],
                    "run_id": record["run_id"],
                    "retrieved_at": record["collected_at"],
                    "raw_record_id": record["file_path"]
                },
                "raw_record_id": record["file_path"]
            }

            try:
                upsert_canonical_evidence(conn, canonical_rec)

                # Extract claims
                claims = extract_claims(record["evidence_snippets"])
                for claim in claims:
                    upsert_claim(conn, claim)
                    link_evidence_claim(conn, canonical_rec["evidence_id"], claim["claim_id"])

                # Extract entities
                for entity_name in record["named_entities"]:
                    eid = hashlib.sha256(entity_name.lower().encode('utf-8')).hexdigest()[:16]
                    entity = {
                        "entity_id": eid,
                        "canonical_name": entity_name,
                        "entity_type": "extracted_entity",
                        "aliases": [entity_name],
                        "external_ids": {}
                    }
                    upsert_entity(conn, entity)
                    link_evidence_entity(conn, canonical_rec["evidence_id"], eid)

            except Exception as e:
                errors.append(f"DB Error for {record['record_id']}: {e}")

        conn.commit()
        conn.close()
    except Exception as e:
        errors.append(f"Ledger DB Init Error: {e}")

    errors_path = normalized_root / "errors.json"
    errors_path.write_text(json.dumps(errors, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {
        "records": records_path,
        "by_area": by_area_path,
        "by_layer": by_layer_path,
        "errors": errors_path,
    }


def resolve_refresh_context(outputs_root: Path):
    if outputs_root.parent.name == "calls":
        target_funding_call_id = outputs_root.name
        run_root = outputs_root.parent.parent
        intermediate_roots = [run_root / "intermediate", outputs_root / "intermediate"]
    else:
        target_funding_call_id = None
        intermediate_roots = [outputs_root / "intermediate"]

    filter_func = None
    if target_funding_call_id:
        selected_path = outputs_root / "workflow" / "selected_funding_call.json"
        if not selected_path.exists():
            raise FileNotFoundError(f"Cannot safely refresh collection records for call '{target_funding_call_id}': {selected_path} is missing, so mission gate cannot be applied.")

        from core.funding_selection import FundingCallContext, ScoredCall, ValidationResult
        from core.mission_gate import relevance as mission_relevance
        try:
            import json
            wrapper = json.loads(selected_path.read_text(encoding="utf-8"))
            record = wrapper.get("selected")
            if not record:
                raise ValueError("Missing 'selected' key in selected_funding_call.json")

            scored = ScoredCall(
                call_id=target_funding_call_id,
                record=record,
                score=10.0,
                validation=ValidationResult(True, ["Reconstructed during refresh."]),
            )
            context = FundingCallContext.from_scored_call(scored, mode="autonomous_live", extra_reasons=["Reconstructed during refresh."])
            def apply_gate(rec):
                gate = mission_relevance(rec, context)
                rec["mission_relevance"] = gate
                return gate["passed"]
            filter_func = apply_gate
        except Exception as e:
            raise RuntimeError(f"Cannot safely refresh collection records for call '{target_funding_call_id}': failed to reconstruct mission context. Details: {e}")

    return intermediate_roots, target_funding_call_id, filter_func


def ensure_normalized_collection_records(outputs_root: Path) -> dict[str, Path]:
    records_path = outputs_root / "normalized" / "records.json"
    intermediate_roots, target_funding_call_id, filter_func = resolve_refresh_context(outputs_root)

    newest_intermediate = 0.0
    for ir in intermediate_roots:
        if ir.exists():
            for path in ir.rglob("*.md"):
                if path.name.lower() == "insights.md":
                    continue
                try:
                    newest_intermediate = max(newest_intermediate, path.stat().st_mtime)
                except OSError:
                    continue
    try:
        records_mtime = records_path.stat().st_mtime
    except OSError:
        records_mtime = 0.0
    force_refresh = False
    if not records_path.exists() or newest_intermediate > records_mtime:
        force_refresh = True
    elif filter_func:
        import json
        import copy
        try:
            cached = json.loads(records_path.read_text(encoding="utf-8"))
            for r in cached:
                if not filter_func(copy.deepcopy(r)):
                    force_refresh = True
                    break

            if not force_refresh:
                area_path = outputs_root / "normalized" / "by_area.json"
                layer_path = outputs_root / "normalized" / "by_layer.json"

                expected_by_area, expected_by_layer = _build_indexes(cached)

                needs_repair = False
                try:
                    area_data = json.loads(area_path.read_text(encoding="utf-8"))
                    if area_data != expected_by_area:
                        needs_repair = True
                except Exception:
                    needs_repair = True

                try:
                    layer_data = json.loads(layer_path.read_text(encoding="utf-8"))
                    if layer_data != expected_by_layer:
                        needs_repair = True
                except Exception:
                    needs_repair = True

                if needs_repair:
                    area_path.write_text(json.dumps(expected_by_area, indent=2), encoding="utf-8")
                    layer_path.write_text(json.dumps(expected_by_layer, indent=2), encoding="utf-8")
        except Exception:
            force_refresh = True

    if force_refresh:
        return save_normalized_collection_records(intermediate_roots, outputs_root, target_funding_call_id, filter_func=filter_func)
    return {
        "records": records_path,
        "by_area": outputs_root / "normalized" / "by_area.json",
        "by_layer": outputs_root / "normalized" / "by_layer.json",
        "errors": outputs_root / "normalized" / "errors.json",
    }
