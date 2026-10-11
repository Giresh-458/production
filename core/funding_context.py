import contextvars
from typing import Any, Dict

from core.funding_selection import FundingCallContext

# Thread-safe global variables for injecting funding context deep into the agent pipeline
current_funding_prompt_context = contextvars.ContextVar("current_funding_prompt_context", default="")
current_funding_queries = contextvars.ContextVar("current_funding_queries", default=None)


def build_agent_specific_funding_context(agent_name: str, funding_context: FundingCallContext) -> str:
    """
    Generate a compact, agent-specific representation of the FundingCallContext.
    This prevents injecting the entire context into every LLM prompt blindly.
    """
    lines = [f"FUNDING OPPORTUNITY CONTEXT (ID: {funding_context.funding_call_id})"]
    lines.append(f"Title: {funding_context.call_title}")
    
    # Common across all agents. Preserve legacy context fields while preferring
    # the new structured research mission when present.
    lines.append(f"Research Area: {funding_context.research_area or 'General'}")
    rc = funding_context.research_context
    selected_domains = tuple(rc.selected_domains) if rc else ()
    if selected_domains:
        lines.append(f"Selected Research Domains: {', '.join(selected_domains)}")
    if rc and rc.domain_relevance:
        lines.append("DOMAIN RELEVANCE (all canonical domains):")
        for item in rc.domain_relevance:
            lines.append(f"- {item.get('rank')}. {item.get('domain')}: {float(item.get('score', 0.0) or 0.0):.2f}; matched_terms={', '.join(item.get('matched_terms', []) or [])}")
    technology_themes = rc.technology_themes if rc else ()
    funding_priorities = rc.funding_priorities if rc else tuple(funding_context.research_priorities or ())
    target_outcomes = rc.target_outcomes if rc else ()
    investigation_questions = rc.investigation_questions if rc else ()
    if technology_themes:
        lines.append(f"Technology Themes: {', '.join(technology_themes)}")
    if funding_priorities:
        lines.append(f"Funding Priorities: {', '.join(funding_priorities)}")
    if target_outcomes:
        lines.append(f"Target Outcomes: {', '.join(target_outcomes)}")
    if investigation_questions:
        lines.append("\nRESEARCH MISSION / INVESTIGATION QUESTIONS:")
        for q in investigation_questions:
            lines.append(f"- {q}")

    # Agent-specific shaping
    if agent_name == "literature":
        if funding_context.trl:
            lines.append(f"TRL Requirement: {funding_context.trl}")
        lines.append("\nPURPOSE: What does existing scientific literature say about the research problems targeted by this funding call? Extract state of the art, limitations, and unresolved problems.")
        
    elif agent_name == "lab":
        if funding_context.trl:
            lines.append(f"TRL Requirement: {funding_context.trl}")
        if funding_context.geography:
            lines.append(f"Geography: {funding_context.geography}")
        lines.append("\nPURPOSE: Which research labs/groups have expertise relevant to this funding opportunity? Identify expertise, recent work, and collaboration fit.")

    elif agent_name == "company":
        if funding_context.geography:
            lines.append(f"Geography: {funding_context.geography}")
        lines.append("\nPURPOSE: Which companies have technologies, deployment experience, or industry needs relevant to this funding opportunity? Extract industry need and deployment evidence.")

    elif agent_name == "regulation":
        if funding_context.geography:
            lines.append(f"Jurisdiction/Geography: {funding_context.geography}")
        lines.append("\nPURPOSE: What regulatory, policy, legal, or standards constraints affect the problem targeted by this funding call?")

    elif agent_name == "opensource":
        lines.append("\nPURPOSE: What engineering limitations, unresolved issues, or architectural problems exist in open-source technologies relevant to the funding call?")

    elif agent_name == "practitioner":
        lines.append("\nPURPOSE: What real practitioners are experiencing that is relevant to this funding opportunity? Look for specific recurring technical pain and workarounds.")

    elif agent_name == "investment":
        lines.append("\nPURPOSE: Does industry investment indicate market demand around this funding opportunity? Note: Investment evidence is market validation, not scientific evidence.")

    elif agent_name == "failure":
        lines.append("\nPURPOSE: What real-world failures demonstrate the importance of the problem targeted by this funding opportunity?")

    elif agent_name == "data_availability":
        lines.append("\nPURPOSE: Can the research opportunity associated with this funding call actually be evaluated? Identify available datasets, missing data, and benchmark gaps.")

    elif agent_name == "hackathon":
        lines.append("\nPURPOSE: What emerging builder/developer demand is relevant to this funding opportunity?")

    elif agent_name == "expert":
        lines.append("\nPURPOSE: Which researchers/domain experts have direct expertise relevant to this funding opportunity?")

    elif agent_name == "funding_context" or agent_name == "funding":
        lines.append("\nPURPOSE: What has already been funded, and where might an unresolved research opportunity remain?")
        
    else:
        # Default fallback
        lines.append("\nPURPOSE: Investigate the research ecosystem relevant to the selected funding opportunity.")

    return "\n".join(lines)


