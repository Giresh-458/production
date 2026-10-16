from datetime import UTC, datetime
from pathlib import Path

from agents import literature_agent
from core.agent_registry import run_registered_agent
from core.funding_context import (
    build_agent_specific_funding_context,
    current_funding_prompt_context,
    current_funding_queries,
    generate_funding_queries,
)
from core.funding_selection import FundingCallContext
from core.normalization import build_normalized_collection_records, normalize_intermediate_artifact


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
        research_priorities=[
            "real-world asset tokenization",
            "secure settlement",
            "privacy-preserving verification",
            "scalable distributed systems",
        ],
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
        selection_reasons=["Deterministic multi-context literature provenance regression"],
    )


def _paper(title: str, url: str, summary: str, source_id: str) -> literature_agent.Paper:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    return literature_agent.Paper(
        title=title,
        summary=summary,
        url=url,
        published=now,
        authors=["Researcher One", "Researcher Two"],
        source="OpenAlex",
        source_id=source_id,
        doi=url.removeprefix("https://doi.org/"),
        venue="Test Research",
        retrieval_timestamp=now.isoformat(),
    )


def test_configured_literature_scan_preserves_multi_call_run_provenance(
    tmp_path: Path, monkeypatch
):
    papers = [
        _paper(
            "Scalable real-world asset tokenization and settlement",
            "https://doi.org/10.5555/rif-literature-provenance-1",
            (
                "Real-world asset tokenization systems face a scalability problem. "
                "Existing settlement systems have verification and compliance limitations, "
                "leaving a research gap in scalable privacy-preserving asset tokenization "
                "and secure distributed systems."
            ),
            "openalex:test-literature-provenance-1",
        ),
        _paper(
            "Privacy-preserving verification for tokenized assets",
            "https://doi.org/10.5555/rif-literature-provenance-2",
            (
                "Real-world asset tokenization platforms struggle with secure settlement "
                "and privacy-preserving verification. Current distributed systems show "
                "scalability and latency limitations, motivating reproducible evaluation "
                "of secure settlement and scalable asset verification."
            ),
            "openalex:test-literature-provenance-2",
        ),
    ]

    monkeypatch.setattr(literature_agent, "fetch_arxiv_entries", lambda _area: papers)
    monkeypatch.setattr(literature_agent, "fetch_openalex_entries", lambda _area: [])
    monkeypatch.setattr(literature_agent, "fetch_semanticscholar_entries", lambda _area: [])
    monkeypatch.setattr(literature_agent, "fetch_crossref_entries", lambda _area: [])
    monkeypatch.setattr(literature_agent, "fetch_dblp_entries", lambda _area: [])
    monkeypatch.setattr(literature_agent, "score_paper", lambda candidate, _area, _now: candidate)
    monkeypatch.setattr(literature_agent, "semantic_rerank", lambda candidates, _query: candidates)

    run_id = "pipeline-run-literature-provenance-001"
    call = _context()
    output_dir = tmp_path / "intermediate" / "literature"
    prompt_token = current_funding_prompt_context.set(
        build_agent_specific_funding_context("literature", call)
    )
    query_token = current_funding_queries.set(generate_funding_queries(call))
    try:
        # Production run_pipeline.py sends funding_contexts (plural) to the
        # shared collection batch, not a single funding_context.
        result = run_registered_agent(
            "literature",
            "configured_scan",
            area=None,
            input_data={
                "output_dir": str(output_dir),
                "db_path": str(tmp_path / "literature.sqlite"),
                "analysis_mode": "collect_only",
                "run_id": run_id,
                "defer_registry_sync": True,
            },
            funding_contexts=[call],
        )
    finally:
        current_funding_queries.reset(query_token)
        current_funding_prompt_context.reset(prompt_token)

    assert result["status"] == "success", result
    assert len(result["outputs"]) == 2, result
    artifact_paths = [Path(item["markdown_path"]) for item in result["outputs"]]
    assert all(path.is_file() for path in artifact_paths)

    normalized = [normalize_intermediate_artifact(path) for path in artifact_paths]
    assert {record["run_id"] for record in normalized} == {run_id}
    assert all(record["funding_call_id"] == call.funding_call_id for record in normalized)
    assert all(call.funding_call_id in record["funding_call_ids"] for record in normalized)
    assert {record["source_url"] for record in normalized} == {paper.url for paper in papers}

    # This is the exact run-scoped selector used by the pipeline. It must keep
    # each collected paper independently, not just one area-level summary.
    selected_records = build_normalized_collection_records(
        output_dir,
        target_funding_call_id=call.funding_call_id,
        target_run_id=run_id,
    )
    assert len(selected_records) == 2
    assert {record["run_id"] for record in selected_records} == {run_id}
    assert {record["funding_call_id"] for record in selected_records} == {call.funding_call_id}
    assert {record["source_url"] for record in selected_records} == {paper.url for paper in papers}
