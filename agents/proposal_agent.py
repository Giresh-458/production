from __future__ import annotations

import argparse
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

from core.schemas import create_error_response, create_partial_response, create_success_response, normalize_area
from core.mission_gate import relevance as mission_relevance

LOGGER = logging.getLogger("rif.proposal_agent")

DEFAULT_OUTPUT_DIR = Path("outputs/proposals")
DEFAULT_IDEA_DIR = Path("outputs/ideas")
AGENT_NAME = "proposal"
LAYER_NAME = "Intelligence"

AREA_FUNDING_ALIGNMENT = {
    "RWA": "Align with funders focused on tokenized asset infrastructure, compliance automation, and reserve-verification systems.",
    "ESG": "Align with climate-finance, carbon-market integrity, MRV, and sustainability-infrastructure funders.",
    "ZK-IoV": "Align with mobility, privacy-preserving systems, V2X communications, and proving-infrastructure programs.",
    "DID": "Align with digital identity, wallet infrastructure, verifiable credentials, and interoperability-focused programs.",
    "DePIN": "Align with storage, wireless, device, and decentralized infrastructure ecosystem grant programs.",
    "MEV": "Align with market-structure, relay, builder, block-production, and execution-integrity research programs.",
    "Stablecoins": "Align with payment-rail, reserve-transparency, settlement, and stable-value infrastructure funders.",
    "DigitalHealthCPS": "Align with digital-health CPS funders focused on clinical validation, product translation, safety, deployment readiness, commercialization, and healthcare stakeholder adoption.",
}


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def cleaned_text(value: str) -> str:
    return " ".join((value or "").split()).strip()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "item"


def _parse_evidence_inputs(raw: str) -> list[dict[str, str]]:
    """Parse the Evidence Inputs section from a synthesis markdown into structured dicts.

    Each line in the section has the format:
        - AgentLayer | SourceName | evidence_snippet: <text>
    Returns a list of dicts with keys: agent_name, source_name, evidence_snippet.
    """
    evidence: list[dict[str, str]] = []
    if not raw:
        return evidence
    for line in raw.splitlines():
        line = line.strip().lstrip("- ").strip()
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) >= 2:
            agent_name = parts[0].strip()
            rest = parts[1].strip() if len(parts) == 2 else parts[1].strip()
            snippet = ""
            source_name = rest
            if len(parts) == 3:
                snippet_part = parts[2].strip()
                if snippet_part.lower().startswith("evidence_snippet:"):
                    snippet = snippet_part[len("evidence_snippet:"):].strip()
                elif snippet_part.lower().startswith("excerpt_window:"):
                    snippet = snippet_part[len("excerpt_window:"):].strip()
                else:
                    snippet = snippet_part
            evidence.append({
                "agent_name": agent_name,
                "source_name": source_name,
                "evidence_snippet": snippet,
            })
    return evidence


def parse_markdown_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current_heading: str | None = None
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("## "):
            if current_heading is not None:
                sections[current_heading] = "\n".join(lines).strip()
            current_heading = line[3:].strip()
            lines = []
        else:
            lines.append(line)
    if current_heading is not None:
        sections[current_heading] = "\n".join(lines).strip()
    return sections


def derive_proposal_title(base_title: str, area: str) -> str:
    clean = cleaned_text(base_title)
    for suffix in ("Research Idea", "Idea"):
        if clean.endswith(suffix):
            clean = clean[: -len(suffix)].strip(" -:")
    return f"{clean or area} Proposal"


