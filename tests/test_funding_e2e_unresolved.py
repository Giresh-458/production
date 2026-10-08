import pytest
import tempfile
from pathlib import Path
from agents.funding_agent import build_markdown, FundingDocument, FundingOpportunity
from core.normalization import normalize_intermediate_artifact, build_normalized_collection_records
from agents.synthesis_agent import build_payload_from_manual_record
from agents.idea_agent import build_idea_payload_from_synthesis, attach_external_novelty_validation

def test_funding_e2e_unresolved_problem():
    from unittest.mock import MagicMock
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        # 1. Funding artifact generation
        doc = MagicMock()
        doc.organization = "Grants.gov"
        doc.title = "Generic Funding Call"
        doc.content = "Title: TCUP Agency: Details: We fund colleges."
        doc.source_type = "api"
        doc.source = "http://grants.gov"
        from unittest.mock import MagicMock
        opp = MagicMock()
        opp.program_name = "Generic Funding Call"
        opp.focus_area = "DePIN"
        opp.evidence = {}
        opp.status = "OPEN"
        opp.keywords = []
        opp.research_context = {}
        opp.funding_body = "Grants"
        opp.opening_date = None
        opp.deadline = None
        opp.funding_amount = None
        opp.currency = None
        opp.duration = None
        opp.eligible_costs = None
        opp.application_url = None
        opp.geography = None
        opp.eligibility = None
        opp.research_priorities = []
        opp.trl = None
        opp.required_partners = None
        opp.deliverables = None
        opp.evaluation_criteria = None
        opp.opportunity_type = None
        opp.source_method = "api"
        opp.call_id = "123"
        opp.source_url = "http://test"
        opp.source_title = "Title"
        opp.retrieved_at = "2026-01-01"
        markdown_text = build_markdown(doc, opp, tags=["#DePIN"])
        
        # Verify it has the sentinel because there's no technical problem
        assert "**Selected Problem**: Insufficient problem signal" in markdown_text
        
        # Write to file for normalization
        artifact_file = tmp_path / "funding.md"
        artifact_file.write_text(markdown_text, encoding="utf-8")
        
        # 2. Normalization
        norm_result = normalize_intermediate_artifact(artifact_file)
        assert norm_result["normalization_status"] == "NORMALIZED"
        assert norm_result["problem_statement"] == "Insufficient problem signal"
        
        records = build_normalized_collection_records(tmp_path)
        assert len(records) == 1
        record = records[0]
        assert record["problem_statement"] == "Insufficient problem signal"
        
        # 3. Synthesis Problem
        synth_payload = build_payload_from_manual_record(record, {"members": [record]})
        assert synth_payload["problem"] == "Insufficient problem signal"
        
        # Mock writing the synthesis artifact
        synth_file = tmp_path / "synthesis.md"
        synth_file.write_text(f"## Research Area\nDePIN\n## Problem\n{synth_payload['problem']}\n## Idea\nDo something\n", encoding="utf-8")
        
        # 4. Idea Problem
        idea_payload = build_idea_payload_from_synthesis({
            "synthesized_file": str(synth_file),
            "area": "DePIN"
        })
        assert idea_payload["problem"] == "Insufficient problem signal"
        assert not idea_payload["validation"]["checks"]["problem_present"]
        assert idea_payload["hypothesis"] == "Unresolved due to insufficient problem signal"
        
        # 5. Novelty Validation
        idea_payload = attach_external_novelty_validation(idea_payload)
        assert idea_payload["validation"]["novelty_search"]["query_executed"] is False
        assert idea_payload["validation"]["novelty_search"]["novelty_status"] == "NO_RESULT"
        assert not idea_payload["validation"]["ready_for_proposal"]
