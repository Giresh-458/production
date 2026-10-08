import pytest
from core.agent_interface import validate_run_input
from core.funding_selection import FundingCallContext, ScoredCall, ValidationResult

def test_three_contexts_validation():
    # Test A: Verify all 11 agents receive A+B+C without error
    # Our fix in validate_run_input allowed funding_contexts to be passed instead of just funding_context
    ctx_a = FundingCallContext.from_scored_call(ScoredCall(call_id="A", score=1.0, record={}, validation=ValidationResult(True, [])), mode="test")
    ctx_b = FundingCallContext.from_scored_call(ScoredCall(call_id="B", score=1.0, record={}, validation=ValidationResult(True, [])), mode="test")
    ctx_c = FundingCallContext.from_scored_call(ScoredCall(call_id="C", score=1.0, record={}, validation=ValidationResult(True, [])), mode="test")
    
    from core.agent_registry import COLLECTION_AGENTS
    downstream_agents = [a for a in COLLECTION_AGENTS if a != "funding"]
    
    for agent in downstream_agents:
        errors = validate_run_input(agent, "configured_scan", {}, funding_contexts=[ctx_a, ctx_b, ctx_c])
        assert "MISSING_FUNDING_CONTEXT" not in errors

def test_source_relevance_any_match():
    # Test B, C, D: Any-match, shared, irrelevant
    from core.funding_context import funding_context_relevance, generate_funding_queries, current_funding_queries
    ctx_a = FundingCallContext.from_scored_call(ScoredCall(call_id="A", score=1.0, record={"focus_area": "Health System", "research_priorities": ["Medical Device"]}, validation=ValidationResult(True, [])), mode="test")
    ctx_c = FundingCallContext.from_scored_call(ScoredCall(call_id="C", score=1.0, record={"focus_area": "Security", "research_priorities": ["Cyber Network"]}, validation=ValidationResult(True, [])), mode="test")
    
    # Set the global context variable
    queries = generate_funding_queries(ctx_a) + generate_funding_queries(ctx_c)
    token = current_funding_queries.set(queries)
    
    source_a = "Medical Device Health System"
    source_c = "Cyber Network Security Architecture"
    source_ac = "Medical Device Health System Cyber Network Security"
    source_irrelevant = "Agriculture Farming Tractor Soil"
    
    try:
        assert funding_context_relevance(source_a) == True
        assert funding_context_relevance(source_c) == True
        assert funding_context_relevance(source_ac) == True
        assert funding_context_relevance(source_irrelevant) == False
    finally:
        current_funding_queries.reset(token)
def test_finalize_collection_agent_response_matching():
    from core.agent_interface import finalize_collection_agent_response
    ctx_a = FundingCallContext.from_scored_call(ScoredCall(call_id="CALL-A", score=1.0, record={"focus_area": "Health System", "research_priorities": ["Medical Device", "Health System"], "funding_call_id": "CALL-A"}, validation=ValidationResult(True, [])), mode="test")
    ctx_c = FundingCallContext.from_scored_call(ScoredCall(call_id="CALL-C", score=1.0, record={"focus_area": "Security", "research_priorities": ["Cyber Network", "Security"], "funding_call_id": "CALL-C"}, validation=ValidationResult(True, [])), mode="test")
    
    document_a = {"title": "Medical Device", "content": "Medical Device Health System CALL-A" * 50, "url": "https://a.com"}
    document_c = {"title": "Network Security", "content": "Cyber Network Security Architecture CALL-C" * 50, "url": "https://c.com"}
    document_ac = {"title": "Medical Cyber", "content": "Medical Device Health System Cyber Network Security CALL-A CALL-C" * 50, "url": "https://ac.com"}

    from datetime import UTC, datetime
    from pathlib import Path
    from unittest.mock import patch
    with patch("core.agent_interface.save_intermediate_markdown") as mock_save:
        mock_save.return_value = Path("dummy.md")
        result = finalize_collection_agent_response(
            agent="test",
            layer="test",
            mode="configured_scan",
            area=None,
            documents=[document_a, document_c, document_ac],
            funding_contexts=[ctx_a, ctx_c],
            started_at=datetime.now(UTC),
            output_dir=Path("tmp"),
        )
        
        payloads = [call.kwargs["payload"] for call in mock_save.call_args_list]
        headers = [p["shared_header"] for p in payloads]
        
        assert headers[0]["funding_call_ids"] == ["CALL-A"]
        assert headers[1]["funding_call_ids"] == ["CALL-C"]
        assert set(headers[2]["funding_call_ids"]) == {"CALL-A", "CALL-C"}
