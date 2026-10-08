import pytest
from core.problem_extraction import _validate_refinement, validate_claim_against_evidence

def test_proposal_evidence_validation():
    # Requirement 5
    evidence = "Latency increases beyond 100K transactions."

    # Claim 1
    assert _validate_refinement("Latency increases at high transaction volume.", evidence) is True
    
    # Claim 2
    assert _validate_refinement("Hospital systems experience unsafe patient delays.", evidence) is False
    
    # Claim 3
    assert _validate_refinement("The system has a guaranteed 2-second latency SLA.", evidence) is False
    
    # Claim 4
    assert _validate_refinement("High transaction volume is associated with increased verification latency.", evidence) is True

def test_provenance_integrity():
    # Requirement 6
    # This must fail (validation_status = supported, but no supporting_evidence_spans)
    from core.research_schemas import ClaimProvenance, ResearchCandidate
    
    with pytest.raises(ValueError):
        ClaimProvenance(
            claim="Some claim",
            provenance="EVIDENCE",
            source_refs=["source-A"],
            supporting_evidence_spans=[],
            validation_status="supported",
            support_score=1.0
        )
        
    span = "irrelevant text about funding"
    claim = "Latency increases at high transaction volume."
    assert _validate_refinement(claim, span) is False

def test_proposal_support_evidence_level():
    # Test for: source reference + specific supporting evidence span + claim/evidence compatibility
    from core.research_schemas import ClaimProvenance
    
    claim_text = "Hospital systems experience unsafe delays."
    evidence_span = "Latency is high."
    
    validation = validate_claim_against_evidence(claim_text, [evidence_span])
    assert validation.is_supported is False
    
    # Simulating what agents.proposal_agent._extract_and_check_claims produces
    claim_obj = {
        "text": claim_text,
        "provenance": "EVIDENCE",
        "supporting_refs": ["source-A"],
        "supporting_evidence_spans": [evidence_span],
        "validation_status": "supported" if validation.is_supported else "unsupported"
    }
    
    assert claim_obj["validation_status"] == "unsupported"
