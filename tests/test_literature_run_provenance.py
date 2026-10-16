from datetime import UTC, datetime
from pathlib import Path

from agents import literature_agent
from core.agent_registry import run_registered_agent
from core.funding_selection import FundingCallContext
from core.normalization import normalize_intermediate_artifact


def _context() -> FundingCallContext:
    return FundingCallContext(
        funding_call_id="CALL-LITERATURE-PROVENANCE-001",
        funding_body="Test Research Agency",
        program_name="RWA Verification Research",
        call_id="LITERATURE-PROVENANCE-001",
        call_title="Scalable real-world asset tokenization and settlement",
        status="OPEN",
        opening_date="2026-01-01",
        deadline="2027-12-31",
        funding_amount="1000000",
        currency="EUR",
        duration="24 months",
        eligibility="Universities and research organizations in eligible jurisdictions.",
        geography="EU",
        trl="TRL3-TRL5",
        research_priorities=["real-world asset tokenization", "secure settlement"],
        required_partners="Research and industry",
        deliverables="Prototype and benchmark report",
        evaluation_criteria="Excellence, impact, implementation",
        eligible_costs="Personnel, equipment, travel",
        application_url="https://example.org/apply",
        source_url="https://example.org/call",
        source_name="Test Research Agency",
        publication_date="2026-01-01",
        last_updated="2026-01-01",
        research_area="RWA",
        selection_mode="test",
        selection_score=1.0,
        selection_reasons=["Deterministic literature provenance regression"],
    )


def test_configured_literature_scan_preserves_run_and_call_provenance(tmp_path: Path, monkeypatch):
    now = datetime(2026, 9, 1, tzinfo=UTC)
    paper = literature_agent.Paper(
        title="Scalable real-world asset tokenization and settlement",
        summary=(
            "Real-world asset tokenization systems face a scalability problem. "
            "Existing settlement systems have verification and compliance limitations, "
            "leaving a research gap in scalable privacy-preserving asset tokenization."
        ),
        url="https://doi.org/10.5555/rif-literature-provenance",
        published=now,
        authors=["Researcher One", "Researcher Two"],
        source="OpenAlex",
        source_id="openalex:test-literature-provenance",
        doi="10.5555/rif-literature-provenance",
        venue="Test Research",
        retrieval_timestamp=now.isoformat(),
    )

    monkeypatch.setattr(literature_agent, "fetch_arxiv_entries", lambda _area: [paper])
    monkeypatch.setattr(literature_agent, "fetch_openalex_entries", lambda _area: [])
    monkeypatch.setattr(literature_agent, "fetch_semanticscholar_entries", lambda _area: [])
    monkeypatch.setattr(literature_agent, "fetch_crossref_entries", lambda _area: [])
    monkeypatch.setattr(literature_agent, "fetch_dblp_entries", lambda _area: [])
    monkeypatch.setattr(literature_agent, "semantic_rerank", lambda papers, _query: papers)

    run_id = "pipeline-run-literature-provenance-001"
    call = _context()
    output_dir = tmp_path / "intermediate" / "literature"
    result = run_registered_agent(
        "literature",
        "configured_scan",
        area="RWA",
        input_data={
            "output_dir": str(output_dir),
            "db_path": str(tmp_path / "literature.sqlite"),
            "analysis_mode": "collect_only",
            "run_id": run_id,
            "defer_registry_sync": True,
        },
        funding_context=call,
    )

    assert result["status"] == "success", result
    assert result["outputs"], result
    artifact_path = Path(result["outputs"][0]["markdown_path"])
    assert artifact_path.is_file()

    normalized = normalize_intermediate_artifact(artifact_path)
    assert normalized["run_id"] == run_id
    assert normalized["funding_call_id"] == call.funding_call_id
    assert call.funding_call_id in normalized["funding_call_ids"]
    assert normalized["source_url"] == paper.url
