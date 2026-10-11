from core.agent_interface import assign_funding_calls
from core.funding_selection import FundingCallContext, ScoredCall, ValidationResult


def _context(call_id: str, title: str, focus_area: str, priorities: list[str]) -> FundingCallContext:
    return FundingCallContext.from_scored_call(
        ScoredCall(
            call_id=call_id,
            score=1.0,
            record={
                "call_id": call_id,
                "title": title,
                "focus_area": focus_area,
                "research_priorities": priorities,
            },
            validation=ValidationResult(True, []),
        ),
        mode="test",
    )


def test_shared_evidence_is_attributed_by_per_call_relevance_without_call_title():
    health_call = _context(
        "CALL-HEALTH",
        "Health System Research Program",
        "Health System",
        ["Medical Device"],
    )
    cyber_call = _context(
        "CALL-CYBER",
        "Cyber Network Security Program",
        "Security Architecture",
        ["Cyber Network"],
    )

    # The source is about the research topic, but does not mention either
    # funding-call title, URL, or ID.
    assigned = assign_funding_calls(
        "Medical Device Field Evaluation",
        (
            "Clinical researchers evaluated Medical Device compatibility in the "
            "Health System and documented calibration and validation findings."
        ),
        [health_call, cyber_call],
    )

    assert assigned == ["CALL-HEALTH"]


def test_shared_evidence_is_not_attributed_from_generic_overlap():
    health_call = _context(
        "CALL-HEALTH",
        "Health System Research Program",
        "Health System",
        ["Medical Device"],
    )
    cyber_call = _context(
        "CALL-CYBER",
        "Cyber Network Security Program",
        "Security Architecture",
        ["Cyber Network"],
    )

    assigned = assign_funding_calls(
        "General Research Update",
        "A general update discusses common deployment issues and future adoption.",
        [health_call, cyber_call],
    )

    assert assigned == []
