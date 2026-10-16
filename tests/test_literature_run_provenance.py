from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from agents import literature_agent
from agents.db import db_connect, init_papers_db, save_paper_record
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
    other_call = replace(
        call,
        funding_call_id="CALL-LITERATURE-OTHER-002",
        funding_body="Unrelated Test Agency",
        program_name="Methane Monitoring Research",
        call_id="LITERATURE-OTHER-002",
        call_title="Satellite detection of methane emissions",
        research_area="ESG",
        research_priorities=["methane detection", "carbon accounting", "satellite emissions"],
        selection_reasons=["Unrelated attribution guard"],
    )
    contexts = [call, other_call]
    # Seed the persistent cross-run ledger with the same records. A run-scoped
    # collection must re-emit evidence into this run even if the DB knew it
    # already, because normalization intentionally drops prior-run artifacts.
    db_path = tmp_path / "literature.sqlite"
    connection = db_connect(db_path)
    init_papers_db(connection)
    try:
        for paper in papers:
            save_paper_record(
                connection,
                title=paper.title,
                summary=paper.summary,
                url=paper.url,
                authors="; ".join(paper.authors),
                year=paper.published.year if paper.published else 0,
                research_area="RWA",
                path=str(tmp_path / "prior-run" / "paper.md"),
                tags=["#Literature", "#RWA"],
                concept_tags=[],
                static_tags_by_area=literature_agent.STATIC_TAGS,
                source_id=paper.source_id,
                doi=paper.doi,
            )
    finally:
        connection.close()

    prompt_value = "\\n\\n---\\n\\n".join(
        build_agent_specific_funding_context("literature", ctx) for ctx in contexts
    )
    query_values = []
    for ctx in contexts:
        query_values.extend(generate_funding_queries(ctx))
    prompt_token = current_funding_prompt_context.set(prompt_value)
    query_token = current_funding_queries.set(list(set(query_values)))
    try:
        # Production run_pipeline.py sends funding_contexts (plural) to the
        # shared collection batch, not a single funding_context.
        result = run_registered_agent(
            "literature",
            "configured_scan",
            area=None,
            input_data={
                "output_dir": str(output_dir),
                "db_path": str(db_path),
                "analysis_mode": "collect_only",
                "run_id": run_id,
                "defer_registry_sync": True,
            },
            funding_contexts=contexts,
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
    assert all(other_call.funding_call_id not in record["funding_call_ids"] for record in normalized)
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
    unrelated_records = build_normalized_collection_records(
        output_dir,
        target_funding_call_id=other_call.funding_call_id,
        target_run_id=run_id,
    )
    assert unrelated_records == []
