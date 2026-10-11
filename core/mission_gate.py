from __future__ import annotations

import re
from typing import Any

from core.funding_selection import FundingCallContext

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


def _mission_anchor_text(ctx: FundingCallContext) -> str:
    """Build relevance anchors only from comparatively specific mission fields.

    Generated investigation questions and inferred challenges are deliberately
    excluded: boilerplate such as "barriers to deployment and adoption" can
    otherwise make unrelated pages look relevant to almost any grant.
    """
    rc = ctx.research_context
    values: list[str] = []

    if rc:
        values.extend(str(v) for v in (rc.technology_themes or ()) if str(v).strip())
        values.extend(str(v) for v in (rc.funding_priorities or ()) if str(v).strip())
        values.extend(str(v) for v in (rc.target_outcomes or ()) if str(v).strip())
        values.extend(str(v) for v in (rc.selected_domains or ()) if str(v).strip())

        rc_domain = _specific_area(getattr(rc, "domain", None))
        if rc_domain:
            values.append(rc_domain)

    ctx_area = _specific_area(ctx.research_area)
    if ctx_area:
        values.append(ctx_area)

    values.extend(str(v) for v in (ctx.research_priorities or []) if str(v).strip())
    return " ".join(values)


def relevance(record: dict[str, Any], ctx: FundingCallContext) -> dict[str, Any]:
    """Conservative post-retrieval relevance gate.

    Only retrieved document content is considered evidence. Search queries,
    keywords, source/evidence types, actor IDs, and other metadata cannot make a
    document relevant because those fields may simply repeat the retrieval
    instructions rather than describe what the source actually says.
    """
    anchor_text = _mission_anchor_text(ctx)
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
    raw_mission = anchor_text.lower()
    raw_document = document_text.lower()
    phrases: list[str] = []
    for phrase in re.findall(r"[a-z0-9][a-z0-9 -]{5,}", raw_mission):
        phrase = " ".join(phrase.split())
        if len(phrase.split()) >= 2 and phrase in raw_document and phrase not in STOP:
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
