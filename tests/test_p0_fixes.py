import pytest
import os
import json
import threading
from pathlib import Path
from core.problem_extraction import extract_problem_intelligence
from core.file_lock import atomic_write_json, _JSON_FILE_LOCK
from scripts.audit_repo import analyze_repo
from core.normalization import save_normalized_collection_records
from core.agent_interface import build_intermediate_artifact_payload

def test_problem_extraction_rejects_greeting():
    content = "Greetings from XYZ Foundation. The current verification architecture cannot process high-volume transactions within the required latency. Please contact us for more information."
    evidence_bundle = {"evidence_snippets": []}
    
    intel = extract_problem_intelligence(content, "Test Title", evidence_bundle, allow_llm_refinement=False)
    
    assert intel["selected_problem"] is not None
    assert "cannot process high-volume transactions" in intel["selected_problem"]
    assert "Greetings" not in intel["selected_problem"]
    assert len(intel["problem_candidates"]) > 0

def test_problem_extraction_boilerplate_rejection():
    content = "Welcome to our website. All rights reserved. Cookie policy is updated. Log in to read more. We are happy to see you. Please click here to subscribe."
    intel = extract_problem_intelligence(content, "Test Title", {}, allow_llm_refinement=False)
    
    # Should fallback because everything is boilerplate or lacks problem signal
    assert intel["problem_type"] == "unresolved"
    assert intel["selected_problem"] is None

def test_evidence_traceability():
    content = "Verification latency becomes impractical above 100K transactions."
    intel = extract_problem_intelligence(content, "Test Title", {}, allow_llm_refinement=False)
    
    # Selected problem must have corresponding evidence
    assert intel["problem_evidence"] == [content]

def test_canonical_document_identity():
    payload1, _ = build_intermediate_artifact_payload(
        agent="funding", layer="Funding", mode="manual_text", area="RWA", 
        title="Doc", source="http://a.com", content="Hello world"
    )
    payload2, _ = build_intermediate_artifact_payload(
        agent="funding", layer="Funding", mode="manual_text", area="RWA", 
        title="Doc", source="http://a.com", content="Hello world"
    )
    payload3, _ = build_intermediate_artifact_payload(
        agent="funding", layer="Funding", mode="manual_text", area="RWA", 
        title="Doc", source="http://b.com", content="Hello world"
    )
    
    assert payload1["provenance"]["document_identity"] == payload2["provenance"]["document_identity"]
    assert payload1["provenance"]["document_identity"] != payload3["provenance"]["document_identity"]
    assert payload1["provenance"]["content_hash"] == payload3["provenance"]["content_hash"]

def test_concurrent_registry_updates(tmp_path):
    registry_file = tmp_path / "registry.json"
    registry_file.write_text('{"count": 0}')
    
    def worker():
        for _ in range(10):
            with _JSON_FILE_LOCK:
                data = json.loads(registry_file.read_text())
                data["count"] += 1
                atomic_write_json(registry_file, data)
            
    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join()
    
    data = json.loads(registry_file.read_text())
    assert data["count"] == 50

def test_audit_script_execution():
    analyze_repo()
    assert os.path.exists("docs/REPOSITORY_CLEANUP_AUDIT.md")


