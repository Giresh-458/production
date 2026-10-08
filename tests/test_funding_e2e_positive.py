import pytest
import tempfile
import json
from pathlib import Path
from agents.proposal_agent import _proposal_gate, run_agent
from core.funding_selection import FundingCallContext
from core.research_schemas import FundingResearchContext
from core.mission_gate import relevance

def test_pipeline_downstream_proposal_generation():
    # 1. Construct a FundingCallContext with a real FundingResearchContext containing genuine technical anchors
    rc = FundingResearchContext(
        funding_call_id="DEPIN-2026",
        domain="DePIN",
        technology_themes=("decentralized physical infrastructure", "compute network", "storage network"),
        funding_priorities=(),
        target_outcomes=(),
        investigation_questions=(),
        inferred_challenges=(),
        evidence_refs=(),
        inference_provenance=(),
        confidence="high",
        selected_domains=("DePIN",)
    )
    
    ctx = FundingCallContext(
        funding_call_id="DEPIN-2026",
        funding_body="NSF",
        program_name="Tech Program",
        call_id="123",
        call_title="DePIN Technical Research",
        status="OPEN",
        opening_date="2026-01-01",
        deadline="2026-12-31",
        funding_amount="1",
        currency="USD",
        duration="1",
        eligible_costs="research",
        eligibility="research institutions",
        geography="US",
        trl="research",
        research_priorities=[],
        required_partners="none",
        deliverables="technical research",
        evaluation_criteria="technical merit",
        application_url="test",
        source_url="test",
        source_name="test",
        publication_date="2026-01-01",
        last_updated="2026-01-01",
        research_area="DePIN",
        selection_mode="test",
        selection_score=1.0,
        selection_reasons=[],
        research_context=rc
    )
    
    # 2. Create deterministic technical evidence whose content genuinely overlaps those technical anchors.
    record = {
        "title": "Decentralized physical infrastructure research",
        "problem": "Improving compute network and storage network reliability",
        "gap": "existing networks lack physical compute capabilities",
        "idea": "build a decentralized compute network",
        "why_important": "because storage network is slow",
        "existing_solutions": "centralized infrastructure",
        "source_inputs": [],
        "confidence_score": 0.9,
        "research_area": "DePIN",
        "validation": {
            "novelty_status": "no_overlap_in_search",
            "validation_score": 10.0,
            "novelty_search": {
                "novelty_status": "no_overlap_in_search"
            }
        }
    }
    
    # 3 & 4. Assert the REAL mission relevance gate returns passed=True
    gate_result = relevance({"title": record["title"], "problem_statement": record["problem"], "context_summary": record["idea"], "source_type": "proposal", "evidence_type": "proposal", "keywords": []}, ctx)
    assert gate_result["passed"] is True
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        # Write out the idea so the proposal agent can load it
        idea_dir = tmp_path / "ideas" / "depin"
        idea_dir.mkdir(parents=True)
        idea_file = idea_dir / "ideas.json"
        idea_file.write_text(json.dumps([record]), encoding="utf-8")
        
        # 5. Verify the record continues to the next downstream stage rather than being rejected by mission relevance.
        from unittest.mock import patch
        with patch("core.llm_provider.generate") as mock_generate:
            # Mock the LLM alignment check which is called by _proposal_gate after mission_relevance passes
            mock_generate.return_value = '{"aligned": true, "reason": "Technical alignment is clear"}'
            
            proposal = run_agent(
                mode="configured_scan",
                area="DePIN",
                input_data={
                    "output_dir": str(tmp_path / "proposals"), 
                    "idea_dir": str(tmp_path / "ideas"), 
                    "__selected_call_context": ctx
                },
            )
            
            assert proposal["status"] == "success", proposal
            assert proposal["items_saved"] == 1, "The record must continue to downstream stage (items_saved = 1) rather than being rejected"
            
            proposals_file = tmp_path / "proposals" / "depin" / "proposals.json"
            assert proposals_file.exists()
            saved_props = json.loads(proposals_file.read_text(encoding="utf-8"))
            assert len(saved_props) == 1
            assert saved_props[0]["problem"] == record["problem"]
