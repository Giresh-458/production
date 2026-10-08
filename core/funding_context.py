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
    """Generate focused, mission-driven queries for downstream collection agents.

    Investigation questions are the strongest signal, followed by technology
    themes and funding priorities. Duplicates are removed while preserving order.
    """
    max_queries = max(1, min(int(max_queries), 8))
    rc = funding_context.research_context
    candidates: list[str] = []

    if rc:
        prefix_domains = tuple(rc.selected_domains) if rc.selected_domains else ((funding_context.research_area,) if funding_context.research_area else ())
        domain_prefix = " / ".join(str(d) for d in prefix_domains if d)
        for q in rc.investigation_questions:
            q_text = q.strip() if q else ""
            if not q_text:
                continue
            candidates.append(f"{domain_prefix}: {q_text}" if domain_prefix and domain_prefix.casefold() not in q_text.casefold() else q_text)
        candidates.extend(t.strip() for t in rc.technology_themes if t and t.strip())
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
    for q in candidates:
        key = q.casefold()
        if key not in seen:
            seen.add(key)
            deduped.append(q)
        if len(deduped) >= max_queries:
            break
    return deduped



def funding_context_relevance(text: str, *, min_matches: int = 2) -> bool:
    """Check whether source text contains concrete terms from the selected funding mission.

    This is deliberately a retrieval gate, not a factual claim. It lets downstream
    collectors operate outside the fixed eight-area taxonomy while still keeping
    unscoped collection tied to the selected live call.
    """
    import re
    raw = str(text or "").lower()
    queries = current_funding_queries.get() or []
    if not raw or not queries:
        return False
    stop = {
        "what", "which", "where", "when", "that", "this", "these", "those",
        "from", "about", "with", "into", "have", "does", "exist", "already",
        "research", "funding", "opportunity", "problems", "problem", "gaps",
        "relevant", "related", "existing", "technology", "technologies",
    }
    for query in queries:
        terms = [t for t in re.findall(r"[a-z0-9][a-z0-9_-]{3,}", str(query).lower()) if t not in stop]
        if not terms:
            continue
        matches = sum(1 for term in set(terms) if term in raw)
        if matches >= min_matches:
            return True
    return False
