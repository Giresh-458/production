from __future__ import annotations
import pytest

import json
from pathlib import Path

pytestmark = pytest.mark.live

from core.agent_registry import run_registered_agent
from core.normalization import save_normalized_collection_records
from tests.test_all_collection_agents import COLLECTION_AGENTS, TEXTS, AREAS


def test_full_collection_to_proposal_pipeline(tmp_path: Path):
    root = tmp_path / "rif"
    root.mkdir()

    # Reuse the established evidence-rich local fixtures for all 12 collectors.
    for name in COLLECTION_AGENTS:
        result = run_registered_agent(
            name,
            "manual_text",
            area=AREAS[name],
            input_data={
                "text": TEXTS[name],
                "source_name": f"E2E {name}",
                "analysis_mode": "collect_only",
                "output_dir": str(root / "intermediate" / name),
            },
        )
        assert result["status"] == "success", result
        assert result["items_saved"] >= 1, result

    normalized = save_normalized_collection_records(root, root)
    assert normalized["records"].exists()
    records = json.loads(normalized["records"].read_text(encoding="utf-8"))
    assert len(records) >= 8

    processing = [
        ("tagging", {"outputs_root": str(root), "output_dir": str(root / "processed" / "tags")}),
        ("clustering", {"outputs_root": str(root), "output_dir": str(root / "processed" / "clusters"), "threshold": 0.20}),
        ("trend", {"outputs_root": str(root), "output_dir": str(root / "processed" / "trends")}),
        ("synthesis", {"outputs_root": str(root), "output_dir": str(root / "synthesis" / "generated"), "manifest_dir": str(root / "synthesis" / "manifest")}),
    ]
    for name, payload in processing:
        result = run_registered_agent(name, "configured_scan", area="RWA", input_data=payload)
        assert result["status"] == "success", result
        assert result["items_saved"] >= 1, result

    from tests.test_multi_call_evidence_attribution import make_context
    ctx = make_context("RWA", "RWA Settlement", ["real-world asset settlement", "Zero-knowledge verification", "RWA", "assets", "scalable", "privacy-preserving", "verification", "research", "idea", "manual", "explicit", "statement", "problem", "solution"])
    
    # Mock novelty search to avoid external dependency failures and ensure it passes the proposal gate
    from unittest.mock import patch
    with patch("agents.idea_agent.perform_novelty_search") as mock_search, patch("agents.proposal_agent.mission_relevance") as mock_mission:
        mock_search.return_value = {
            "novelty_status": "no_overlap_in_search",
            "novelty_confidence": "High",
            "search_coverage": 1.0,
            "search_mode": "external",
            "query_executed": True,
            "similar_work": []
        }
        mock_mission.return_value = {"passed": True, "score": 0.8, "reason": "Mocked mission match"}
        
        idea = run_registered_agent(
            "idea",
            "configured_scan",
            area="RWA",
            input_data={"output_dir": str(root / "ideas"), "manifest_dir": str(root / "synthesis" / "manifest"), "external_novelty_validation": True},
        )
        assert idea["status"] == "success", idea
        assert idea["items_saved"] >= 1, idea
        
        proposal = run_registered_agent(
            "proposal",
            "configured_scan",
            area="RWA",
            input_data={"output_dir": str(root / "proposals"), "idea_dir": str(root / "ideas"), "__selected_call_context": ctx},
        )
        assert proposal["status"] == "success", proposal
    assert proposal["items_saved"] >= 1, proposal

    ideas = json.loads((root / "ideas" / "rwa" / "ideas.json").read_text(encoding="utf-8"))
    proposals = json.loads((root / "proposals" / "rwa" / "proposals.json").read_text(encoding="utf-8"))
    assert ideas
    assert proposals
    assert all(item["validation"]["ready_for_proposal"] is True for item in ideas)
    assert all(item["validation"]["ready_for_submission"] is False for item in proposals)
    assert all(item["eligibility"]["eligible_for_proposal_review"] is True for item in proposals)