def test_normalization_behavior(tmp_path):
    import json
    from core.normalization import save_normalized_collection_records

    outputs = tmp_path / "outputs"
    intermediate = outputs / "intermediate" / "test_area"
    intermediate.mkdir(parents=True)
    
    # Create doc A version 1
    doc_a_v1 = intermediate / "doc_a_v1.md"
    doc_a_v1.write_text('''# Doc A
## Shared Metadata
- Source: http://doc.a
- Title: Doc A
- Agent Name: funding
- Collection Mode: manual
- Collected At: 2023-01-01T00:00:00Z
## Problem Intelligence
**Selected Problem**: Problem V1
**Mode**: deterministic
## Evidence Bundle
### Page Title
Doc A
''')

    save_normalized_collection_records(outputs, outputs)
    records = json.loads((outputs / "normalized" / "records.json").read_text())
    assert len(records) == 1
    assert records[0]["observation_count"] == 1
    assert records[0]["problem_statement"] == "Problem V1"
    
    # Case A: Identical observation
    doc_a_dup = intermediate / "doc_a_dup.md"
    doc_a_dup.write_text(doc_a_v1.read_text().replace("2023-01-01", "2023-01-02"))
    
    save_normalized_collection_records(outputs, outputs)
    records = json.loads((outputs / "normalized" / "records.json").read_text())
    assert len(records) == 1
    assert records[0]["observation_count"] == 2
    assert len(records[0]["content_versions"]) == 1
    
    # Case B: Updated document (content changes)
    doc_a_v2 = intermediate / "doc_a_v2.md"
    doc_a_v2.write_text(doc_a_v1.read_text().replace("Problem V1", "Problem V2").replace("2023-01-01", "2023-01-03"))
    
    save_normalized_collection_records(outputs, outputs)
    records = json.loads((outputs / "normalized" / "records.json").read_text())
    assert len(records) == 1
    assert records[0]["observation_count"] == 3
    assert len(records[0]["content_versions"]) == 2
    assert records[0]["problem_statement"] == "Problem V2"
    
    # Case C: Genuinely different document
    doc_b = intermediate / "doc_b.md"
    doc_b.write_text(doc_a_v1.read_text().replace("http://doc.a", "http://doc.b").replace("Doc A", "Doc B"))
    
    save_normalized_collection_records(outputs, outputs)
    records = json.loads((outputs / "normalized" / "records.json").read_text())
    assert len(records) == 2


def test_semantic_regression_plausible_first_sentence():
    content = "ABC Labs develops scalable verification infrastructure. Our current verifier cannot maintain target latency at high transaction volume."
    intel = extract_problem_intelligence(content, "Test Title", {}, allow_llm_refinement=False)
    
    assert intel["selected_problem"] is not None
    assert "cannot maintain target latency" in intel["selected_problem"]
    assert "ABC Labs develops" not in intel["selected_problem"]

def test_structured_problem_signals_are_evidence_grounded():
    cases = [
        ("hackathon", "DID", "Challenge track: build a decentralized identity wallet that issues and verifies verifiable credentials.", "challenge_statement"),
        ("failure", "Stablecoins", "Incident report: a stablecoin depeg triggered a liquidation cascade after reserve confidence weakened.", "incident"),
        ("data_availability", "ZK-IoV", "Dataset and benchmark description: a mobility-trace simulator plus proving-time benchmark provides verifier latency metrics.", "benchmark_need"),
    ]
    for agent, area, text, expected_type in cases:
        intel = extract_problem_intelligence(text, "Structured Signal", {}, agent_name=agent, research_area=area, allow_llm_refinement=False)
        assert intel["selected_problem"] == text
        assert intel["problem_type"] == expected_type
        assert intel["problem_evidence"] == [text]


def test_structured_problem_signal_does_not_promote_generic_prose():
    text = "One of the key concepts of Construction 4.0 is cyber-physical systems and connected infrastructure."
    intel = extract_problem_intelligence(text, "Background", {}, agent_name="hackathon", research_area="DigitalHealthCPS", allow_llm_refinement=False)
    assert intel["selected_problem"] is None


def test_funding_markdown_handles_empty_inferred_challenges():
    from agents.funding_agent import FundingDocument, FundingOpportunity, build_markdown

    document = FundingDocument(
        organization="Test Foundation", title="Test Call", source="Manual", source_type="Manual",
        content="Funding supports a prototype and evaluation plan.", year=2026,
    )
    analysis = FundingOpportunity(
        funding_body="Test Foundation", program_name="Test Call", call_id=None, status="OPEN",
        opening_date=None, deadline=None, funding_amount=None, currency=None, duration=None,
        eligibility=None, geography=None, trl=None, research_priorities=[], required_partners=None,
        deliverables=None, evaluation_criteria=None, eligible_costs=None, application_url=None,
        opportunity_type="RFP", focus_area="RWA", keywords=[], research_context={
            "inferred_challenges": [], "investigation_questions": []
        }, source_method="deterministic", source_url="Manual", source_title="Test Call",
        retrieved_at="2026-09-14T00:00:00+00:00", published_at=None, last_verified_at="2026-09-14T00:00:00+00:00",
    )
    markdown = build_markdown(document, analysis, ["#Funding"])
    assert "## Research Mission" in markdown
    assert "Unknown" in markdown

