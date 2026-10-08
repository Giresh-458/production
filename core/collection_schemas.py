from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class CollectionField:
    name: str
    label: str
    required: bool = False


@dataclass(frozen=True, slots=True)
class CollectionSchema:
    agent_name: str
    schema_name: str
    version: str
    fields: tuple[CollectionField, ...]
    min_key_paragraphs: int = 1
    min_evidence_snippets: int = 1
    min_excerpt_windows: int = 0
    min_section_headings_or_bullets: int = 0
    allow_additional_fields: bool = True
    notes: tuple[str, ...] = field(default_factory=tuple)


def _schema(
    agent_name: str,
    schema_name: str,
    fields: list[tuple[str, str, bool]],
    notes: list[str],
    *,
    min_key_paragraphs: int = 1,
    min_evidence_snippets: int = 1,
    min_excerpt_windows: int = 0,
    min_section_headings_or_bullets: int = 0,
) -> CollectionSchema:
    return CollectionSchema(
        agent_name=agent_name,
        schema_name=schema_name,
        version="v1",
        fields=tuple(CollectionField(name=name, label=label, required=required) for name, label, required in fields),
        min_key_paragraphs=min_key_paragraphs,
        min_evidence_snippets=min_evidence_snippets,
        min_excerpt_windows=min_excerpt_windows,
        min_section_headings_or_bullets=min_section_headings_or_bullets,
        allow_additional_fields=True,
        notes=tuple(notes),
    )


COLLECTION_SCHEMAS: dict[str, CollectionSchema] = {
    "literature": _schema(
        "literature",
        "literature-paper-record",
        [
            ("published", "Publication Date", False),
            ("authors", "Authors", True),
            ("score", "Combined Ranking Score", True),
            ("relevance_score", "Relevance Score", True),
            ("impact_score", "Impact Score", True),
            ("recency_score", "Recency Score", True),
            ("problem_clarity_score", "Problem Clarity Score", True),
        ],
        [
            "Preserve ranking signals and bibliographic detail for paper selection.",
            "Raw abstract/summary remains in Raw Content rather than the agent-specific body.",
        ],
        min_key_paragraphs=1,
        min_evidence_snippets=2,
    ),
    "funding": _schema(
        "funding",
        "funding-call-record",
        [
            ("organization", "Funding Organization", True),
            ("source_type", "Source Type", True),
            ("year", "Year", True),
        ],
        [
            "Funding artifacts should keep funding-program context, source type, and timing signals.",
        ],
        min_key_paragraphs=1,
        min_evidence_snippets=1,
        min_excerpt_windows=1,
        min_section_headings_or_bullets=0,
    ),
    "lab": _schema(
        "lab",
        "lab-activity-record",
        [
            ("institution", "Institution", True),
            ("base_url", "Lab Base URL", True),
            ("source_type", "Source Type", True),
            ("pages", "Collected Pages", True),
        ],
        [
            "Lab artifacts should retain the collected page list rather than collapsing everything into one summary.",
        ],
        min_key_paragraphs=1,
        min_evidence_snippets=1,
        min_excerpt_windows=0,
        min_section_headings_or_bullets=0,
    ),
    "company": _schema(
        "company",
        "company-use-case-record",
        [
            ("source_type", "Source Type", True),
            ("focus", "Focus", True),
        ],
        [
            "Company artifacts should preserve use-case focus and source-type context for later enterprise/native comparisons.",
        ],
        min_key_paragraphs=1,
        min_evidence_snippets=2,
        min_excerpt_windows=1,
        min_section_headings_or_bullets=1,
    ),
    "regulation": _schema(
        "regulation",
        "regulation-standard-record",
        [
            ("issuing_body", "Issuing Body", True),
            ("source_type", "Source Type", True),
            ("evidence_type", "Evidence Type", True),
            ("focus", "Focus", True),
        ],
        [
            "Regulation artifacts should preserve issuing body, evidence type, and compliance focus before normalization.",
        ],
        min_key_paragraphs=1,
        min_evidence_snippets=1,
        min_excerpt_windows=0,
        min_section_headings_or_bullets=0,
    ),
    "opensource": _schema(
        "opensource",
        "opensource-signal-record",
        [
            ("source_type", "Source Type", True),
            ("evidence_type", "Evidence Type", True),
            ("focus", "Focus", True),
        ],
        [
            "Open-source artifacts should preserve whether the evidence came from docs, issues, roadmap pages, or discussions.",
        ],
        min_key_paragraphs=1,
        min_evidence_snippets=2,
        min_excerpt_windows=1,
        min_section_headings_or_bullets=1,
    ),
    "practitioner": _schema(
        "practitioner",
        "practitioner-signal-record",
        [
            ("source_type", "Source Type", True),
            ("evidence_type", "Evidence Type", True),
            ("practitioner_role_hint", "Practitioner Role Hint", True),
            ("focus", "Focus", True),
        ],
        [
            "Practitioner artifacts should preserve role hints and source type so later synthesis can distinguish operator pain from commentary.",
        ],
        min_key_paragraphs=1,
        min_evidence_snippets=2,
        min_excerpt_windows=1,
    ),
    "investment": _schema(
        "investment",
        "investment-signal-record",
        [
            ("investor_or_organization", "Investor / Organization", True),
            ("source_type", "Source Type", True),
            ("evidence_type", "Evidence Type", True),
            ("focus", "Focus", True),
        ],
        [
            "Investment artifacts should retain organization and source-type context for capital-flow analysis.",
        ],
        min_key_paragraphs=1,
        min_evidence_snippets=2,
        min_excerpt_windows=1,
        min_section_headings_or_bullets=1,
    ),
    "failure": _schema(
        "failure",
        "failure-incident-record",
        [
            ("source_type", "Source Type", True),
            ("evidence_type", "Evidence Type", True),
            ("focus", "Focus", True),
            ("incident_date", "Incident Date", False),
            ("attack_vector", "Attack Vector", False),
            ("evidence_strength", "Evidence Strength", False),
        ],
        [
            "Failure artifacts should retain incident timing, mechanism, and source strength before root-cause synthesis.",
        ],
        min_key_paragraphs=1,
        min_evidence_snippets=2,
        min_excerpt_windows=1,
    ),
    "data_availability": _schema(
        "data_availability",
        "data-availability-record",
        [
            ("source_type", "Source Type", True),
            ("evidence_type", "Evidence Type", True),
            ("focus", "Focus", True),
            ("access_type", "Access Type", False),
            ("benchmark_gap", "Benchmark Gap", False),
            ("reproducibility", "Reproducibility", False),
        ],
        [
            "Data-availability artifacts should preserve access, benchmark coverage, and reproducibility signals.",
        ],
        min_key_paragraphs=1,
        min_evidence_snippets=1,
        min_excerpt_windows=0,
        min_section_headings_or_bullets=0,
    ),
    "hackathon": _schema(
        "hackathon",
        "hackathon-challenge-record",
        [
            ("source_type", "Source Type", True),
            ("evidence_type", "Evidence Type", True),
            ("sponsor_or_organizer", "Sponsor / Organizer", True),
            ("focus", "Focus", True),
        ],
        [
            "Hackathon artifacts should preserve track/bounty context and organizer information for later prototype synthesis.",
        ],
        min_key_paragraphs=1,
        min_evidence_snippets=2,
        min_excerpt_windows=1,
        min_section_headings_or_bullets=1,
    ),
    "expert": _schema(
        "expert",
        "expert-profile-record",
        [
            ("source_type", "Source Type", True),
            ("evidence_type", "Evidence Type", True),
            ("expert_name_hint", "Expert Name Hint", True),
            ("affiliation_hint", "Affiliation Hint", True),
            ("role_hint", "Role Hint", True),
            ("focus", "Focus", True),
            ("collaboration_fit_score", "Collaboration Fit Score", False),
            ("recent_publication_signal", "Recent Publication Signal", False),
        ],
        [
            "Expert artifacts should preserve person, affiliation, recent activity, and proposal-fit signals before people-network synthesis.",
        ],
        min_key_paragraphs=1,
        min_evidence_snippets=1,
        min_excerpt_windows=0,
        min_section_headings_or_bullets=0,
    ),
}


