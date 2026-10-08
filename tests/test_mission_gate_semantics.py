from core.mission_gate import relevance
from core.funding_selection import FundingCallContext, ScoredCall, ValidationResult
from core.research_schemas import FundingResearchContext

def build_ctx(title: str, rc: FundingResearchContext) -> FundingCallContext:
    scored = ScoredCall(
        call_id="1", 
        record={
            "funding_call_id": "1", 
            "title": title, 
            "program_name": title,
            "research_context": {
                "funding_call_id": rc.funding_call_id,
                "domain": rc.domain,
                "technology_themes": rc.technology_themes,
                "funding_priorities": rc.funding_priorities,
                "target_outcomes": rc.target_outcomes,
                "investigation_questions": rc.investigation_questions,
                "inferred_challenges": rc.inferred_challenges,
                "evidence_refs": rc.evidence_refs,
                "inference_provenance": rc.inference_provenance,
                "confidence": rc.confidence
            }
        },
        score=1.0,
        validation=ValidationResult(True, [])
    )
    return FundingCallContext.from_scored_call(scored, mode="test")

def test_mission_title_identity_rejection():
    # A. Funding-title-only identity match
    rc = FundingResearchContext(
        funding_call_id="1", domain="DePIN", technology_themes=(), funding_priorities=(),
        target_outcomes=(), investigation_questions=(), inferred_challenges=(),
        evidence_refs=(), inference_provenance=(), confidence="high"
    )
    ctx = build_ctx("Tribal Colleges and Universities Program", rc)
    doc = {"title": "A paper on Tribal Colleges and Universities Program"}
    result = relevance(doc, ctx)
    assert not result["passed"], "Mission relevance MUST fail for title identity match alone"

def test_administrative_identity_rejection():
    # B. Institutional/admin-only text
    rc = FundingResearchContext(
        funding_call_id="1", domain="DePIN", technology_themes=("sensors",), funding_priorities=(),
        target_outcomes=(), investigation_questions=(), inferred_challenges=(),
        evidence_refs=(), inference_provenance=(), confidence="high"
    )
    ctx = build_ctx("Grant Program", rc)
    doc = {"title": "tribal college university agency grant"}
    result = relevance(doc, ctx)
    assert not result["passed"], "Must not count institutional words as technical mission evidence"

def test_genuine_technical_evidence_passes():
    # D. Genuine technical evidence still passes
    rc = FundingResearchContext(
        funding_call_id="1", domain="DePIN", technology_themes=("decentralized physical infrastructure", "compute network"),
        funding_priorities=(), target_outcomes=(), investigation_questions=(), inferred_challenges=(),
        evidence_refs=(), inference_provenance=(), confidence="high"
    )
    ctx = build_ctx("DePIN Grant", rc)
    doc = {"title": "Research on decentralized physical infrastructure and compute network design"}
    result = relevance(doc, ctx)
    assert result["passed"], "Robust multi-term evidence must satisfy existing routing/relevance expectations"

def test_legitimate_technical_phrases_preserved():
    # E. Legitimate technical phrases are preserved (not blocked by broad stop words)
    rc = FundingResearchContext(
        funding_call_id="1", domain="DePIN", technology_themes=("data center", "state estimation"),
        funding_priorities=(), target_outcomes=(), investigation_questions=(), inferred_challenges=(),
        evidence_refs=(), inference_provenance=(), confidence="high"
    )
    ctx = build_ctx("Tech Grant", rc)
    doc = {"title": "An approach to data center cooling and state estimation"}
    result = relevance(doc, ctx)
    assert result["passed"], "Technical phrases containing center/state must not be broken by filtering"
