import pytest
from pathlib import Path
import json
from datetime import datetime, UTC
from unittest.mock import MagicMock
from core.funding_selection import FundingCallContext
from agents.adaptive_research_agent import AdaptiveDocument
from core.agent_interface import finalize_collection_agent_response, save_intermediate_markdown

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

def test_adaptive_filename_collisions(tmp_path: Path):
    # Test stable filename logic in save_intermediate_markdown
    common_fields = {
        "metadata": {"quality_gate": {"passed": True}},
        "problem_intelligence": {},
        "evidence_bundle": {"page_title": "", "section_headings": [], "key_paragraphs": [], "bullet_lists": [], "quoted_passages": [], "links_found": [], "named_entities_or_programs": [], "evidence_snippets": [], "excerpt_windows": []},
        "agent_specific_body": {}
    }

    # Document A and B have identical title and URL but different content
    payload_a = dict(common_fields)
    payload_a.update({
        "shared_header": {"agent_name": "adaptive_research", "collection_mode": "configured_scan", "layer": "Adaptive Research", "collected_at": "2026-10-10T00:00:00Z", "title": "Same Title", "research_area": "Unscoped", "source": "http://example.com/shared"},
        "provenance": {"content_hash": "hash_a"},
        "raw_content": "Content A"
    })

    payload_b = dict(common_fields)
    payload_b.update({
        "shared_header": {"agent_name": "adaptive_research", "collection_mode": "configured_scan", "layer": "Adaptive Research", "collected_at": "2026-10-10T00:00:00Z", "title": "Same Title", "research_area": "Unscoped", "source": "http://example.com/shared"},
        "provenance": {"content_hash": "hash_b"},
        "raw_content": "Content B"
    })

    # Document C has different URL
    payload_c = dict(common_fields)
    payload_c.update({
        "shared_header": {"agent_name": "adaptive_research", "collection_mode": "configured_scan", "layer": "Adaptive Research", "collected_at": "2026-10-10T00:00:00Z", "title": "Same Title", "research_area": "Unscoped", "source": "http://example.com/other"},
        "provenance": {"content_hash": "hash_c"},
        "raw_content": "Content C"
    })

    # Document D has NO URL
    payload_d = dict(common_fields)
    payload_d.update({
        "shared_header": {"agent_name": "adaptive_research", "collection_mode": "configured_scan", "layer": "Adaptive Research", "collected_at": "2026-10-10T00:00:00Z", "title": "Same Title", "research_area": "Unscoped", "source": ""},
        "provenance": {"content_hash": "hash_d"},
        "raw_content": "Content D"
    })

    dir_ab = tmp_path / "order_ab"
    dir_ba = tmp_path / "order_ba"
    dir_ab.mkdir()
    dir_ba.mkdir()

    # Write A then B
    path_ab_a = save_intermediate_markdown(payload=payload_a, output_dir=dir_ab)
    path_ab_b = save_intermediate_markdown(payload=payload_b, output_dir=dir_ab)

    # Write B then A
    path_ba_b = save_intermediate_markdown(payload=payload_b, output_dir=dir_ba)
    path_ba_a = save_intermediate_markdown(payload=payload_a, output_dir=dir_ba)

    # Assert deterministic paths regardless of order
    assert path_ab_a.name == path_ba_a.name, "File A should have the same deterministic path in both orders"
    assert path_ab_b.name == path_ba_b.name, "File B should have the same deterministic path in both orders"
    assert path_ab_a.name != path_ab_b.name, "Distinct documents with same title/URL must get distinct paths"

    # Verify contents remain intact in both directories
    assert "Content A" in path_ab_a.read_text(encoding="utf-8")
    assert "Content B" in path_ab_b.read_text(encoding="utf-8")
    assert "Content A" in path_ba_a.read_text(encoding="utf-8")
    assert "Content B" in path_ba_b.read_text(encoding="utf-8")

    # Rewriting A or B does not alter files or duplicate
    path_rewrite = save_intermediate_markdown(payload=payload_a, output_dir=dir_ab)
    assert path_rewrite == path_ab_a
    assert "Content A" in path_rewrite.read_text(encoding="utf-8")
    assert len(list(dir_ab.rglob("*.md"))) == 2

    # Different URLs remain distinguishable
    path_c = save_intermediate_markdown(payload=payload_c, output_dir=dir_ab)
    assert path_c.name != path_ab_a.name

    # Verify hash lengths: base-url(16)-content(16)
    parts_a = path_ab_a.stem.split("-")
    assert len(parts_a[-1]) == 16, "Content fingerprint should be 16 chars"
    assert len(parts_a[-2]) == 16, "URL fingerprint should be 16 chars"

    # Missing URLs still produce deterministic paths (base-content(16))
    path_d1 = save_intermediate_markdown(payload=payload_d, output_dir=dir_ab)
    path_d2 = save_intermediate_markdown(payload=payload_d, output_dir=dir_ba)
    assert path_d1.name == path_d2.name
    assert path_d1.name != path_ab_a.name
    parts_d = path_d1.stem.split("-")
    assert len(parts_d[-1]) == 16, "Content fingerprint should be 16 chars (no url hash)"

    # Test semantic payload hash fallback when raw_content is missing
    payload_semantic_1 = dict(common_fields)
    payload_semantic_1.update({
        "shared_header": {"agent_name": "adaptive_research", "collection_mode": "configured_scan", "layer": "Adaptive Research", "collected_at": "2026-10-10T00:00:01Z", "title": "No Raw Content", "research_area": "Unscoped", "source": ""},
        "raw_content": "",
        "provenance": {},
        "evidence_bundle": {"page_title": "Page 1"}
    })
    payload_semantic_2 = dict(common_fields)
    payload_semantic_2.update({
        "shared_header": {"agent_name": "adaptive_research", "collection_mode": "configured_scan", "layer": "Adaptive Research", "collected_at": "2026-10-10T23:59:59Z", "title": "No Raw Content", "research_area": "Unscoped", "source": ""},
        "raw_content": "",
        "provenance": {},
        "evidence_bundle": {"page_title": "Page 1"}
    })
    payload_semantic_3 = dict(common_fields)
    payload_semantic_3.update({
        "shared_header": {"agent_name": "adaptive_research", "collection_mode": "configured_scan", "layer": "Adaptive Research", "collected_at": "2026-10-10T23:59:59Z", "title": "No Raw Content", "research_area": "Unscoped", "source": ""},
        "raw_content": "",
        "provenance": {},
        "evidence_bundle": {"page_title": "Page 2"} # different semantic content
    })

    from unittest.mock import patch
    with patch("core.agent_interface.validate_intermediate_artifact", return_value=[]):
        path_sem_1 = save_intermediate_markdown(payload=payload_semantic_1, output_dir=dir_ab)
        path_sem_2 = save_intermediate_markdown(payload=payload_semantic_2, output_dir=dir_ab)
        path_sem_3 = save_intermediate_markdown(payload=payload_semantic_3, output_dir=dir_ab)

    assert path_sem_1.name == path_sem_2.name, "Timestamps should not change the identity hash when raw_content is missing"
    assert path_sem_1.name != path_sem_3.name, "Different semantic content should produce different hashes"

