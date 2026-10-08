from __future__ import annotations

import argparse
import json
import dataclasses
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
from core.novelty_search import perform_novelty_search

LOGGER = logging.getLogger("rif.idea_agent")

DEFAULT_OUTPUT_DIR = Path("outputs/ideas")
DEFAULT_OUTPUTS_ROOT = Path("outputs")
DEFAULT_MANIFEST_DIR = Path("outputs/synthesis/manifest")
AGENT_NAME = "idea"
LAYER_NAME = "Intelligence"

TOPIC_HINTS = {
    "oracle": ("oracle", "data feed", "proof of reserve"),
    "compliance": ("compliance", "reporting", "disclosure", "assurance"),
    "identity": ("identity", "credential", "did", "authentication"),
    "privacy": ("privacy", "zero-knowledge", "zk", "verifier"),
    "settlement": ("settlement", "payment rails", "payments"),
    "stablecoin": ("stablecoin", "reserve", "depeg", "vault"),
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

AREA_DATASET_HINTS = {
    "RWA": "Asset registries, reserve attestations, oracle feeds, settlement logs, and tokenized asset telemetry.",
    "ESG": "Registry exports, MRV datasets, emissions feeds, verifier records, and carbon-credit lifecycle events.",
    "ZK-IoV": "Mobility traces, simulator outputs, proving benchmarks, verifier performance metrics, and V2X message logs.",
    "DID": "Credential issuance logs, conformance suites, interoperability fixtures, wallet traces, and privacy test cases.",
    "DePIN": "Node uptime data, device telemetry, explorer metrics, storage retrieval traces, and service-level reliability logs.",
    "MEV": "Block/transaction ordering traces, relay data, bundle outcomes, builder telemetry, and execution simulations.",
    "Stablecoins": "Reserve attestations, vault telemetry, payment-rail events, explorer traces, and depeg/liquidity histories.",
    "DigitalHealthCPS": "Clinical workflow traces, de-identified patient telemetry, device logs, validation datasets, hospital deployment metrics, safety events, and regulatory evaluation records.",
}


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def cleaned_text(value: str) -> str:
    return " ".join((value or "").split()).strip()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "item"


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
        if synthesized_file and Path(synthesized_file).exists():
            live_entries.append(entry)
    return live_entries


def filter_manifest_entries(entries: list[dict[str, Any]], *, area: str | None = None, source_url: str | None = None) -> list[dict[str, Any]]:
    normalized_area = normalize_area(area) if area else None
    filtered: list[dict[str, Any]] = []
    for entry in entries:
        if normalized_area and normalize_area(entry.get("area")) != normalized_area:
            continue
        if source_url:
            source_urls = [cleaned_text(str(item)) for item in entry.get("source_urls", []) if cleaned_text(str(item))]
            synthesized_file = cleaned_text(str(entry.get("synthesized_file", "")))
            if cleaned_text(source_url) not in source_urls and cleaned_text(source_url) != synthesized_file:
                continue
        filtered.append(entry)
    return filtered


def derive_title(base_title: str, area: str) -> str:
    clean = cleaned_text(base_title)
    if clean.lower().startswith("synthesis:"):
        clean = clean.split(":", 1)[1].strip()
    if clean.lower().endswith("research synthesis"):
        clean = clean[: -len("research synthesis")].strip(" -")
    return f"{clean or area} Research Idea"


def extract_topics(text: str) -> list[str]:
    lowered = text.lower()
    topics: list[str] = []
    for topic, keywords in TOPIC_HINTS.items():
        if any(keyword in lowered for keyword in keywords):
            topics.append(topic)
    return topics


def infer_hypothesis(problem: str, gap: str, area: str) -> str:
    focus = cleaned_text(problem) or cleaned_text(gap)
    if area == "Stablecoins":
        return f"If we add verifiable assurance and monitoring around {focus.lower()}, then stable-value infrastructure can become safer for routine economic coordination."
    if area == "ESG":
        return f"If we make {focus.lower()} machine-verifiable across registry and MRV flows, then carbon-tokenization systems can reduce trust gaps and double-counting risk."
    if area == "MEV":
        return f"If we expose measurable controls around {focus.lower()}, then builders and searchers can reduce hidden market-structure risk and improve fairness diagnostics."
    if area == "DigitalHealthCPS":
        return f"If we design and validate a cyber-physical healthcare intervention around {focus.lower()}, then the project can move from prototype evidence toward clinically meaningful deployment readiness."
    return f"If we design a focused intervention around {focus.lower()}, then the observed gap can be turned into a testable prototype and benchmarked research program."


def infer_experiment_direction(area: str, sections: dict[str, str]) -> str:
    dataset_hint = AREA_DATASET_HINTS.get(area, "Relevant structured evidence, benchmark data, and system traces.")
    idea = cleaned_text(sections.get("Idea", ""))
    gap = cleaned_text(sections.get("Gap", ""))
    return (
        f"Build a narrow evaluation around the proposed direction: define 2-3 measurable assurance or performance metrics, "
        f"test them against {dataset_hint.lower()} "
        f"and compare the baseline described by the current gap ({gap[:180] or 'unresolved ecosystem fragmentation'}) "
        f"with a prototype implementation ({idea[:180] or 'the proposed intervention'})."
    )


def infer_prototype_scope(area: str, sections: dict[str, str]) -> str:
    idea = cleaned_text(sections.get("Idea", ""))
    if area == "Stablecoins":
        return (
            "Prototype a compact assurance dashboard and scoring service that joins reserve or vault telemetry, explorer traces, "
            "and service-level evidence into one machine-checkable risk view."
        )
    if area == "ESG":
        return (
            "Prototype a registry-to-MRV reconciliation tool that flags inconsistencies, missing attestations, and weak carbon-credit provenance."
        )
    if area == "MEV":
        return (
            "Prototype a replay and monitoring tool that compares transaction-ordering outcomes across relay, builder, and block data."
        )
    if area == "DigitalHealthCPS":
        return (
            "Prototype a validated digital-health CPS workflow that joins device or software telemetry, clinical-user feedback, safety checks, and deployment-readiness metrics."
        )
    if idea:
        return f"Prototype the narrowest demonstrable slice of this direction first: {idea[:260]}"
    return "Prototype one end-to-end, testable workflow that turns the synthesized gap into a measurable engineering intervention."


def infer_collaborators(area: str, entry: dict[str, Any], sections: dict[str, str]) -> str:
    layers = ", ".join(entry.get("layers_covered", [])) or "ecosystem stakeholders"
    source_urls = [cleaned_text(str(item)) for item in entry.get("source_urls", []) if cleaned_text(str(item))]
    domain_hint = ""
    if source_urls:
        parts = source_urls[0].split("/")
        if len(parts) > 2:
            domain_hint = parts[2]
        else:
            domain_hint = source_urls[0]
    existing = cleaned_text(sections.get("Existing Solutions", ""))
    actor_bits = []
    if domain_hint:
        actor_bits.append(domain_hint)
    if existing:
        actor_bits.append(existing[:220])
    joined = "; ".join(actor_bits) if actor_bits else "current ecosystem operators and maintainers"
    return f"Likely collaborators or sponsors include the {layers} layers already represented here, especially {joined}."


def validate_idea_payload(problem: str, gap: str, hypothesis: str, experiment: str, feasibility: str, entry: dict[str, Any], existing_solutions: str = "") -> dict[str, Any]:
    checks = {
        "problem_present": bool(problem),
        "gap_present": bool(gap),
        "testable_hypothesis": hypothesis.lower().startswith("if ") and " then " in hypothesis.lower(),
        "measurable_experiment": any(term in experiment.lower() for term in ("metric", "benchmark", "compare", "baseline", "measure")),
        "feasibility_stated": bool(feasibility),
        "evidence_linked": bool(entry.get("source_urls") or entry.get("input_files") or entry.get("synthesized_file")),
    }
    passed = sum(bool(v) for v in checks.values())
    # Novelty cannot be proven from the local synthesis artifact alone.
    novelty_status = "needs_external_validation"
    existing = cleaned_text(existing_solutions).lower()
    if any(term in existing for term in ("already", "existing", "partial efforts", "current solutions")):
        novelty_status = "existing_solution_overlap_possible"
    return {
        "checks": checks,
        "validation_score": round(passed / len(checks), 3),
        # A local synthesis can be structurally ready for review, but it can never
        # be marked proposal-ready until novelty has been externally validated.
        "ready_for_proposal": passed == len(checks) and novelty_status == "externally_validated",
        "ready_for_external_review": passed == len(checks),
        "novelty_status": novelty_status,
        "novelty_claim": "Novelty has not been established; external literature/patent/open-source validation is required before promotion.",
    }


def build_idea_payload_from_synthesis(entry: dict[str, Any]) -> dict[str, Any]:
    synthesis_path = Path(str(entry.get("synthesized_file", "")))
    text = synthesis_path.read_text(encoding="utf-8")
    sections = parse_markdown_sections(text)
    area = normalize_area(entry.get("area")) or normalize_area(sections.get("Research Area")) or "Unscoped"
    title = derive_title(str(entry.get("title", "")), area)
    problem = cleaned_text(sections.get("Problem", ""))
    why_important = cleaned_text(sections.get("Why Important", ""))
    existing_solutions = cleaned_text(sections.get("Existing Solutions", ""))
    gap = cleaned_text(sections.get("Gap", ""))
    seed_idea = cleaned_text(sections.get("Idea", ""))
    feasibility = cleaned_text(sections.get("Feasibility", "")) or "Moderate"
    source_inputs = sections.get("Source Inputs", "")
    layers = entry.get("layers_covered", []) or [cleaned_text(sections.get("Layer or Layers", ""))]
    source_urls = [cleaned_text(str(item)) for item in entry.get("source_urls", []) if cleaned_text(str(item))]
    confidence_label = str(entry.get("ranking", {}).get("label") or entry.get("confidence", {}).get("label") or "Unassessed").strip() or "Unassessed"

    hypothesis = infer_hypothesis(problem, gap, area)
    experiment_direction = infer_experiment_direction(area, sections)
    prototype_scope = infer_prototype_scope(area, sections)
    dataset_need = AREA_DATASET_HINTS.get(area, "Area-specific evaluation datasets, telemetry, and benchmark fixtures.")
    collaborators = infer_collaborators(area, entry, sections)
    enriched_idea = (
        f"{seed_idea} "
        f"The near-term research idea is to formalize this into a bounded hypothesis, measurable prototype, and evaluation plan rather than leaving it as a broad ecosystem recommendation."
    ).strip()
    topic_tags = [f"#{topic}" for topic in extract_topics(" ".join([problem, gap, seed_idea, enriched_idea]))]
    validation = validate_idea_payload(problem, gap, hypothesis, experiment_direction, feasibility, entry, existing_solutions)
    tags = list(
        dict.fromkeys(
            [
                "#Idea",
                "#Intelligence",
                f"#{area.replace('-', '')}",
                *[f"#layer-{slugify(layer)}" for layer in layers if cleaned_text(str(layer))],
                *topic_tags,
            ]
        )
    )
    return {
        "title": title,
        "problem": problem,
        "funding_call_id": entry.get("funding_call_id"),
        "funding_call_ids": entry.get("funding_call_ids", []),
        "source": source_urls[0] if source_urls else str(synthesis_path.resolve()),
        "layer": "Idea",
        "research_area": area,
        "why_important": why_important,
        "existing_solutions": existing_solutions,
        "gap": gap,
        "idea": enriched_idea,
        "feasibility": feasibility,
        "hypothesis": hypothesis,
        "experiment_direction": experiment_direction,
        "prototype_scope": prototype_scope,
        "expected_dataset_need": dataset_need,
        "likely_collaborators": collaborators,
        "based_on": str(synthesis_path.resolve()),
        "source_inputs": source_inputs,
        "supporting_layers": layers,
        "confidence": confidence_label if confidence_label in {"High", "Medium", "Low"} else "Unassessed",
        "tags": tags,
        "validation": validation,
    }


def attach_external_novelty_validation(idea_payload: dict[str, Any]) -> dict[str, Any]:
    problem = cleaned_text(str(idea_payload.get("problem", "")))
    validation = idea_payload.get("validation") if isinstance(idea_payload.get("validation"), dict) else {}
    novelty = perform_novelty_search(problem, use_external=True) if problem else {
        "novelty_status": "UNKNOWN", "novelty_confidence": "Unassessed",
        "search_coverage": 0.0, "search_mode": "unavailable", "similar_work": []
    }
    validation["novelty_search"] = novelty
    if novelty.get("search_mode") == "external" and novelty.get("query_executed"):
        validation["novelty_status"] = "externally_validated"
        validation["novelty_claim"] = "External search completed; novelty is not proven and any overlap must be reviewed."
        validation["ready_for_proposal"] = validation.get("ready_for_external_review", False) and novelty.get("novelty_status") != "OVERLAP_FOUND"
    else:
        validation["novelty_status"] = "unknown"
        validation["ready_for_proposal"] = False
    idea_payload["validation"] = validation
    return idea_payload


def build_manual_payload(*, text: str, title: str, source: str, area: str | None) -> dict[str, Any]:
    normalized_area = normalize_area(area) or "Unscoped"
    clean = cleaned_text(text)
    hypothesis = infer_hypothesis(clean, clean, normalized_area)
    experiment_direction = (
        f"Start with a constrained benchmark or prototype that tests whether the stated problem can be turned into measurable improvement signals in {normalized_area}."
    )
    prototype_scope = "Build the smallest end-to-end demonstrator that proves the core technical claim and produces measurable evaluation output."
    return {
        "title": derive_title(title or "Manual Input", normalized_area),
        "problem": clean[:400],
        "source": source or "manual://idea/manual-input",
        "layer": "Idea",
        "research_area": normalized_area,
        "why_important": "Manual input suggests a candidate problem worth turning into a testable research idea.",
        "existing_solutions": "Existing solutions need to be investigated from adjacent synthesis, company, funding, and open-source evidence.",
        "gap": clean[:400],
        "idea": "Turn the manual problem statement into a scoped prototype and evaluation workflow.",
        "feasibility": "Moderate",
        "hypothesis": hypothesis,
        "experiment_direction": experiment_direction,
        "prototype_scope": prototype_scope,
        "expected_dataset_need": AREA_DATASET_HINTS.get(normalized_area, "Area-specific evaluation datasets and telemetry."),
        "likely_collaborators": "Likely collaborators depend on the surrounding ecosystem and should be resolved from follow-on synthesis.",
        "based_on": source or "Manual",
        "source_inputs": source or "Manual",
        "supporting_layers": ["Manual"],
        "confidence": "Medium",
        "tags": ["#Idea", "#Manual", f"#{normalized_area.replace('-', '')}"],
    }


def build_markdown(payload: dict[str, Any]) -> str:
    tags = payload.get("tags", [])
    if isinstance(tags, list):
        tags_line = " ".join(str(tag) for tag in tags if cleaned_text(str(tag)))
    else:
        tags_line = str(tags)
    supporting_layers = payload.get("supporting_layers", [])
    layers_text = ", ".join(str(item) for item in supporting_layers if cleaned_text(str(item))) or "Unknown"
    lines = [
        f"# Idea: {payload['title']}",
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
        "## Hypothesis",
        payload["hypothesis"],
        "",
        "## Experiment Direction",
        payload["experiment_direction"],
        "",
        "## Prototype Scope",
        payload["prototype_scope"],
        "",
        "## Expected Dataset / Benchmark Need",
        payload["expected_dataset_need"],
        "",
        "## Likely Collaborators / Sponsors",
        payload["likely_collaborators"],
        "",
        "## Based On",
        payload["based_on"],
        "",
        "## Supporting Layers",
        layers_text,
        "",
        "## Source Inputs",
        payload["source_inputs"],
        "",
        "## Confidence",
        payload["confidence"],
        "",
        "## Validation",
        f"- validation_score: {payload.get('validation', {}).get('validation_score', 0)}",
        f"- novelty_status: {payload.get('validation', {}).get('novelty_status', 'unknown')}",
        f"- ready_for_proposal: {payload.get('validation', {}).get('ready_for_proposal', False)}",
        f"- novelty_claim: {payload.get('validation', {}).get('novelty_claim', '')}",
        "",
        "## Tags",
        tags_line or "#Idea",
        "",
    ]
    return "\n".join(lines)


def unique_output_path(output_dir: Path, base_slug: str) -> Path:
    candidate = output_dir / f"{base_slug}.md"
    if not candidate.exists():
        return candidate
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return output_dir / f"{base_slug}-{stamp}.md"


def _json_safe(value: Any) -> Any:
    """Convert nested dataclass/Path/set values to JSON-safe primitives."""
    if dataclasses.is_dataclass(value):
        return {k: _json_safe(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return value

def save_outputs(idea_payloads: list[dict[str, Any]], output_dir: Path, area: str | None) -> tuple[list[Path], Path, Path]:
    target_dir = output_dir / slugify(normalize_area(area) or "multi-area")
    target_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[Path] = []
    for payload in idea_payloads:
        md_path = unique_output_path(target_dir, slugify(payload["title"]))
        md_path.write_text(build_markdown(payload) + "\n", encoding="utf-8")
        saved_paths.append(md_path)

    summary_path = target_dir / "insights.md"
    json_path = target_dir / "ideas.json"
    idea_counter = Counter(payload["research_area"] for payload in idea_payloads)
    lines = [
        "# Idea Insights",
        "",
        "## Problem",
        "Turn synthesized ecosystem problems into actionable research and prototype ideas.",
        "",
        "## Source",
        "Synthesis artifacts",
        "",
        "## Layer",
        "Idea",
        "",
        "## Research Area",
        normalize_area(area) or "Multi-Area",
        "",
        "## Why Important",
        "Idea generation helps convert synthesis into testable next-step research directions rather than leaving insights as passive notes.",
        "",
        "## Existing Solutions",
        "Synthesis artifacts already summarize problems and gaps, but idea artifacts convert them into hypotheses, prototype scopes, and experiment directions.",
        "",
        "## Gap",
        "Without idea artifacts, strong synthesized problems still require manual interpretation before they can become experiments or proposals.",
        "",
        "## Idea",
        "Use the generated idea notes as inputs to proposal generation, sponsor alignment, and prototype planning.",
        "",
        "## Feasibility",
        "High",
        "",
        "## Area Distribution",
    ]
    for key, value in sorted(idea_counter.items()):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Generated Ideas"])
    for path, payload in zip(saved_paths, idea_payloads):
        lines.append(f"- {payload['research_area']} | {payload['title']} | {path.resolve()}")
    lines.extend(["", "## Tags", "#Idea #Intelligence #Insights", ""])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    json_path.write_text(json.dumps(_json_safe(idea_payloads), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return saved_paths, summary_path, json_path


def run_agent(mode: str, area: str | None = None, input_data: dict[str, Any] | None = None, funding_context=None) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    payload = input_data or {}
    output_dir = Path(payload.get("output_dir", DEFAULT_OUTPUT_DIR))
    manifest_dir = Path(payload.get("manifest_dir", DEFAULT_MANIFEST_DIR))

    LOGGER.info("Starting idea agent run: mode=%s area=%s", mode, area)
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
            title = cleaned_text(str(payload.get("title", ""))) or "Manual Idea Input"
            source = cleaned_text(str(payload.get("source", ""))) or "manual://idea/manual-input"
            idea_payloads = [build_manual_payload(text=text, title=title, source=source, area=area)]
            items_processed = 1
        else:
            entries = load_manifest_entries(manifest_dir)
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
                entries = filter_manifest_entries(entries, area=area, source_url=url)
            elif mode == "configured_scan":
                entries = filter_manifest_entries(entries, area=area)
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

            if not entries:
                return create_partial_response(
                    agent=AGENT_NAME,
                    layer=LAYER_NAME,
                    mode=mode,
                    area=area,
                    items_processed=0,
                    items_saved=0,
                    outputs=[],
                    warnings=["No synthesis artifacts matched the idea request."],
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                )

            idea_payloads = []
            for entry in entries:
                try:
                    idea_payload = build_idea_payload_from_synthesis(entry)
                    if bool(payload.get("external_novelty_validation", False)):
                        idea_payload = attach_external_novelty_validation(idea_payload)
                    idea_payloads.append(idea_payload)
                except Exception as exc:
                    warnings.append(f"Skipped synthesis artifact {entry.get('synthesized_file')}: {exc}")
            items_processed = len(entries)

        if not idea_payloads:
            return create_partial_response(
                agent=AGENT_NAME,
                layer=LAYER_NAME,
                mode=mode,
                area=area,
                items_processed=items_processed,
                items_saved=0,
                outputs=[],
                warnings=warnings or ["No idea payloads could be built."],
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        saved_paths, summary_path, json_path = save_outputs(idea_payloads, output_dir, area)
        outputs = []
        for md_path, idea_payload in zip(saved_paths, idea_payloads):
            outputs.append(
                {
                    "title": idea_payload["title"],
                    "problem": idea_payload["problem"],
                    "problem_signature": (
                        f"{idea_payload['research_area']} | idea | "
                        f"{slugify(idea_payload['title']).replace('-', '_')}"
                    ),
                    "confidence": idea_payload["confidence"],
                    "source": idea_payload["source"],
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
            warnings=warnings + ["Idea agent saved intelligence artifacts under outputs/ideas/."],
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        response["metadata"]["idea_summary"] = {
            "ideas_created": len(outputs),
            "summary_markdown": str(summary_path.resolve()),
            "ideas_json": str(json_path.resolve()),
            "output_dir": str((output_dir / slugify(normalize_area(area) or 'multi-area')).resolve()),
        }
        return response
    except Exception as exc:
        LOGGER.exception("Idea agent failed during run_agent")
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
    parser = argparse.ArgumentParser(description="Run the idea intelligence agent.")
    parser.add_argument("--mode", choices=["configured_scan", "manual_url", "manual_text"], default="configured_scan")
    parser.add_argument("--area")
    parser.add_argument("--url")
    parser.add_argument("--text")
    parser.add_argument("--title")
    parser.add_argument("--source")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--manifest-dir", default=str(DEFAULT_MANIFEST_DIR))
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(args.log_level)
    payload: dict[str, Any] = {
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
