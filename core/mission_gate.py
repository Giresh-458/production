from __future__ import annotations

import re
from typing import Any

from core.funding_selection import FundingCallContext
from core.schemas import DOMAIN_TERM_WEIGHTS, has_required_domain_anchor_groups, normalize_area

STOP = {
    "the", "and", "for", "with", "that", "this", "from", "into", "their",
    "there", "which", "using", "used", "use", "are", "was", "were", "have",
    "has", "had", "will", "can", "may", "more", "than", "also", "such",
    "these", "those", "about", "research", "funding", "opportunity",
    "program", "call", "application", "applicant", "applicants", "project",
    "projects", "barrier", "barriers", "deployment", "validation",
    "evaluation", "adoption", "existing", "solutions", "address", "problems",
    "gaps", "gap", "technical", "transition", "commercialization", "tcup", "nsf",
    "investigate", "affecting", "unresolved", "limitations", "improving",
    # Generic funding priorities must not count as substantive technical overlap.
    "market", "markets", "adoption",
}


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", (text or "").lower())
        if token not in STOP
    }


def _specific_area(value: Any) -> str:
    rendered = str(value or "").strip()
    return "" if rendered.lower() in {"", "general", "unscoped", "unknown", "none", "null"} else rendered


def _supported_domain_names(research_context: Any) -> set[str] | None:
    """Return canonical domains backed by the same strong evidence as Batch-1 routing.

    Older persisted contexts may not contain domain_relevance. For those
    contexts, return None and preserve their legacy selected-domain behavior.
    When evidence is present, weak secondary domains must not become mission
    anchors merely because they appear in selected_domains.
    """
    raw_ranking = getattr(research_context, "domain_relevance", None)
    if not isinstance(raw_ranking, (list, tuple)) or not raw_ranking:
        return None
    if not any(
        isinstance(item, dict) and ("raw_score" in item or "matched_terms" in item)
        for item in raw_ranking
    ):
        return None

    supported: set[str] = set()
    for item in raw_ranking:
        if not isinstance(item, dict):
            continue
        domain = normalize_area(str(item.get("domain") or item.get("area") or ""))
        weights = DOMAIN_TERM_WEIGHTS.get(str(domain or ""))
        if not weights:
            continue
        try:
            raw_score = float(item.get("raw_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if raw_score < 2.5:
            continue
        matched_terms = item.get("matched_terms") or []
        if not isinstance(matched_terms, (list, tuple, set)):
            continue
        strong_terms = {
            term.casefold()
            for term, weight in weights.items()
            if float(weight) >= 2.0
        }
        supported_terms = {
            str(term).casefold().strip()
            for term in matched_terms
            if str(term).casefold().strip() in strong_terms
        }
        if supported_terms and has_required_domain_anchor_groups(domain, supported_terms):
            supported.add(str(domain))
    return supported


def _mission_anchor_values(ctx: FundingCallContext) -> list[str]:
    """Build relevance anchors only from comparatively specific mission fields.

    Generated investigation questions and inferred challenges are deliberately
    excluded: boilerplate such as "barriers to deployment and adoption" can
    otherwise make unrelated pages look relevant to almost any grant. Canonical
    domain labels are included only when the persisted domain-ranking evidence
    meets the same strong-anchor threshold used by the pipeline's domain router.
    """
    rc = ctx.research_context
    values: list[str] = []
    supported_domains = _supported_domain_names(rc) if rc else None

    def add_values(raw_values: Any) -> None:
        if not isinstance(raw_values, (list, tuple, set)):
            return
        for raw_value in raw_values:
            value = str(raw_value or "").strip()
            if not value:
                continue
            canonical = normalize_area(value)
            if (
                supported_domains is not None
                and canonical in DOMAIN_TERM_WEIGHTS
                and canonical not in supported_domains
            ):
                continue
            values.append(value)

    def add_domain(raw_value: Any) -> None:
        value = _specific_area(raw_value)
        if not value:
            return
        canonical = normalize_area(value)
        if (
            supported_domains is not None
            and canonical in DOMAIN_TERM_WEIGHTS
            and canonical not in supported_domains
        ):
            return
        values.append(value)

    if rc:
        add_values(rc.technology_themes)
        add_values(rc.funding_priorities)
        add_values(rc.target_outcomes)
        add_values(rc.selected_domains)
        add_domain(getattr(rc, "domain", None))

    add_domain(ctx.research_area)
    add_values(ctx.research_priorities)
    return list(dict.fromkeys(values))


def _mission_anchor_text(ctx: FundingCallContext) -> str:
    return " ".join(_mission_anchor_values(ctx))


def relevance(record: dict[str, Any], ctx: FundingCallContext) -> dict[str, Any]:
    """Conservative post-retrieval relevance gate.

    Only retrieved document content is considered evidence. Search queries,
    keywords, source/evidence types, actor IDs, and other metadata cannot make a
    document relevant because those fields may simply repeat the retrieval
    instructions rather than describe what the source actually says.
    """
    anchor_values = _mission_anchor_values(ctx)
    anchor_text = " ".join(anchor_values)
    document_text = " ".join(
        str(record.get(key, ""))
        for key in ("title", "problem_statement", "context_summary")
        if record.get(key)
    )

    mission_tokens = _tokens(anchor_text)
    document_tokens = _tokens(document_text)
    if not mission_tokens or not document_tokens:
        return {
            "passed": False,
            "score": 0.0,
            "matched_terms": [],
            "matched_phrases": [],
            "reason": "empty substantive mission/document vocabulary",
        }

    matches = sorted(mission_tokens & document_tokens)
    raw_document = document_text.lower()
    phrases: list[str] = []
    for anchor_value in anchor_values:
        for phrase in re.findall(r"[a-z0-9][a-z0-9 -]{5,}", anchor_value.lower()):
            phrase = " ".join(phrase.split())
            phrase_tokens = _tokens(phrase)
            if (
                len(phrase.split()) >= 2
                and phrase_tokens
                and phrase in raw_document
                and phrase not in STOP
            ):
                phrases.append(phrase)

    # One exact technical phrase or at least two non-boilerplate terms is
    # required. Funding-call identity and generated retrieval queries alone do
    # not satisfy this check.
    passed = bool(phrases) or len(matches) >= 2
    score = min(1.0, len(matches) / max(3, min(len(mission_tokens), 12)))
    return {
        "passed": passed,
        "score": round(score, 3),
        "matched_terms": matches[:20],
        "matched_phrases": phrases[:5],
        "reason": "substantive mission overlap" if passed else "insufficient substantive mission overlap",
    }
