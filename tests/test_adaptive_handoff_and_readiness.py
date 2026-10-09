import pytest
from pathlib import Path
import json
from datetime import datetime, UTC
from unittest.mock import MagicMock
from core.funding_selection import FundingCallContext
from agents.adaptive_research_agent import AdaptiveDocument
from core.agent_interface import finalize_collection_agent_response

def test_adaptive_handoff_no_live_rescue_and_provenance(tmp_path: Path):
    doc = AdaptiveDocument(
        title="Generic Policy Document",
        source="https://example.com/policy",
        content="blablabla " * 50,
        query="health policy"
    )

    ctx = MagicMock()
    ctx.funding_call_id = "CALL-123"
    ctx.funding_body = "NSF"
    ctx.program_name = "Plasma"
    ctx.research_area = "General"
    ctx.source_url = "https://example.com/nsf"

    response = finalize_collection_agent_response(
        agent="adaptive_research",
        layer="Adaptive Research",
        mode="configured_scan",
        area="Unscoped",
        documents=[doc],
        output_dir=tmp_path,
        started_at=datetime.now(UTC),
        funding_context=ctx,
        funding_contexts=[ctx]
    )

    md_files = list(tmp_path.rglob("*.md"))
    assert len(md_files) == 0, "Low quality adaptive document should have been rejected without live_source_rescue"

def test_synthesis_priority_readiness_rejection():
    from agents.synthesis_agent import build_payload_from_manual_record

    record = {
        "record_id": "r1",
        "title": "Manual Signal",
        "problem_statement": "A very basic statement without any strong negative words.",
        "layer": "Manual"
    }

    cluster = {
        "cluster_id": "test",
        "area": "Unscoped",
        "members": [record]
    }

    payload = build_payload_from_manual_record(record, cluster)
    assert payload["evidence_assessment"]["synthesis_decision"]["decision"] == "insufficient_evidence"
    assert payload["priority_readiness"]["ready_for_high_priority"] is False

def test_synthesis_priority_readiness_contested():
    from agents.synthesis_agent import build_payload_from_manual_record
    from unittest.mock import patch

    record = {
        "record_id": "r1",
        "title": "Manual Signal",
        "problem_statement": "A very basic statement.",
        "layer": "Manual"
    }

    cluster = {
        "cluster_id": "test",
        "area": "Unscoped",
        "members": [record]
    }

    with patch("agents.synthesis_agent._synthesis_decision", return_value={"decision": "contested_gap", "gap_supported": False, "confidence": 0.5}):
        payload = build_payload_from_manual_record(record, cluster)
        assert payload["priority_readiness"]["ready_for_high_priority"] is False

def test_adaptive_call_isolation(tmp_path: Path):
    from core.normalization import build_normalized_collection_records
    from unittest.mock import patch
    import json

    raw_record_1 = {
        "funding_call_ids": ["CALL-123"],
        "funding_call_id": "CALL-123",
        "source_url": "http://x",
        "evidence_id": "doc1"
    }

    raw_record_2 = {
        "funding_call_ids": ["CALL-456"],
        "funding_call_id": "CALL-456",
        "source_url": "http://x",
        "evidence_id": "doc2"
    }

    root_123 = tmp_path / "calls" / "CALL-123" / "intermediate" / "adaptive_research" / "unscoped"
    root_123.mkdir(parents=True, exist_ok=True)
    file_123 = root_123 / "doc.md"
    file_123.write_text("dummy", encoding="utf-8")

    root_456 = tmp_path / "calls" / "CALL-456" / "intermediate" / "adaptive_research" / "unscoped"
    root_456.mkdir(parents=True, exist_ok=True)
    file_456 = root_456 / "doc.md"
    file_456.write_text("dummy", encoding="utf-8")

    def fake_normalize(p: Path):
        if "CALL-123" in str(p):
            return raw_record_1
        return raw_record_2

    with patch("core.normalization.normalize_intermediate_artifact", side_effect=fake_normalize):
        errors_123 = []
        records_123 = build_normalized_collection_records([tmp_path / "intermediate", tmp_path / "calls" / "CALL-123" / "intermediate"], target_funding_call_id="CALL-123", errors=errors_123)

        assert len(records_123) == 1, f"Failed: {errors_123}"
        assert records_123[0]["funding_call_id"] == "CALL-123"

        errors_456 = []
        records_456 = build_normalized_collection_records([tmp_path / "intermediate", tmp_path / "calls" / "CALL-456" / "intermediate"], target_funding_call_id="CALL-456", errors=errors_456)

        assert len(records_456) == 1, f"Failed: {errors_456}"
        assert records_456[0]["funding_call_id"] == "CALL-456"
