import pytest
from agents.proposal_agent import build_manual_payload, _proposal_gate

def test_build_manual_payload_no_nameerror():
    # Should not raise NameError due to undefined 'record'
    payload = build_manual_payload(text="Some text", title="Some Title", source="Manual", area="DePIN")
    assert payload["funding_alignment"].startswith("Funding alignment must be established")
    
def test_proposal_gate_rejects_unvalidated_novelty():
    record = {
        "validation": {
            "novelty_status": "NO_RESULT"
        }
    }
    class MockFundingContext:
        pass
    
    passed, reason = _proposal_gate(record, MockFundingContext())
    assert not passed
    assert reason["reason"] == "external novelty validation is not complete"
    
def test_proposal_gate_rejects_overlap():
    record = {
        "validation": {
            "novelty_status": "externally_validated",
            "novelty_search": {
                "novelty_status": "OVERLAP_FOUND"
            }
        }
    }
    class MockFundingContext:
        pass
    
    passed, reason = _proposal_gate(record, MockFundingContext())
    assert not passed
    assert reason["reason"] == "external search found overlap"
