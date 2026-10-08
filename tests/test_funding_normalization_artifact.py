import pytest
from pathlib import Path
from agents.funding_agent import build_markdown, FundingDocument, FundingOpportunity
from core.normalization import normalize_intermediate_artifact, build_normalized_collection_records

def test_funding_artifact_normalization(tmp_path: Path):
    doc = FundingDocument(
        source="https://example.com",
        source_type="API",
        title="Test Doc",
        content="This is the raw content.",
        organization="Test Org",
        year="2026"
    )
    analysis = FundingOpportunity(
        funding_body="Test Body",
        program_name="Test Program",
        call_id="CALL-12345",
        status="OPEN_CALL",
        opening_date="2026-01-01",
        deadline="2026-12-31",
        funding_amount="$1M",
        currency="USD",
        duration="1 year",
        eligibility="Everyone",
        geography="Global",
        trl="1-3",
        research_priorities=["Blockchain"],
        required_partners="None",
        deliverables="Code",
        evaluation_criteria="Novelty",
        eligible_costs="Salary",
        application_url="https://example.com/apply",
        opportunity_type="Grant",
        focus_area="DePIN",
        keywords=["IoT"],
        research_context={"inferred_challenges": ["Security"], "investigation_questions": ["How?"]},
        source_method="api",
        source_url="https://example.com",
        source_title="Test Source",
        retrieved_at="2026-10-08T00:00:00Z",
        published_at="2026-01-01T00:00:00Z",
        last_verified_at="2026-10-08T00:00:00Z",
        evidence={"Mission": "Solve X."}
    )
    tags = ["#DePIN"]
    run_id = "RUN-999"
    
    md_content = build_markdown(doc, analysis, tags, run_id=run_id)
    
    md_path = tmp_path / "test_artifact.md"
    md_path.write_text(md_content, encoding="utf-8")
    
    # 1. normalize_intermediate_artifact() returns analysis.call_id
    # 2. source_url is populated
    # 3. evidence_snippets is non-empty
    norm = normalize_intermediate_artifact(md_path)
    
    assert norm["funding_call_id"] == "CALL-12345"
    assert norm["source_url"] == "https://example.com"
    assert len(norm["evidence_snippets"]) > 0
    assert "Solve X." in norm["evidence_snippets"][0]
    
    # 4. normalization_status is NORMALIZED
    # Wait, normalization_status is determined in build_normalized_collection_records.
    
    records = build_normalized_collection_records(md_path.parent, target_funding_call_id="CALL-12345")
    assert len(records) == 1
    assert records[0]["normalization_status"] == "NORMALIZED"
    assert records[0]["funding_call_id"] == "CALL-12345"
