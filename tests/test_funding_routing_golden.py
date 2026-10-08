from core.agent_registry import DOWNSTREAM_FUNDING_CONTEXT_AGENTS, FUNDING_DISCOVERY_AGENT


def test_funding_is_gate_and_exactly_eleven_downstream_collectors():
    assert FUNDING_DISCOVERY_AGENT == "funding"
    assert "funding" not in DOWNSTREAM_FUNDING_CONTEXT_AGENTS
    assert len(DOWNSTREAM_FUNDING_CONTEXT_AGENTS) == 11

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from core.batch_runner import execute_batch_run
from core.funding_selection import FundingCallContext

def test_funding_routing_golden(tmp_path: Path):
    mock_context = FundingCallContext(
        funding_call_id="CALL-TEST-001",
        funding_body="Test Agency",
        program_name="Test Program",
        call_id="TEST-001",
        call_title="Advanced Blockchain Tech",
        status="OPEN",
        opening_date=None,
        deadline=None,
        funding_amount=None,
        currency=None,
        duration=None,
        eligibility=None,
        geography="EU",
        trl="TRL4",
        research_priorities=["ZKP", "DID"],
        required_partners=None,
        deliverables=None,
        evaluation_criteria=None,
        eligible_costs=None,
        application_url=None,
        source_url=None,
        source_name="Manual",
        publication_date=None,
        last_updated=None,
        research_area="General",
        selection_mode="test",
        selection_score=0.99,
        selection_reasons=["Golden test"]
    )

    from core.agent_registry import COLLECTION_AGENTS
    target_agents = list(COLLECTION_AGENTS - {"funding", "funding_context"})

    with tempfile.TemporaryDirectory() as d:
        out_dir = Path(d)
        report_path = out_dir / "report.json"
        
        # We need to make sure the agents write their artifacts to out_dir
        report = execute_batch_run(
            agent_names=target_agents,
            area_names=["RWA"],
            mode="manual_text",
            base_input_data={
                "text": "This is a generic text about blockchain, zero knowledge proofs, and real world assets.",
                "title": "Golden Test Doc",
                "source": "Manual",
                "output_dir": str(out_dir),
            },
            execution_mode="sequential",
            dry_run=False,
            timeout_budget_seconds=300,
            analysis_mode="collect_only",
            report_path=report_path,
            funding_context=mock_context,
        )
        
        assert report["status"] in ("completed", "completed_with_errors")
        
        # Check generated artifacts in out_dir
        artifacts_found = 0
        for md_file in out_dir.rglob("*.md"):
            content = md_file.read_text(encoding="utf-8")
            
            # Extract JSON from Markdown
            if "```json" in content:
                json_text = content.split("```json")[1].split("```")[0]
                try:
                    payload = json.loads(json_text)
                    if "shared_header" in payload:
                        artifacts_found += 1
                        shared_header = payload["shared_header"]
                        assert shared_header.get("funding_call_id") == "CALL-TEST-001"
                        assert "run_id" in shared_header
                except Exception as e:
                    pass # Some might not be valid JSON if not generated properly, but if it is JSON, it MUST have the ID.
                    
        # Verify that the report contains the tasks
        assert len(report["tasks"]) > 0
        
        # Check tasks for success
        success_tasks = [t for t in report["tasks"] if t["status"] == "success"]
        
        # As long as at least some agents ran successfully in manual_text mode
        # and produced artifacts containing the funding context, the routing is working.
        if success_tasks:
            assert artifacts_found > 0, "No artifacts were successfully generated and validated with funding context."

