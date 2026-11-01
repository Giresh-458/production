from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.agent_registry import DOWNSTREAM_FUNDING_CONTEXT_AGENTS
from core.batch2 import execute_batch2
from core.funding_selection import FundingCallContext
from core.research_schemas import FundingResearchContext
from core.normalization import resolve_refresh_context


def _context(call_id: str = "CALL-BATCH2-001") -> FundingCallContext:
    return FundingCallContext(
        funding_call_id=call_id,
        funding_body="Batch-1 Test Agency",
        program_name="Privacy-preserving settlement research",
        call_id=call_id,
        call_title="Privacy-preserving settlement research",
        status="OPEN_CALL",
        opening_date="2026-01-01",
        deadline="2027-12-31",
        funding_amount="1000000",
        currency="EUR",
        duration="24 months",
        eligible_costs="Personnel and equipment",
        eligibility="Universities and research organizations may apply.",
        geography="EU",
        trl="TRL3-TRL5",
        research_priorities=["zero knowledge verification"],
        required_partners="Research and industry partners",
        deliverables="Prototype and benchmark report",
        evaluation_criteria="Excellence and implementation",
        application_url="https://example.org/apply",
        source_url="https://example.org/funding-call",
        source_name="Batch-1 Test Agency",
        publication_date="2026-01-01",
        last_updated="2026-01-01",
        research_area="RWA",
        selection_mode="autonomous",
        selection_score=0.95,
        selection_reasons=["Validated Batch-1 fixture"],
        research_context=FundingResearchContext(
            funding_call_id=call_id,
            domain="RWA",
            technology_themes=("zero knowledge verification",),
            funding_priorities=("zero knowledge verification",),
            target_outcomes=("scalable secure settlement",),
            investigation_questions=("How can verification throughput scale securely?",),
            inferred_challenges=("Verification throughput degrades under high transaction volume.",),
            evidence_refs=("https://example.org/funding-call",),
            inference_provenance=("source_grounded_problem_extraction",),
            confidence="High",
            research_intent="research",
            research_intent_evidence=("The official call supports technical research and validation.",),
            selected_domains=("RWA",),
            domain_relevance=(),
        ),
    )


def _artifact(run_id: str, call_id: str, agent_name: str = "literature") -> str:
    title = f"Zero-knowledge verification evidence from {agent_name}"
    source_url = f"https://example.org/research/{agent_name}"
    return f"""# {title}

## Shared Metadata
- title: {title}
- source: {source_url}
- layer: Literature
- research_area: RWA
- collected_at: 2026-11-01T10:00:00+00:00
- agent_name: {agent_name}
- run_id: {run_id}
- funding_call_id: {call_id}
- funding_call_ids: {call_id}
- collection_mode: configured_scan

## Provenance
- source_url: {source_url}
- collected_at: 2026-11-01T10:00:00+00:00

## Evidence Bundle
### Page Title
{title}
### Key Paragraphs
- Zero knowledge verification latency increases under high transaction volume and limits settlement throughput.
### Evidence Snippets
- Zero knowledge verification throughput degrades under high transaction volume.
### Excerpt Windows
- The benchmark reports verification latency and throughput constraints.

## Problem Intelligence
**Selected Problem**: Zero knowledge verification throughput degrades under high transaction volume, limiting secure settlement.
**Original Candidate**: Zero knowledge verification throughput degrades under high transaction volume.
**Mode**: deterministic
**Confidence**: High

### Problem Candidates
- none

## Agent-Specific Body
### Source Type
- Research paper

## Machine Metadata
- source_hints: {{"published_at": "2026-10-01"}}

## Raw Content
Zero knowledge verification latency increases under high transaction volume and limits settlement throughput. The source reports measurable verification bottlenecks and scalability constraints.
"""