def load_idea_records(idea_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if idea_dir.exists():
        for json_path in sorted(idea_dir.glob("**/ideas.json")):
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        payload = dict(item)
                        payload.setdefault("_source_file", str(json_path.resolve()))
                        records.append(payload)

        existing_keys = {
            (cleaned_text(str(item.get("based_on", ""))), cleaned_text(str(item.get("title", ""))))
            for item in records
            if cleaned_text(str(item.get("based_on", ""))) or cleaned_text(str(item.get("title", "")))
        }
        for md_path in sorted(idea_dir.glob("**/*.md")):
            if md_path.name == "insights.md":
                continue
            try:
                sections = parse_markdown_sections(md_path.read_text(encoding="utf-8"))
            except OSError:
                continue
            if not sections:
                continue
            title = md_path.stem.replace("-", " ").title()
            based_on_value = sections.get("Based On", str(md_path.resolve()))
            dedupe_key = (cleaned_text(str(based_on_value)), cleaned_text(title))
            if dedupe_key in existing_keys or (cleaned_text(str(based_on_value)) and any(cleaned_text(str(item.get("based_on", ""))) == cleaned_text(str(based_on_value)) for item in records)):
                continue
            payload = {
                    "title": title,
                    "problem": sections.get("Problem", ""),
                    "source": sections.get("Source", ""),
                    "layer": sections.get("Layer", "Idea"),
                    "research_area": sections.get("Research Area", ""),
                    "why_important": sections.get("Why Important", ""),
                    "existing_solutions": sections.get("Existing Solutions", ""),
                    "gap": sections.get("Gap", ""),
                    "idea": sections.get("Idea", ""),
                    "feasibility": sections.get("Feasibility", ""),
                    "hypothesis": sections.get("Hypothesis", ""),
                    "experiment_direction": sections.get("Experiment Direction", ""),
                    "prototype_scope": sections.get("Prototype Scope", ""),
                    "expected_dataset_need": sections.get("Expected Dataset / Benchmark Need", ""),
                    "likely_collaborators": sections.get("Likely Collaborators / Sponsors", ""),
                    "based_on": sections.get("Based On", str(md_path.resolve())),
                    "source_inputs": sections.get("Source Inputs", ""),
                    "evidence": _parse_evidence_inputs(sections.get("Evidence Inputs", "") or sections.get("Source Inputs", "")),
                    "confidence": sections.get("Confidence", "Medium"),
                    "tags": sections.get("Tags", "").split(),
                    "funding_call_ids": [id.strip() for id in sections.get("Funding Call IDs", "").split(",") if id.strip()],
                    "_source_file": str(md_path.resolve()),
                }
            records.append(payload)

    # Final pass: Fetch evidence from the synthesis file if it's missing from any idea record
    for payload in records:
        if not payload.get("evidence") and payload.get("based_on"):
            based_on_path = Path(payload["based_on"])
            if based_on_path.exists():
                try:
                    synthesis_sections = parse_markdown_sections(based_on_path.read_text("utf-8"))
                    payload["evidence"] = _parse_evidence_inputs(synthesis_sections.get("Evidence Inputs", "") or synthesis_sections.get("Source Inputs", ""))
                except OSError:
                    pass

    return records


def filter_idea_records(records: list[dict[str, Any]], *, area: str | None = None, source_url: str | None = None) -> list[dict[str, Any]]:
    normalized_area = normalize_area(area) if area else None
    filtered: list[dict[str, Any]] = []
    for record in records:
        if normalized_area and normalize_area(record.get("research_area")) != normalized_area:
            continue
        if source_url:
            candidates = [
                cleaned_text(str(record.get("source", ""))),
                cleaned_text(str(record.get("based_on", ""))),
                cleaned_text(str(record.get("_source_file", ""))),
            ]
            if cleaned_text(source_url) not in candidates:
                continue
        filtered.append(record)
    return filtered


def validate_proposal_payload(record: dict[str, Any], proposal: dict[str, Any], funding_requirements: dict[str, Any] | None = None) -> dict[str, Any]:
    requirements = funding_requirements or {}
    idea_validation = record.get("validation") if isinstance(record.get("validation"), dict) else {}
    checks = {
        "problem": bool(cleaned_text(proposal.get("problem"))),
        "gap": bool(cleaned_text(proposal.get("gap"))),
        "method": bool(cleaned_text(proposal.get("proposed_method"))),
        "evaluation": bool(cleaned_text(proposal.get("evaluation_plan"))),
        "funding_alignment": bool(cleaned_text(proposal.get("funding_alignment"))),
        "idea_validation_present": bool(idea_validation) or bool(record.get("based_on")),
        "external_novelty_validation": idea_validation.get("novelty_status") == "externally_validated",
        "human_submission_review": bool(record.get("submission_review_passed", False)),
    }
    compliance: dict[str, Any] = {}
    for key, expected in requirements.items():
        if key in {"required_topics", "required_partners", "required_metrics"}:
            haystack = " ".join(str(v) for v in proposal.values()).lower()
            values = expected if isinstance(expected, list) else [expected]
            compliance[key] = {str(v): str(v).lower() in haystack for v in values}
        else:
            compliance[key] = {"specified": expected, "status": "manual_check_required"}
    score = round(sum(bool(v) for v in checks.values()) / len(checks), 3)
    funding_requirements_passed = True
    for result in compliance.values():
        if not isinstance(result, dict):
            funding_requirements_passed = False
            break
        boolean_values = [v for v in result.values() if isinstance(v, bool)]
        if not boolean_values:
            # A requirement that still needs manual checking must never be
            # treated as satisfied merely because the automated checks passed.
            funding_requirements_passed = False
            break
        if not all(boolean_values):
            funding_requirements_passed = False
            break

    return {
        "checks": checks,
        "validation_score": score,
        "funding_requirements": requirements,
        "funding_compliance": compliance,
        "ready_for_submission": score == 1.0 and funding_requirements_passed,
        "status": "draft_requires_human_review",
    }


def build_proposal_payload_from_idea(record: dict[str, Any], funding_context: Any = None) -> dict[str, Any]:
    area = normalize_area(record.get("research_area")) or "Unscoped"
    title = derive_proposal_title(str(record.get("title", "")), area)
    problem = cleaned_text(str(record.get("problem", "")))
    why_important = cleaned_text(str(record.get("why_important", "")))
    existing_solutions = cleaned_text(str(record.get("existing_solutions", "")))
    gap = cleaned_text(str(record.get("gap", "")))
    idea = cleaned_text(str(record.get("idea", "")))
    feasibility = cleaned_text(str(record.get("feasibility", ""))) or "Moderate"
    hypothesis = cleaned_text(str(record.get("hypothesis", "")))
    experiment_direction = cleaned_text(str(record.get("experiment_direction", "")))
    prototype_scope = cleaned_text(str(record.get("prototype_scope", "")))
    dataset_need = cleaned_text(str(record.get("expected_dataset_need", "")))
    collaborators = cleaned_text(str(record.get("likely_collaborators", "")))

    motivation = (
        f"{why_important} "
        f"The proposal should convert this into a scoped research and implementation program with explicit deliverables rather than leaving it as an open-ended ecosystem recommendation."
    ).strip()
    proposed_method = (
        f"Use the hypothesis as the proposal core: {hypothesis} "
        f"Then implement the bounded prototype scope ({prototype_scope}) and formalize the intervention into measurable system components."
    ).strip()
    evaluation_plan = (
        f"{experiment_direction} "
        f"Evaluation should explicitly use or construct the following benchmark/data support: {dataset_need}"
    ).strip()
    
    if funding_context:
        funding_alignment = (
            f"Aligned with {funding_context.funding_body} - {funding_context.program_name} ({funding_context.funding_call_id}). "
            f"Deliverables must target {funding_context.deliverables}. "
            f"Eligibility constraints: {funding_context.eligibility}. "
            f"Duration: {funding_context.duration}, Amount: {funding_context.funding_amount}. "
            f"Likely collaborators: {collaborators or 'ecosystem stakeholders adjacent to this problem.'}"
        ).strip()
    else:
        funding_context_text = cleaned_text(str(record.get("funding_alignment", "")))
        funding_alignment = (
            f"{funding_context_text or 'Funding alignment is not established from a fixed taxonomy; it must be verified against the selected live funding call and its evidence.'} "
            f"Likely collaborators or sponsor entry points: {collaborators or 'ecosystem stakeholders already adjacent to this problem.'}"
        ).strip()
        
    confidence = cleaned_text(str(record.get("confidence", ""))) or "Unassessed"
    if confidence not in {"High", "Medium", "Low"}:
        confidence = "Unassessed"
    tags = record.get("tags", [])
    if isinstance(tags, str):
        tags = tags.split()
    normalized_tags = list(dict.fromkeys([str(tag) if str(tag).startswith("#") else f"#{tag}" for tag in tags if cleaned_text(str(tag))] + ["#Proposal"]))
    payload = {
        "title": title,
        "problem": problem,
        "funding_call_id": record.get("funding_call_id"),
        "funding_call_ids": record.get("funding_call_ids", []),
        "source": cleaned_text(str(record.get("source", ""))) or cleaned_text(str(record.get("based_on", ""))) or cleaned_text(str(record.get("_source_file", ""))),
        "layer": "Proposal",
        "research_area": area,
        "why_important": motivation,
        "existing_solutions": existing_solutions,
        "gap": gap,
        "idea": idea,
        "feasibility": feasibility,
        "motivation": motivation,
        "proposed_method": proposed_method,
        "evaluation_plan": evaluation_plan,
        "funding_alignment": funding_alignment,
        "based_on": cleaned_text(str(record.get("based_on", ""))) or cleaned_text(str(record.get("_source_file", ""))),
        "confidence": confidence,
        "tags": normalized_tags,
        "evidence": record.get("evidence", []),
    }
    payload["validation"] = validate_proposal_payload(record, payload)
    idea_validation = record.get("validation") if isinstance(record.get("validation"), dict) else {}
    payload["eligibility"] = {
        "eligible_for_proposal_review": bool(idea_validation.get("ready_for_proposal", False)),
        "novelty_status": idea_validation.get("novelty_status", "unknown"),
        "reason": (
            "Idea passed its local validation and external novelty validation."
            if idea_validation.get("ready_for_proposal")
            else "Proposal is a draft scaffold only; external novelty validation and human review are still required."
        ),
    }
    return payload


def build_manual_payload(*, text: str, title: str, source: str, area: str | None) -> dict[str, Any]:
    normalized_area = normalize_area(area) or "Unscoped"
    clean = cleaned_text(text)
    motivation = "Manual input suggests a candidate problem that is already specific enough to draft into a proposal scaffold."
    proposed_method = "Define a narrow technical intervention, implement a first prototype, and compare it against a measurable baseline."
    evaluation_plan = "Use a bounded benchmark or dataset slice and report explicit outcome metrics tied to the target system behavior."
    funding_alignment = "Funding alignment must be established from the selected live funding call and its preserved evidence; no fixed research-area mapping is used in adaptive mode."
    payload = {
        "title": derive_proposal_title(title or "Manual Input", normalized_area),
        "problem": clean[:500],
        "funding_call_id": None,
        "funding_call_ids": [],
        "source": source or "manual://proposal/manual-input",
        "layer": "Proposal",
        "research_area": normalized_area,
        "why_important": motivation,
        "existing_solutions": "Existing solutions should be mapped during follow-up literature, company, and open-source review.",
        "gap": clean[:500],
        "idea": "Turn the manual problem statement into a scoped research and prototype proposal.",
        "feasibility": "Moderate",
        "motivation": motivation,
        "proposed_method": proposed_method,
        "evaluation_plan": evaluation_plan,
        "funding_alignment": funding_alignment,
        "based_on": source or "Manual",
        "confidence": "Medium",
        "tags": ["#Proposal", "#Manual", f"#{normalized_area.replace('-', '')}"],
    }
    payload["validation"] = validate_proposal_payload(payload, payload)
    payload["eligibility"] = {
        "eligible_for_proposal_review": False,
        "novelty_status": "not_validated",
        "reason": "Manual proposal input is a draft scaffold and has not passed external novelty validation or human review.",
    }
    return payload



def _extract_and_check_claims(payload: dict) -> dict:
    from core.problem_extraction import validate_claim_against_evidence
    claims = []
    
    # Extract evidence spans if available
    evidence_data = payload.get("evidence") or payload.get("problem_evidence") or []
    if isinstance(evidence_data, list):
        evidence_texts = [str(e) for e in evidence_data]
    else:
        evidence_texts = [str(evidence_data)] if evidence_data else []

    claim_text = payload.get("problem", "")
    
    support_status = "uncertain"
    support_score = 0.0
    reason = ""
    if evidence_texts and claim_text:
        validation = validate_claim_against_evidence(claim_text, evidence_texts)
        support_status = "supported" if validation.is_supported else "unsupported"
        support_score = validation.score
        reason = validation.reason

    claims.append({
        "text": claim_text,
        "provenance": "EVIDENCE",
        "supporting_refs": [payload.get("source", "")] if payload.get("source") else [],
        "supporting_evidence_spans": evidence_texts,
        "support_score": support_score,
        "validation_status": support_status,
        "assumptions": []
    })
    
    claims.append({
        "text": payload.get("proposed_method", ""),
        "provenance": "PROPOSED",
        "supporting_refs": [],
        "assumptions": ["Method is technically feasible"]
    })
    
    claims.append({
        "text": payload.get("evaluation_plan", ""),
        "provenance": "PROPOSED",
        "supporting_refs": [],
        "assumptions": ["Benchmark is available"]
    })
    
    unsupported_flag = False
    for claim in claims:
        if claim["provenance"] == "EVIDENCE":
            if not claim["supporting_refs"]:
                unsupported_flag = True
            elif claim.get("validation_status") == "unsupported":
                unsupported_flag = True
    
    payload["structured_claims"] = claims
    payload["unsupported_claim_flag"] = unsupported_flag
    
    if unsupported_flag:
        if "validation" not in payload:
            payload["validation"] = {}
        payload["validation"]["status"] = "needs_review"
        payload["validation"]["ready_for_submission"] = False
    
    return payload

def build_markdown(payload: dict[str, Any]) -> str:
    tags = payload.get("tags", [])
    tags_line = " ".join(str(tag) for tag in tags if cleaned_text(str(tag))) if isinstance(tags, list) else str(tags)
    lines = [
        f"# Proposal: {payload['title']}",
        "",
        "## Problem",
        payload["problem"],
        "",
        "## Source",
        payload["source"],
        "",
        "## Layer",
        payload["layer"],
        "",
        "## Funding Call IDs",
        ", ".join(payload.get("funding_call_ids", [])) or str(payload.get("funding_call_id") or "Unknown"),
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
        "## Motivation",
        payload["motivation"],
        "",
        "## Proposed Method",
        payload["proposed_method"],
        "",
        "## Evaluation Plan",
        payload["evaluation_plan"],
        "",
        "## Funding Alignment",
        payload["funding_alignment"],
        "",
        "## Based On",
        payload["based_on"],
        "",
        "## Confidence",
        payload["confidence"],
        "",
        "## Validation",
        f"- validation_score: {payload.get('validation', {}).get('validation_score', 0)}",
        f"- ready_for_submission: {payload.get('validation', {}).get('ready_for_submission', False)}",
        f"- eligible_for_proposal_review: {payload.get('eligibility', {}).get('eligible_for_proposal_review', False)}",
        f"- novelty_status: {payload.get('eligibility', {}).get('novelty_status', 'unknown')}",
        f"- status: {payload.get('validation', {}).get('status', 'draft_requires_human_review')}",
        "",

        "## Tags",
        tags_line or "#Proposal",
        "",
        "## Provenance & Claims",
        f"- Unsupported Claims Flag: {payload.get('unsupported_claim_flag', False)}",

    ]
    return "\n".join(lines)


def unique_output_path(output_dir: Path, base_slug: str) -> Path:
    candidate = output_dir / f"{base_slug}.md"
    if not candidate.exists():
        return candidate
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return output_dir / f"{base_slug}-{stamp}.md"


def save_outputs(proposal_payloads: list[dict[str, Any]], output_dir: Path, area: str | None) -> tuple[list[Path], Path, Path]:
    target_dir = output_dir / slugify(normalize_area(area) or "multi-area")
    target_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[Path] = []
    for payload in proposal_payloads:
        md_path = unique_output_path(target_dir, slugify(payload["title"]))
        md_path.write_text(build_markdown(payload) + "\n", encoding="utf-8")
        saved_paths.append(md_path)

    summary_path = target_dir / "insights.md"
    json_path = target_dir / "proposals.json"
    area_counts = Counter(payload["research_area"] for payload in proposal_payloads)
    lines = [
        "# Proposal Insights",
        "",
        "## Problem",
        "Turn idea artifacts into structured proposal drafts with method, evaluation, and funding alignment.",
        "",
        "## Source",
        "Idea artifacts",
        "",
        "## Layer",
        "Proposal",
        "",
        "## Research Area",
        normalize_area(area) or "Multi-Area",
        "",
        "## Why Important",
        "Proposal drafts reduce the gap between promising ideas and sponsor-ready or lab-ready execution plans.",
        "",
        "## Existing Solutions",
        "Idea artifacts already frame the hypothesis and prototype scope, but proposal artifacts structure them into execution and evaluation plans.",
        "",
        "## Gap",
        "Without proposal drafts, ideas still require another round of manual structuring before they can be pitched or funded.",
        "",
        "## Idea",
        "Use the generated proposal notes as baseline scaffolds for sponsorship, grant applications, or internal project planning.",
        "",
        "## Feasibility",
        "High",
        "",
        "## Area Distribution",
    ]
    for key, value in sorted(area_counts.items()):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Generated Proposals"])
    for path, payload in zip(saved_paths, proposal_payloads):
        lines.append(f"- {payload['research_area']} | {payload['title']} | {path.resolve()}")
    lines.extend(["", "## Tags", "#Proposal #Intelligence #Insights", ""])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    json_path.write_text(json.dumps(proposal_payloads, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # A pipeline run should end with one clearly identified artifact rather than
    # forcing the operator to guess which proposal file is the final candidate.
    # This is still a research draft: validation flags are preserved and never
    # silently promoted to submission-ready status.
    ranked_payloads = sorted(
        proposal_payloads,
        key=lambda item: (
            float(item.get("validation", {}).get("validation_score", 0.0) or 0.0),
            1 if item.get("confidence") == "High" else 0,
            len(str(item.get("evaluation_plan", ""))),
        ),
        reverse=True,
    )
    final_payload = ranked_payloads[0] if ranked_payloads else None
    final_path = target_dir / "FINAL_PROPOSAL.md"
    if final_payload:
        final_body = build_markdown(final_payload)
        final_body = (
            "# FINAL PROPOSAL CANDIDATE\n\n"
            "> Generated by the RIF Proposal Agent. This is the strongest proposal candidate from this run; \n            external novelty validation, sponsor-specific compliance checks, and human review remain required.\n\n"
            + final_body
        )
        final_path.write_text(final_body + "\n", encoding="utf-8")
    return saved_paths, summary_path, json_path


def _proposal_gate(record: dict[str, Any], funding_context: Any) -> tuple[bool, dict[str, Any]]:
    validation = record.get("validation") if isinstance(record.get("validation"), dict) else {}
    novelty = str(validation.get("novelty_status", "")).lower()
    if novelty not in {"externally_validated", "no_overlap_in_search"}:
        return False, {"reason": "external novelty validation is not complete", "novelty_status": novelty}
    search = validation.get("novelty_search") if isinstance(validation.get("novelty_search"), dict) else {}
    if str(search.get("novelty_status", "")).upper() == "OVERLAP_FOUND":
        return False, {"reason": "external search found overlap", "novelty_status": "OVERLAP_FOUND"}
    combined = " ".join(str(record.get(k, "")) for k in ("problem","gap","idea","why_important","existing_solutions","source_inputs"))
    gate = mission_relevance({"title": record.get("title", ""), "problem_statement": combined, "context_summary": combined, "source_type": "proposal", "evidence_type": "proposal", "keywords": []}, funding_context)
    if not gate.get("passed") or float(gate.get("score", 0.0)) < 0.12:
        return False, {"reason": "proposal content is not sufficiently aligned to the selected funding mission", "mission_relevance": gate}
    
    # Strict Semantic Alignment Check
    try:
        from core.llm_provider import generate
        import json
        import re
        prompt = f"""You are a strict funding alignment verifier.
Decide if the proposed research idea actually addresses the specific deliverables and scope of the funding grant.
Do NOT approve just because they share domain keywords (e.g., both are 'Web3' or 'Crypto').
The core deliverables of the funding grant MUST explicitly match or require the technology in the idea.

Funding Grant:
Title: {funding_context.call_title}
Deliverables: {funding_context.deliverables}

Idea:
Title: {record.get('title')}
Problem: {combined[:1000]}

Return JSON only: {{"aligned": true/false, "reason": "strict explanation"}}"""
        res = generate(prompt)
        match = re.search(r"\{.*\}", res, flags=re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            aligned_val = str(parsed.get("aligned", "")).lower()
            if aligned_val != "true":
                gate["llm_rejected"] = True
                gate["llm_reason"] = parsed.get("reason")
                return False, {"reason": f"LLM rejected alignment: {parsed.get('reason')}", "mission_relevance": gate}
    except Exception as e:
        LOGGER.warning("LLM alignment check failed: %s", e)

    return True, {"reason": "proposal passed evidence/novelty/mission gate", "mission_relevance": gate}

def run_agent(mode: str, area: str | None = None, input_data: dict[str, Any] | None = None, funding_context=None) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    payload = input_data or {}
    output_dir = Path(payload.get("output_dir", DEFAULT_OUTPUT_DIR))
    idea_dir = Path(payload.get("idea_dir", DEFAULT_IDEA_DIR))

    LOGGER.info("Starting proposal agent run: mode=%s area=%s", mode, area)
    try:
        warnings: list[str] = []
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
            title = cleaned_text(str(payload.get("title", ""))) or "Manual Proposal Input"
            source = cleaned_text(str(payload.get("source", ""))) or "manual://proposal/manual-input"
            proposal_payloads = [build_manual_payload(text=text, title=title, source=source, area=area)]
            items_processed = 1
        else:
            records = load_idea_records(idea_dir)
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
                records = filter_idea_records(records, area=area, source_url=url)
            elif mode == "configured_scan":
                records = filter_idea_records(records, area=area)
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
                    warnings=["No idea artifacts matched the proposal request."],
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                )

            # A synthesis run can contain many evidence clusters. Proposal generation
            # is intentionally bounded to the strongest few candidates so cluster count
            # cannot turn directly into proposal count. Unselected ideas remain available
            # in the ideas artifacts for review.
            def _idea_rank(record: dict[str, Any]) -> tuple[float, float, int]:
                validation = record.get("validation") if isinstance(record.get("validation"), dict) else {}
                score = float(validation.get("validation_score", 0.0) or 0.0)
                evidence = len(record.get("source_inputs", []) or []) if isinstance(record.get("source_inputs"), list) else 0
                return (score, float(record.get("confidence_score", 0.0) or 0.0), evidence)
            records = sorted(records, key=_idea_rank, reverse=True)[:3]
            eligible_records = []
            ctx = payload.get("__selected_call_context")
            for record in records:
                if ctx is None:
                    warnings.append(f"Rejected idea {record.get('title')}: selected funding context is missing.")
                    continue
                passed, gate = _proposal_gate(record, ctx)
                record["proposal_gate"] = gate
                if passed:
                    eligible_records.append(record)
                else:
                    warnings.append(f"Rejected idea {record.get('title')}: {gate.get('reason')}")
            records = eligible_records
            proposal_payloads = []
            for record in records:
                try:
                    proposal_payloads.append(build_proposal_payload_from_idea(record, ctx))
                except Exception as exc:
                    warnings.append(f"Skipped idea artifact {record.get('_source_file') or record.get('based_on')}: {exc}")
            items_processed = len(records)
            warnings.append(f"Proposal generation bounded to top {len(records)} candidate(s) after idea ranking.")

        if not proposal_payloads:
            return create_partial_response(
                agent=AGENT_NAME,
                layer=LAYER_NAME,
                mode=mode,
                area=area,
                items_processed=items_processed,
                items_saved=0,
                outputs=[],
                warnings=warnings or ["No proposal payloads could be built."],
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        saved_paths, summary_path, json_path = save_outputs(proposal_payloads, output_dir, area)
        outputs: list[dict[str, Any]] = []
        for md_path, proposal_payload in zip(saved_paths, proposal_payloads):
            outputs.append(
                {
                    "title": proposal_payload["title"],
                    "problem": proposal_payload["problem"],
                    "problem_signature": (
                        f"{proposal_payload['research_area']} | proposal | "
                        f"{slugify(proposal_payload['title']).replace('-', '_')}"
                    ),
                    "confidence": proposal_payload["confidence"],
                    "source": proposal_payload["source"],
                    "markdown_path": str(md_path.resolve()),
                }
            )

        response = create_success_response(
            agent=AGENT_NAME,
            layer=LAYER_NAME,
            mode=mode,
            area=area,
            items_processed=items_processed,
            items_saved=len(outputs),
            outputs=outputs,
            warnings=warnings + ["Proposal agent saved intelligence artifacts under outputs/proposals/."],
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        response["metadata"]["proposal_summary"] = {
            "proposals_created": len(outputs),
            "summary_markdown": str(summary_path.resolve()),
            "proposals_json": str(json_path.resolve()),
            "output_dir": str((output_dir / slugify(normalize_area(area) or 'multi-area')).resolve()),
            "final_proposal": str((output_dir / slugify(normalize_area(area) or 'multi-area') / "FINAL_PROPOSAL.md").resolve()),
        }
        return response
    except Exception as exc:
        LOGGER.exception("Proposal agent failed during run_agent")
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
    parser = argparse.ArgumentParser(description="Run the proposal intelligence agent.")
    parser.add_argument("--mode", choices=["configured_scan", "manual_url", "manual_text"], default="configured_scan")
    parser.add_argument("--area")
    parser.add_argument("--url")
    parser.add_argument("--text")
    parser.add_argument("--title")
    parser.add_argument("--source")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--idea-dir", default=str(DEFAULT_IDEA_DIR))
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)
    payload: dict[str, Any] = {
        "output_dir": args.output_dir,
        "idea_dir": args.idea_dir,
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
