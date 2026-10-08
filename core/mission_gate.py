from __future__ import annotations
import re
from typing import Any
from core.funding_selection import FundingCallContext

STOP = {"the","and","for","with","that","this","from","into","their","there","which","using","used","use","are","was","were","have","has","had","will","can","may","more","than","also","such","these","those","about","research","funding","opportunity","program","call","application","applicant","applicants","project","projects"}

def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", (text or '').lower()) if t not in STOP}

def mission_text(ctx: FundingCallContext) -> str:
    rc = ctx.research_context
    vals = [ctx.call_title, ctx.program_name, ctx.funding_body, ctx.geography, ctx.eligibility,
            ctx.eligible_costs, ctx.required_partners, ctx.deliverables, ctx.evaluation_criteria,
            ctx.funding_amount, ctx.duration, ctx.research_area]
    if rc:
        vals += list(rc.technology_themes) + list(rc.funding_priorities) + list(rc.target_outcomes) + list(rc.investigation_questions) + list(rc.inferred_challenges) + list(rc.selected_domains)
    vals += list(ctx.research_priorities or [])
    return " ".join(str(v) for v in vals if v)

def relevance(record: dict[str, Any], ctx: FundingCallContext) -> dict[str, Any]:
    """Conservative post-retrieval relevance gate.

    Generic words such as research, program, project, application, security, or
    deployment must never be enough to admit a document. The gate requires overlap
    with substantive mission anchors extracted from the selected call.
    """
    rc = ctx.research_context
    substantive_values: list[str] = []
    if rc:
        substantive_values.extend(list(rc.technology_themes or ()))
        substantive_values.extend(list(rc.funding_priorities or ()))
        substantive_values.extend(list(rc.target_outcomes or ()))
        substantive_values.extend(list(rc.investigation_questions or ()))
        substantive_values.extend(list(rc.inferred_challenges or ()))
        substantive_values.extend(list(rc.selected_domains or ()))
    if ctx.research_area and str(ctx.research_area).strip().lower() not in {"general", "unscoped"}:
        substantive_values.append(str(ctx.research_area))
    # The call title is useful only as a substantive anchor when it contains more
    # than generic funding/program language.
    substantive_values.append(str(ctx.call_title or ""))
    anchor_text = " ".join(v for v in substantive_values if v)
    doc = " ".join(str(record.get(k, '')) for k in ('title','problem_statement','context_summary','focus','source_type','evidence_type','actor','keywords'))
    mt = _tokens(anchor_text); dt = _tokens(doc)
    if not mt or not dt:
        return {"passed": False, "score": 0.0, "matched_terms": [], "matched_phrases": [], "reason": "empty substantive mission/document vocabulary"}
    matches = sorted(mt & dt)
    raw_m = anchor_text.lower(); raw_d = doc.lower()
    phrases = []
    for phrase in re.findall(r"[a-z0-9][a-z0-9 -]{5,}", raw_m):
        phrase = " ".join(phrase.split())
        if len(phrase.split()) >= 2 and phrase in raw_d and phrase not in STOP:
            phrases.append(phrase)
    # Ignore generic overlap. A document must match at least one substantive
    # multi-character anchor and, for short/generic anchors, a second anchor.
    substantive_matches = [m for m in matches if m not in STOP and len(m) >= 5]
    passed = bool(phrases) or len(substantive_matches) >= 2
    score = min(1.0, len(substantive_matches) / max(3, min(len(mt), 12)))
    return {
        "passed": passed,
        "score": round(score, 3),
        "matched_terms": substantive_matches[:20],
        "matched_phrases": phrases[:5],
        "reason": "substantive mission overlap" if passed else "insufficient substantive mission overlap",
    }

