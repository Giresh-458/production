import pytest
from core.agent_interface import build_intermediate_artifact_payload

def test_p0_end_to_end_bad_greeting_extraction():
    content = """Greetings from the IIT Indore organizing committee. We are thrilled to welcome you.
    
The fundamental challenge in current proof generation is that verification latency becomes impractical above 100K transactions.
This deployment friction limits scale.

Please contact us if you need any assistance."""
    
    payload, _ = build_intermediate_artifact_payload(
        agent="funding", layer="Funding", mode="manual_text", area="RWA", 
        title="IIT Indore Hackathon", source="http://iitindore.ac.in", content=content
    )
    
    problem_intel = payload["problem_intelligence"]
    
    # Assert bad greeting is rejected
    assert "Greetings from the IIT Indore" not in problem_intel["selected_problem"]
    
    # Assert actual problem is extracted
    assert "verification latency becomes impractical" in problem_intel["selected_problem"].lower()
    
    # Assert evidence is attached
    assert len(problem_intel["problem_evidence"]) > 0
    assert "verification latency" in problem_intel["problem_evidence"][0].lower()
    
    # Assert structured info exists
    assert problem_intel["problem_type"] == "identified_limitation"
    assert problem_intel["intelligence_mode"] == "deterministic"
