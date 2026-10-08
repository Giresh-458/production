from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from core.collection_schemas import get_collection_schema
from core.evidence import PROBLEM_SIGNAL_TERMS
from core.schemas import AREA_KEYWORDS

GENERIC_TITLE_PATTERNS = {
    "home",
    "insights",
    "archive",
    "all content",
    "blog",
    "news",
    "research",
    "docs",
    "documentation",
    "featured",
    "filings",
    "writing",
}

BOILERPLATE_TERMS = (
    "subscribe",
    "sign up",
    "cookie",
    "privacy policy",
    "terms of service",
    "all rights reserved",
    "newsletter",
    "follow us",
    "learn more",
)

LENIENT_COLLECTION_AGENTS = {"regulation", "data_availability", "expert", "lab"}


def _clamp_score(value: int) -> int:
    return max(0, min(value, 5))


def _generic_title_score(title: str) -> int:
    cleaned = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    if not cleaned:
        return 2
    if cleaned in GENERIC_TITLE_PATTERNS:
        return 0
    if any(cleaned == pattern or cleaned.endswith(f" {pattern}") for pattern in GENERIC_TITLE_PATTERNS):
        return 1
    return 2


def _score_research_area_match(research_area: str | None, area_scores: dict[str, int], *, explicit_area: str | None = None) -> int:
    if not research_area or research_area == "Unscoped":
        return 1
    if explicit_area and research_area == explicit_area:
        return 5
    score = int(area_scores.get(research_area, 0))
    if score >= 8:
        return 5
    if score >= 5:
        return 4
    if score >= 2:
        return 3
    if score >= 1:
        return 2
    return 0


def _score_source_specificity(title: str, source: str, evidence_bundle: dict[str, Any]) -> int:
    title_score = _generic_title_score(title)
    parsed = urlparse(source or "")
    path = parsed.path.strip("/")
    path_segments = [segment for segment in path.split("/") if segment]

    score = title_score
    if parsed.netloc:
        score += 1
    if len(path_segments) >= 2:
        score += 1
    if path_segments and all(segment not in {"", "blog", "news", "docs", "research", "archive"} for segment in path_segments[-2:]):
        score += 1
    if evidence_bundle.get("section_headings") or evidence_bundle.get("bullet_lists"):
        score += 1
    return _clamp_score(score)


def _score_problem_signal_density(raw_content: str, research_area: str | None, evidence_bundle: dict[str, Any]) -> int:
    lowered = (raw_content or "").lower()
    hits = 0
    for term in PROBLEM_SIGNAL_TERMS:
        hits += lowered.count(term.lower())
    if research_area and research_area in AREA_KEYWORDS:
        for keyword in AREA_KEYWORDS[research_area]:
            hits += lowered.count(keyword.lower())
    hits += len(evidence_bundle.get("evidence_snippets", []))
    hits += len(evidence_bundle.get("excerpt_windows", []))

    if hits >= 16:
        return 5
    if hits >= 10:
        return 4
    if hits >= 6:
        return 3
    if hits >= 3:
        return 2
    if hits >= 1:
        return 1
    return 0


def _score_evidence_richness(raw_content: str, evidence_bundle: dict[str, Any]) -> int:
    score = 0
    if len(raw_content.strip()) >= 600:
        score += 2
    elif len(raw_content.strip()) >= 200:
        score += 1

    for key in (
        "section_headings",
        "key_paragraphs",
        "bullet_lists",
        "quoted_passages",
        "links_found",
        "named_entities_or_programs",
        "evidence_snippets",
        "excerpt_windows",
    ):
        value = evidence_bundle.get(key, [])
        if value:
            score += 1
    return _clamp_score(score)


def _boilerplate_density(raw_content: str) -> int:
    lowered = (raw_content or "").lower()
    return sum(lowered.count(term) for term in BOILERPLATE_TERMS)


