from __future__ import annotations
import json
from pathlib import Path
import pytest
from core.agent_registry import run_registered_agent
from tests.test_multi_call_evidence_attribution import make_context

def test_provenance_trace(tmp_path: Path):
    root = tmp_path / "rif"
    root.mkdir()
    
    # Funding A
    ctx = make_context("FUNDING-A", "Trace Program", ["traceable provenance evidence", "document demonstrating provenance"])
    
    # Collector
    col_out = run_registered_agent(
        "lab",
        "manual_text",
        area="RWA",
        input_data={"text": "This is a traceable evidence document demonstrating provenance for FUNDING-A. " * 50, "source_url": "https://example.com/test-source", "output_dir": str(root / "intermediate" / "lab")},
        funding_contexts=[ctx]
    )
    assert col_out["status"] in ("success", "partial_success")
    assert col_out["items_saved"] > 0
    print("COL_OUT:", col_out)
    
    # Normalize
    from core.normalization import save_normalized_collection_records
    norm_res = save_normalized_collection_records(root, root)
    records = json.loads(norm_res["records"].read_text(encoding="utf-8"))
    assert len(records) == 1
    assert "FUNDING-A" in records[0]["funding_call_ids"]
    
    # Tagging & Clustering & Synthesis
    run_registered_agent("tagging", "configured_scan", area="RWA", input_data={"outputs_root": str(root), "output_dir": str(root / "processed" / "tags")})
    run_registered_agent("clustering", "configured_scan", area="RWA", input_data={"outputs_root": str(root), "output_dir": str(root / "processed" / "clusters"), "threshold": 0.20})
    run_registered_agent("trend", "configured_scan", area="RWA", input_data={"outputs_root": str(root), "output_dir": str(root / "processed" / "trends")})
    run_registered_agent("synthesis", "configured_scan", area="RWA", input_data={"outputs_root": str(root), "output_dir": str(root / "synthesis" / "generated"), "manifest_dir": str(root / "synthesis" / "manifest")})
    
    # Idea
    from unittest.mock import patch
    with patch("agents.idea_agent.perform_novelty_search") as mock_search, patch("agents.proposal_agent.mission_relevance") as mock_mission:
        mock_search.return_value = {"novelty_status": "no_overlap_in_search", "novelty_confidence": "High", "search_coverage": 1.0, "search_mode": "external", "query_executed": True, "similar_work": []}
        idea = run_registered_agent("idea", "configured_scan", area="RWA", input_data={"output_dir": str(root / "ideas"), "manifest_dir": str(root / "synthesis" / "manifest"), "external_novelty_validation": True})
        
        mock_mission.return_value = {"passed": True, "score": 0.8, "reason": "Mocked"}
        proposal = run_registered_agent("proposal", "configured_scan", area="RWA", input_data={"output_dir": str(root / "proposals"), "idea_dir": str(root / "ideas"), "__selected_call_context": ctx})
    
    # Verify provenance
    print("IDEA:", idea)
    print("PROPOSAL:", proposal)
    proposals = json.loads((root / "proposals" / "rwa" / "proposals.json").read_text(encoding="utf-8"))
    assert len(proposals) > 0
    p = proposals[0]
    assert "FUNDING-A" in p["funding_call_ids"]
    
