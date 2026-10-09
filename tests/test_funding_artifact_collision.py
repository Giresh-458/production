import pytest
from pathlib import Path
from agents.funding_agent import save_markdown_output, FundingDocument, FundingOpportunity
from core.normalization import build_normalized_collection_records

def test_funding_artifact_collision_safe(tmp_path: Path):
    # Two documents with the same organization and title but different IDs and URLs
    doc1 = FundingDocument(
        organization="Grants.gov",
        title="Shared Program Title",
        source="https://www.grants.gov/search-results-detail/357282",
        source_type="API",
        content="Call 1 content",
        year=2026
    )
    analysis1 = FundingOpportunity(
        funding_body="Grants.gov",
        program_name="Shared Program Title",
        call_id="CALL-5AD5E7593144",
        status="OPEN",
        opening_date=None,
        deadline=None,
        funding_amount=None,
        currency=None,
        duration=None,
        eligibility=None,
        geography=None,
        trl=None,
        research_priorities=["Priority 1"],
        required_partners=None,
        deliverables=None,
        evaluation_criteria=None,
        eligible_costs=None,
        application_url=None,
        opportunity_type="Grant",
        focus_area="General",
        keywords=[],
        research_context={"technology_themes": [], "inferred_challenges": [], "investigation_questions": []},
        source_method="deterministic",
        source_url=doc1.source,
        source_title=doc1.title,
        retrieved_at="2026-10-10",
        published_at=None,
        last_verified_at="2026-10-10"
    )

    doc2 = FundingDocument(
        organization="Grants.gov",
        title="Shared Program Title",
        source="https://www.grants.gov/search-results-detail/354081",
        source_type="API",
        content="Call 2 content",
        year=2026
    )
    analysis2 = FundingOpportunity(
        funding_body="Grants.gov",
        program_name="Shared Program Title",
        call_id="CALL-78164EE906DE",
        status="OPEN",
        opening_date=None,
        deadline=None,
        funding_amount=None,
        currency=None,
        duration=None,
        eligibility=None,
        geography=None,
        trl=None,
        research_priorities=["Priority 2"],
        required_partners=None,
        deliverables=None,
        evaluation_criteria=None,
        eligible_costs=None,
        application_url=None,
        opportunity_type="Grant",
        focus_area="General",
        keywords=[],
        research_context={"technology_themes": [], "inferred_challenges": [], "investigation_questions": []},
        source_method="deterministic",
        source_url=doc2.source,
        source_title=doc2.title,
        retrieved_at="2026-10-10",
        published_at=None,
        last_verified_at="2026-10-10"
    )

    # Save first artifact
    path1 = save_markdown_output(doc1, analysis1, tmp_path, run_id="run_test")
    
    # Save second artifact
    path2 = save_markdown_output(doc2, analysis2, tmp_path, run_id="run_test")
    
    # 1. Distinct paths
    assert path1 != path2
    
    # 2. Both files exist
    assert path1.exists()
    assert path2.exists()
    
    # 3. Read back text to verify unique attributes
    text1 = path1.read_text(encoding="utf-8")
    text2 = path2.read_text(encoding="utf-8")
    assert "CALL-5AD5E7593144" in text1
    assert doc1.source in text1
    assert "CALL-78164EE906DE" in text2
    assert doc2.source in text2
    
    # 4. Normalize independently
    records1 = build_normalized_collection_records(tmp_path, target_funding_call_id="CALL-5AD5E7593144")
    records2 = build_normalized_collection_records(tmp_path, target_funding_call_id="CALL-78164EE906DE")
    
    assert len(records1) == 1
    assert records1[0]["source_url"] == doc1.source
    
    assert len(records2) == 1
    assert records2[0]["source_url"] == doc2.source

def test_funding_artifact_collision_fallback(tmp_path: Path):
    # What if call_id is None?
    doc1 = FundingDocument(
        organization="Grants.gov",
        title="Shared Title",
        source="https://test1.com",
        source_type="Manual",
        content="1",
        year=2026
    )
    analysis1 = FundingOpportunity(
        funding_body="Grants.gov",
        program_name="Shared Title",
        call_id=None,
        status="OPEN",
        opening_date=None,
        deadline=None,
        funding_amount=None,
        currency=None,
        duration=None,
        eligibility=None,
        geography=None,
        trl=None,
        research_priorities=[],
        required_partners=None,
        deliverables=None,
        evaluation_criteria=None,
        eligible_costs=None,
        application_url=None,
        opportunity_type=None,
        focus_area="General",
        keywords=[],
        research_context={"technology_themes": [], "inferred_challenges": [], "investigation_questions": []},
        source_method="deterministic",
        source_url=doc1.source,
        source_title=doc1.title,
        retrieved_at="2026-10-10",
        published_at=None,
        last_verified_at="2026-10-10"
    )

    doc2 = FundingDocument(
        organization="Grants.gov",
        title="Shared Title",
        source="https://test2.com",
        source_type="Manual",
        content="2",
        year=2026
    )
    analysis2 = FundingOpportunity(
        funding_body="Grants.gov",
        program_name="Shared Title",
        call_id=None,
        status="OPEN",
        opening_date=None,
        deadline=None,
        funding_amount=None,
        currency=None,
        duration=None,
        eligibility=None,
        geography=None,
        trl=None,
        research_priorities=[],
        required_partners=None,
        deliverables=None,
        evaluation_criteria=None,
        eligible_costs=None,
        application_url=None,
        opportunity_type=None,
        focus_area="General",
        keywords=[],
        research_context={"technology_themes": [], "inferred_challenges": [], "investigation_questions": []},
        source_method="deterministic",
        source_url=doc2.source,
        source_title=doc2.title,
        retrieved_at="2026-10-10",
        published_at=None,
        last_verified_at="2026-10-10"
    )

    path1 = save_markdown_output(doc1, analysis1, tmp_path)
    path2 = save_markdown_output(doc2, analysis2, tmp_path)
    
    assert path1 != path2
    assert path1.exists()
    assert path2.exists()