def evaluate_collection_quality(
    *,
    agent_name: str,
    title: str,
    source: str,
    research_area: str | None,
    raw_content: str,
    evidence_bundle: dict[str, Any],
    area_scores: dict[str, int],
) -> dict[str, Any]:
    schema = get_collection_schema(agent_name)
    is_lenient_agent = agent_name in LENIENT_COLLECTION_AGENTS
    is_manual_source = (source or "").startswith("manual://") or (source or "").strip().lower() == "manual"
    research_area_match = _score_research_area_match(research_area, area_scores, explicit_area=research_area)
    source_specificity = _score_source_specificity(title, source, evidence_bundle)
    problem_signal_density = _score_problem_signal_density(raw_content, research_area, evidence_bundle)
    evidence_richness = _score_evidence_richness(raw_content, evidence_bundle)

    total_score = (
        research_area_match * 30
        + source_specificity * 20
        + problem_signal_density * 25
        + evidence_richness * 25
    ) // 5

    rejection_reasons: list[str] = []
    generic_title = _generic_title_score(title) == 0
    boilerplate_hits = _boilerplate_density(raw_content)
    key_paragraphs = len(evidence_bundle.get("key_paragraphs", []))
    evidence_snippets = len(evidence_bundle.get("evidence_snippets", []))
    excerpt_windows = len(evidence_bundle.get("excerpt_windows", []))
    section_headings_or_bullets = len(evidence_bundle.get("section_headings", [])) + len(evidence_bundle.get("bullet_lists", []))

    min_content_length = 60 if is_lenient_agent else 80
    if len((raw_content or "").strip()) < min_content_length:
        rejection_reasons.append("content too short to preserve useful evidence")
    if research_area and research_area != "Unscoped" and research_area_match == 0:
        rejection_reasons.append("weak or missing research-area evidence")
    if problem_signal_density <= (0 if is_lenient_agent else 1):
        rejection_reasons.append("problem-signal density too low")
    if evidence_richness <= (0 if is_lenient_agent else 1):
        rejection_reasons.append("evidence bundle too thin")
    if generic_title and problem_signal_density <= (1 if is_lenient_agent else 2):
        rejection_reasons.append("generic page title without enough specific evidence")
    if boilerplate_hits >= (6 if is_lenient_agent else 4) and problem_signal_density <= (1 if is_lenient_agent else 2):
        rejection_reasons.append("content appears dominated by boilerplate/navigation text")
    if total_score < (35 if is_lenient_agent else 45):
        rejection_reasons.append("overall quality score below minimum threshold")
    if schema is not None:
        if key_paragraphs < schema.min_key_paragraphs:
            rejection_reasons.append(
                f"{agent_name} acceptance rule failed: expected at least {schema.min_key_paragraphs} key paragraph(s)"
            )
        required_evidence_snippets = 1 if is_manual_source else schema.min_evidence_snippets
        required_excerpt_windows = 0 if is_manual_source else schema.min_excerpt_windows
        if evidence_snippets < required_evidence_snippets:
            rejection_reasons.append(
                f"{agent_name} acceptance rule failed: expected at least {required_evidence_snippets} evidence snippet(s)"
            )
        if excerpt_windows < required_excerpt_windows:
            rejection_reasons.append(
                f"{agent_name} acceptance rule failed: expected at least {required_excerpt_windows} excerpt window(s)"
            )
        if not is_manual_source and section_headings_or_bullets < schema.min_section_headings_or_bullets:
            rejection_reasons.append(
                f"{agent_name} acceptance rule failed: expected section/bullet structure for this collector"
            )

    # Deduplicate reasons while keeping order.
    unique_reasons: list[str] = []
    seen: set[str] = set()
    for reason in rejection_reasons:
        if reason not in seen:
            seen.add(reason)
            unique_reasons.append(reason)

    return {
        "agent_name": agent_name,
        "quality_dimensions": {
            "research_area_match": research_area_match,
            "source_specificity": source_specificity,
            "problem_signal_density": problem_signal_density,
            "evidence_richness": evidence_richness,
        },
        "acceptance_dimensions": {
            "key_paragraphs": key_paragraphs,
            "evidence_snippets": evidence_snippets,
            "excerpt_windows": excerpt_windows,
            "section_headings_or_bullets": section_headings_or_bullets,
        },
        "quality_score": total_score,
        "generic_title_detected": generic_title,
        "boilerplate_hits": boilerplate_hits,
        "passed": not unique_reasons,
        "rejection_reason": "; ".join(unique_reasons) if unique_reasons else "",
    }
