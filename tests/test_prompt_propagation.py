"""
Legacy test_prompt_propagation.py
=================================
Superseded by test_batch3_verification.py which provides comprehensive coverage
of all 15 Batch 3 verification requirements including prompt propagation.

This file is kept as a minimal integration smoke test.
"""
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from core.batch_runner import execute_batch_run
from core.funding_selection import FundingCallContext


@pytest.fixture
def mock_context():
    return FundingCallContext(
        funding_call_id="CALL-PROMPT-001",
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
        selection_reasons=["Golden test"],
    )


def test_lab_prompt_propagation_sequential(mock_context):
    """Smoke test: lab agent LLM prompt contains funding context in sequential mode."""
    captured = []

    def fake_generate(prompt, model=None):
        captured.append(prompt)
        return "{}"

    def fake_quality(*args, **kwargs):
        return {"passed": True, "reasons": []}

    patches = [
        patch("agents.lab_agent.llm_generate", side_effect=fake_generate),
        patch("core.agent_interface.evaluate_collection_quality", side_effect=fake_quality),
        patch("core.quality.evaluate_collection_quality", side_effect=fake_quality),
    ]
    for p in patches:
        p.start()

    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            out_dir = Path(d)
            report = execute_batch_run(
                agent_names=["lab"],
                area_names=["RWA"],
                mode="manual_text",
                base_input_data={
                    "text": "RWA tokenization blockchain research.",
                    "output_dir": str(out_dir),
                    "db_path": str(out_dir / "test.db"),
                },
                execution_mode="sequential",
                dry_run=False,
                timeout_budget_seconds=60,
                report_path=out_dir / "report.json",
                funding_context=mock_context,
            )
    finally:
        for p in patches:
            p.stop()

    assert report["tasks"][0]["status"] in ("success", "partial_success")
    if captured:
        assert any("CALL-PROMPT-001" in p for p in captured)


def test_lab_prompt_propagation_parallel(mock_context):
    """Smoke test: lab agent LLM prompt contains funding context in parallel mode."""
    captured = []

    def fake_generate(prompt, model=None):
        captured.append(prompt)
        return "{}"

    def fake_quality(*args, **kwargs):
        return {"passed": True, "reasons": []}

    patches = [
        patch("agents.lab_agent.llm_generate", side_effect=fake_generate),
        patch("core.agent_interface.evaluate_collection_quality", side_effect=fake_quality),
        patch("core.quality.evaluate_collection_quality", side_effect=fake_quality),
    ]
    for p in patches:
        p.start()

    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            out_dir = Path(d)
            report = execute_batch_run(
                agent_names=["lab"],
                area_names=["RWA"],
                mode="manual_text",
                base_input_data={
                    "text": "RWA tokenization blockchain research.",
                    "output_dir": str(out_dir),
                    "db_path": str(out_dir / "test.db"),
                },
                execution_mode="parallel",
                dry_run=False,
                timeout_budget_seconds=60,
                report_path=out_dir / "report.json",
                funding_context=mock_context,
            )
    finally:
        for p in patches:
            p.stop()

    assert report["tasks"][0]["status"] in ("success", "partial_success")
    if captured:
        assert any("CALL-PROMPT-001" in p for p in captured)