def test_adaptive_real_handoff_and_isolation(tmp_path: Path):
    from core.normalization import build_normalized_collection_records
    from agents.synthesis_agent import build_payload_from_cluster

    # Real pipeline run using finalize_collection_agent_response to create real markdown
    doc1 = AdaptiveDocument(
        title="Unique Doc For Call 123",
        source="https://example.com/doc1",
        content="This is an actually valid document. It details significant limitations in current methodology, meaning a major gap exists in the field.",
        query="methodology gap"
    )
    doc2 = AdaptiveDocument(
        title="Unique Doc For Call 456",
        source="https://example.com/doc2",
        content="This is another valid document. It details significant limitations in current methodology, meaning a major gap exists in the field.",
        query="methodology gap"
    )

    ctx1 = MagicMock()
    ctx1.funding_call_id = "CALL-123"
    ctx1.funding_body = "NSF"
    ctx1.program_name = "Plasma"
    ctx1.research_area = "General"
    ctx1.source_url = "https://example.com/nsf1"

    ctx2 = MagicMock()
    ctx2.funding_call_id = "CALL-456"
    ctx2.funding_body = "NSF"
    ctx2.program_name = "Plasma"
    ctx2.research_area = "General"
    ctx2.source_url = "https://example.com/nsf2"

    dir_123 = tmp_path / "calls" / "CALL-123" / "intermediate"
    dir_456 = tmp_path / "calls" / "CALL-456" / "intermediate"

    # Create the markdown using the real formatting path
    finalize_collection_agent_response(
        agent="adaptive_research", layer="Adaptive Research", mode="configured_scan", area="Unscoped",
        documents=[doc1], output_dir=dir_123, started_at=datetime.now(UTC), funding_context=ctx1, funding_contexts=[ctx1]
    )

    finalize_collection_agent_response(
        agent="adaptive_research", layer="Adaptive Research", mode="configured_scan", area="Unscoped",
        documents=[doc2], output_dir=dir_456, started_at=datetime.now(UTC), funding_context=ctx2, funding_contexts=[ctx2]
    )

    # Normalize for CALL-123 (passing both roots to match run_pipeline.py logic)
    records_123 = build_normalized_collection_records([tmp_path / "intermediate", dir_123], target_funding_call_id="CALL-123")
    assert len(records_123) == 1
    assert records_123[0]["funding_call_id"] == "CALL-123"
    assert records_123[0]["source_url"] == "https://example.com/doc1"

    # Normalize for CALL-456
    records_456 = build_normalized_collection_records([tmp_path / "intermediate", dir_456], target_funding_call_id="CALL-456")
    assert len(records_456) == 1
    assert records_456[0]["funding_call_id"] == "CALL-456"
    assert records_456[0]["source_url"] == "https://example.com/doc2"

    # Prove that the evidence reaches downstream synthesis input correctly
    # build_payload_from_cluster needs a cluster definition containing the normalized record
    md_file_path = str(list(dir_123.rglob("*.md"))[0])
    cluster = {
        "cluster_id": "test-cluster",
        "area": "Unscoped",
        "representative_problem": "methodology gap",
        "members": [records_123[0]],
        "evidence_links": [md_file_path],
        "supporting_layers": ["Adaptive Research"]
    }

    # Pass outputs_root
    payload = build_payload_from_cluster(cluster, tmp_path)

    # The provenance in synthesis must reflect the adaptive document
    assert payload["evidence_assessment"]["synthesis_decision"]["decision"] != "uncertain", "Should evaluate the evidence properly"
    assert "https://example.com/doc1" in str(payload), "The source URL must survive and be passed into synthesis payload"