def get_collection_schema(agent_name: str) -> CollectionSchema | None:
    return COLLECTION_SCHEMAS.get(agent_name)


def validate_agent_specific_body(agent_name: str, body: dict[str, Any]) -> list[str]:
    schema = get_collection_schema(agent_name)
    if schema is None:
        return []

    errors: list[str] = []
    for field in schema.fields:
        if not field.required:
            continue
        value = body.get(field.name)
        if value is None:
            errors.append(f"{agent_name} body missing required field: {field.name}")
            continue
        if isinstance(value, str) and not value.strip():
            errors.append(f"{agent_name} body missing required field: {field.name}")
        elif isinstance(value, list) and not value:
            errors.append(f"{agent_name} body missing required field: {field.name}")

    if not schema.allow_additional_fields:
        allowed = {field.name for field in schema.fields}
        extra_fields = sorted(set(body) - allowed)
        if extra_fields:
            errors.append(f"{agent_name} body has unsupported fields: {', '.join(extra_fields)}")

    return errors


def ordered_agent_body_items(agent_name: str, body: dict[str, Any]) -> list[tuple[str, str, Any]]:
    schema = get_collection_schema(agent_name)
    if schema is None:
        return [(name, name.replace("_", " ").title(), value) for name, value in sorted(body.items())]

    ordered: list[tuple[str, str, Any]] = []
    seen: set[str] = set()
    for field in schema.fields:
        if field.name in body:
            ordered.append((field.name, field.label, body[field.name]))
            seen.add(field.name)

    for name in sorted(body):
        if name in seen:
            continue
        ordered.append((name, name.replace("_", " ").title(), body[name]))
    return ordered