def generate_funding_queries(funding_context: FundingCallContext, max_queries: int = 6) -> list[str]:
    """Generate focused, mission-driven queries for downstream collection.

    High-confidence contexts may use their investigation questions. For a low-
    confidence or inference-only context, prefer the actual funding-call title
    and extracted topic fields instead of turning generic inferred challenges
    into broad searches such as "barriers to deployment/adoption".
    """
    max_queries = max(1, min(int(max_queries), 8))
    rc = funding_context.research_context
    candidates: list[str] = []

    if rc:
        confidence = str(getattr(rc, "confidence", "") or "").strip().lower()
        provenance = {
            str(value).strip().upper()
            for value in (getattr(rc, "inference_provenance", ()) or ())
            if str(value).strip()
        }
        low_confidence = confidence in {"low", "unknown", "unassessed", "none", ""}
        inference_only = bool(provenance) and provenance.issubset({"INFERENCE", "INFERRED"})

        if low_confidence or inference_only:
            # Keep low-confidence generic questions/challenges out of the query
            # list. Start from the identified opportunity and its extracted
            # topic terms, while keeping each search string bounded.
            title = str(funding_context.call_title or funding_context.program_name or "").strip()
            if title:
                candidates.append(title)

            topics: list[str] = []
            for values in (
                rc.technology_themes,
                rc.funding_priorities,
                rc.target_outcomes,
                funding_context.research_priorities or (),
            ):
                for value in values or ():
                    topic = str(value or "").strip()
                    if topic and topic.casefold() not in {item.casefold() for item in topics}:
                        topics.append(topic)

            if title and topics:
                candidates.append(f"{title}: {', '.join(topics[:5])}")
            for topic in topics:
                candidates.append(f"{title} {topic}".strip() if title else topic)
        else:
            prefix_domains = tuple(rc.selected_domains) if rc.selected_domains else ((funding_context.research_area,) if funding_context.research_area else ())
            domain_prefix = " / ".join(str(d) for d in prefix_domains if d)
            for question in rc.investigation_questions:
                question_text = question.strip() if question else ""
                if not question_text:
                    continue
                candidates.append(
                    f"{domain_prefix}: {question_text}"
                    if domain_prefix and domain_prefix.casefold() not in question_text.casefold()
                    else question_text
                )
            candidates.extend(str(term).strip() for term in rc.technology_themes if term and str(term).strip())
            for priority in rc.funding_priorities:
                priority_text = str(priority).strip()
                if not priority_text:
                    continue
                if funding_context.research_area and funding_context.research_area.casefold() not in priority_text.casefold():
                    candidates.append(f"{funding_context.research_area} {priority_text}")
                else:
                    candidates.append(priority_text)

    if not candidates and funding_context.research_priorities:
        for priority in funding_context.research_priorities:
            priority_text = str(priority).strip()
            if not priority_text:
                continue
            if funding_context.research_area and funding_context.research_area.casefold() not in priority_text.casefold():
                candidates.append(f"{funding_context.research_area} {priority_text}")
            else:
                candidates.append(priority_text)

    if not candidates and funding_context.call_title:
        candidates.append(funding_context.call_title.strip())

    if not candidates and funding_context.research_area:
        candidates.append(funding_context.research_area.strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for query in candidates:
        key = query.casefold()
        if key not in seen:
            seen.add(key)
            deduped.append(query)
        if len(deduped) >= max_queries:
            break
    return deduped


def _matches_funding_queries(text: str, queries: list[str] | None, *, min_matches: int = 2) -> bool:
    """Match source text against one set of funding queries.

    Each query is evaluated independently. Terms from separate calls/queries are
    never combined to manufacture a match.
    """
    import re

    raw = str(text or "").lower()
    if not raw or not queries:
        return False
    stop = {
        "what", "which", "where", "when", "that", "this", "these", "those",
        "from", "about", "with", "into", "have", "does", "exist", "already",
        "research", "funding", "opportunity", "problems", "problem", "gaps",
        "relevant", "related", "existing", "technology", "technologies",
    }
    for query in queries:
        terms = [
            term
            for term in re.findall(r"[a-z0-9][a-z0-9_-]{3,}", str(query).lower())
            if term not in stop
        ]
        unique_terms = set(terms)
        if len(unique_terms) < min_matches:
            continue
        matches = sum(1 for term in unique_terms if term in raw)
        if matches >= min_matches:
            return True
    return False


def funding_context_relevance(text: str, *, min_matches: int = 2) -> bool:
    """Check source text against the current worker's combined funding queries."""
    return _matches_funding_queries(
        text,
        current_funding_queries.get(),
        min_matches=min_matches,
    )


def funding_context_relevance_for_context(
    text: str,
    funding_context: FundingCallContext,
    *,
    min_matches: int = 2,
) -> bool:
    """Check source text against one specific funding call's queries.

    This is used after shared collection to assign evidence to individual calls.
    It avoids assigning a document to every call merely because it matched a
    combined, multi-call query set.
    """
    return _matches_funding_queries(
        text,
        generate_funding_queries(funding_context),
        min_matches=min_matches,
    )