def test_batch2_runs_eleven_collectors_normalizes_and_stops_before_batch3(tmp_path):
    context = _context()
    captured = {}

    def fake_execute_batch_run(**kwargs):
        captured.update(kwargs)
        run_root = Path(kwargs["base_input_data"]["pipeline_run_dir"])
        for agent in sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS):
            artifact_dir = run_root / "intermediate" / agent
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / f"{agent}-signal.md").write_text(
                _artifact(kwargs["base_input_data"]["run_id"], context.funding_call_id, agent),
                encoding="utf-8",
            )
        return {
            "status": "completed",
            "tasks": [
                {
                    "agent_name": agent,
                    "status": "success",
                    "outputs_count": 1,
                    "items_processed": 1,
                    "items_saved": 1,
                }
                for agent in sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS)
            ],
        }

    with patch("core.batch2.execute_batch_run", side_effect=fake_execute_batch_run):
        report = execute_batch2(
            funding_contexts=[context],
            output_root=tmp_path / "batch2",
            run_id="batch2-test-run",
            batch1_run_id="batch1-test-run",
            execution_mode="sequential",
            max_workers=1,
            timeout_budget_seconds=60,
        )

    assert report["status"] == "ready_for_batch3"
    assert report["batch1_run_id"] == "batch1-test-run"
    assert report["batch3_started"] is False
    assert report["collector_health"]["total_agents"] == 11
    assert report["collector_health"]["healthy_agents"] == 11
    assert set(captured["agent_names"]) == DOWNSTREAM_FUNDING_CONTEXT_AGENTS
    assert captured["area_names"] == [None]
    assert captured["analysis_mode"] == "collect_only"
    assert captured["base_input_data"]["allow_llm_refinement"] is False
    assert captured["base_input_data"]["allow_llm_analysis"] is False
    assert captured["base_input_data"]["run_id"] == "batch2-test-run"

    call_report = report["funding_calls"][0]
    assert call_report["status"] == "ready_for_batch3"
    assert call_report["normalized_records"] == 11

    call_root = tmp_path / "batch2" / "batch2-test-run" / "calls" / context.funding_call_id
    records = json.loads((call_root / "normalized" / "records.json").read_text(encoding="utf-8"))
    assert len(records) == 11
    assert records[0]["funding_call_id"] == context.funding_call_id
    assert records[0]["run_id"] == "batch2-test-run"
    assert records[0]["normalization_status"] == "NORMALIZED"

    # The persisted selected-call shape must remain compatible with Batch-3 refresh.
    roots, target_call_id, gate = resolve_refresh_context(call_root)
    assert target_call_id == context.funding_call_id
    assert roots[0].name == "intermediate"
    assert gate(records[0]) is True

    # Batch-3's real tagging entry point must be able to reload and consume the
    # normalized per-call records. Keep semantic scores deterministic so this
    # integration test does not depend on Ollama or a transformer download.
    from agents import tagging_agent

    tag_output = call_root / "processed" / "tags"
    with patch("agents.tagging_agent._semantic_tag_scores", return_value={}), patch(
        "agents.tagging_agent._semantic_signal_scores", return_value={}
    ):
        tagging_response = tagging_agent.run_agent(
            "configured_scan",
            area="RWA",
            input_data={
                "outputs_root": str(call_root),
                "output_dir": str(tag_output),
            },
        )
    assert tagging_response["status"] == "success"
    assert tagging_response["metadata"]["tagging_summary"]["records_tagged"] == 11
    assert Path(tagging_response["metadata"]["tagging_summary"]["summary_markdown"]).is_file()

    report_path = Path(report["report_path"])
    assert report_path.is_file()
    saved_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved_report["batch3_started"] is False
    assert saved_report["status"] == "ready_for_batch3"
    handoff_copy = json.loads(
        (call_root.parent.parent / "workflow" / "funding_contexts.json").read_text(encoding="utf-8")
    )
    assert handoff_copy["batch1_run_id"] == "batch1-test-run"
    assert handoff_copy["funding_contexts"][0]["funding_call_id"] == context.funding_call_id


def test_batch2_rejects_duplicate_call_ids(tmp_path):
    context = _context()
    with pytest.raises(ValueError, match="must be unique"):
        execute_batch2(
            funding_contexts=[context, context],
            output_root=tmp_path,
            run_id="duplicate-test",
        )


def test_batch2_does_not_reuse_nonempty_run_directory(tmp_path):
    context = _context()
    run_root = tmp_path / "existing-run"
    run_root.mkdir()
    (run_root / "stale.md").write_text("old data", encoding="utf-8")

    with pytest.raises(FileExistsError, match="Use a new --run-id"):
        execute_batch2(
            funding_contexts=[context],
            output_root=tmp_path,
            run_id="existing-run",
        )


def test_batch2_dry_run_plans_all_collectors_without_normalizing(tmp_path):
    context = _context()
    captured = {}

    def fake_execute_batch_run(**kwargs):
        captured.update(kwargs)
        return {
            "status": "dry_run",
            "tasks": [
                {"agent_name": agent, "status": "planned", "outputs_count": 0}
                for agent in sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS)
            ],
        }

    with patch("core.batch2.execute_batch_run", side_effect=fake_execute_batch_run):
        report = execute_batch2(
            funding_contexts=[context],
            output_root=tmp_path / "dry-run",
            run_id="batch2-dry-run",
            dry_run=True,
        )

    assert report["status"] == "dry_run"
    assert report["normalization_started"] is False
    assert report["batch3_started"] is False
    assert captured["dry_run"] is True
    assert set(captured["agent_names"]) == DOWNSTREAM_FUNDING_CONTEXT_AGENTS
    call_root = tmp_path / "dry-run" / "batch2-dry-run" / "calls" / context.funding_call_id
    assert not (call_root / "normalized").exists()


def test_batch1_funding_context_handoff_file_loads_without_reconstructing_batch1(tmp_path):
    from scripts.pipeline_tools.run_batch2 import load_funding_context_handoff, load_funding_contexts

    context = _context()
    handoff = tmp_path / "funding_contexts.json"
    handoff.write_text(
        json.dumps(
            {
                "run_id": "batch1-fixture-run",
                "funding_contexts": [context.to_dict()],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    loaded, batch1_run_id = load_funding_context_handoff(handoff)
    assert batch1_run_id == "batch1-fixture-run"
    assert load_funding_contexts(handoff)[0].funding_call_id == context.funding_call_id
    assert len(loaded) == 1
    assert loaded[0].funding_call_id == context.funding_call_id
    assert loaded[0].research_priorities == context.research_priorities


def test_batch2_rejects_context_that_does_not_pass_batch1_handoff_gates(tmp_path):
    context = _context()
    invalid = FundingCallContext.from_dict({**context.to_dict(), "status": "CLOSED_CALL"})

    with pytest.raises(ValueError, match="not confirmed currently open"):
        execute_batch2(
            funding_contexts=[invalid],
            output_root=tmp_path,
            run_id="invalid-context-run",
        )


def test_batch2_accepts_either_official_source_or_application_url_from_batch1():
    from core.batch2 import validate_batch1_context

    context = _context()
    source_only = FundingCallContext.from_dict({**context.to_dict(), "application_url": None})
    application_only = FundingCallContext.from_dict({**context.to_dict(), "source_url": None})

    assert validate_batch1_context(source_only) == []
    assert validate_batch1_context(application_only) == []
