from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.agent_registry import DOWNSTREAM_FUNDING_CONTEXT_AGENTS
from core.batch2 import audit_collector_artifacts, execute_batch2, evaluate_collection_health, is_safe_path_component, validate_batch1_context, validate_evidence_ledger, validate_normalized_handoff
from core.funding_selection import FundingCallContext
from core.research_schemas import FundingResearchContext
from core.normalization import build_normalized_collection_records, resolve_refresh_context
from core.agent_interface import assign_funding_calls


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


def test_shared_programme_name_and_application_portal_do_not_cross_assign_calls():
    first = _context("CALL-SHARED-A")
    second = _context("CALL-SHARED-B")
    first_payload = first.to_dict()
    second_payload = second.to_dict()
    for payload in (first_payload, second_payload):
        payload["program_name"] = "European Research Programme"
        payload["application_url"] = "https://example.org/shared-application-portal"
    first_payload["call_title"] = "Privacy-preserving settlement verification research"
    second_payload["call_title"] = "Quantum sensing for resilient coastal infrastructure"
    second_payload["research_priorities"] = ["quantum sensing", "coastal resilience"]
    second_rc = second_payload["research_context"]
    second_rc["technology_themes"] = ["quantum sensing", "coastal resilience"]
    second_rc["funding_priorities"] = ["quantum sensing", "coastal resilience"]
    second_rc["target_outcomes"] = ["resilient coastal monitoring"]
    second_rc["investigation_questions"] = ["How can quantum sensors improve coastal monitoring?"]
    first = FundingCallContext.from_dict(first_payload)
    second = FundingCallContext.from_dict(second_payload)

    shared_programme_text = "European Research Programme announces a general information session."

    assert assign_funding_calls("Programme overview", shared_programme_text, [first, second]) == []
    assert assign_funding_calls(first.call_title, first.call_title, [first, second]) == [first.funding_call_id]


def test_parent_programme_name_and_generic_portal_are_not_call_identity():
    context = _context("CALL-PARENT-PROGRAMME")
    payload = context.to_dict()
    payload["program_name"] = "European Innovation Council"
    payload["application_url"] = "https://apply.example.org/submit"
    context = FundingCallContext.from_dict(payload)

    assert assign_funding_calls(
        "General programme overview",
        "European Innovation Council publishes a general programme overview.",
        [context],
    ) == []
    assert assign_funding_calls(
        "Application portal",
        "Submit applications at https://apply.example.org/submit",
        [context],
    ) == []


def test_normalized_shared_evidence_is_scoped_to_one_call_at_a_time(tmp_path):
    root = tmp_path / "intermediate"
    artifact_dir = root / "literature"
    artifact_dir.mkdir(parents=True)
    shared_artifact = _artifact("batch2-shared-run", "CALL-SHARED-A")
    shared_artifact = shared_artifact.replace("- funding_call_id: CALL-SHARED-A\n", "")
    shared_artifact = shared_artifact.replace(
        "- funding_call_ids: CALL-SHARED-A\n",
        "- funding_call_ids: CALL-SHARED-A, CALL-SHARED-B\n",
    )
    (artifact_dir / "shared-evidence.md").write_text(shared_artifact, encoding="utf-8")

    first_records = build_normalized_collection_records(
        root,
        target_funding_call_id="CALL-SHARED-A",
        target_run_id="batch2-shared-run",
    )
    second_records = build_normalized_collection_records(
        root,
        target_funding_call_id="CALL-SHARED-B",
        target_run_id="batch2-shared-run",
    )

    assert len(first_records) == len(second_records) == 1
    assert first_records[0]["funding_call_id"] == "CALL-SHARED-A"
    assert first_records[0]["funding_call_ids"] == ["CALL-SHARED-A"]
    assert first_records[0]["source_funding_call_ids"] == ["CALL-SHARED-A", "CALL-SHARED-B"]
    assert second_records[0]["funding_call_id"] == "CALL-SHARED-B"
    assert second_records[0]["funding_call_ids"] == ["CALL-SHARED-B"]
    assert second_records[0]["source_funding_call_ids"] == ["CALL-SHARED-A", "CALL-SHARED-B"]
    assert first_records[0]["evidence_id"] != second_records[0]["evidence_id"]


def test_evidence_ids_use_problem_text_when_source_has_no_excerpt_snippets(tmp_path):
    root = tmp_path / "intermediate"
    agent_dir = root / "literature"
    agent_dir.mkdir(parents=True)
    base = _artifact("fallback-id-run", "CALL-001")
    first = base.replace(
        "**Selected Problem**: Zero knowledge verification throughput degrades under high transaction volume, limiting secure settlement.",
        "**Selected Problem**: Verification latency increases sharply under high transaction volume.",
    )
    second = base.replace(
        "**Selected Problem**: Zero knowledge verification throughput degrades under high transaction volume, limiting secure settlement.",
        "**Selected Problem**: Evidence provenance becomes incomplete across multi-source settlement workflows.",
    )
    for name, artifact in (("first", first), ("second", second)):
        artifact = artifact.replace(
            "- Zero knowledge verification throughput degrades under high transaction volume.\n",
            "",
        )
        artifact = artifact.replace(
            "- The benchmark reports verification latency and throughput constraints.\n",
            "",
        )
        (agent_dir / f"{name}.md").write_text(artifact, encoding="utf-8")

    records = build_normalized_collection_records(
        root,
        target_funding_call_id="CALL-001",
        target_run_id="fallback-id-run",
    )

    assert len(records) == 2
    assert records[0]["evidence_snippets"] == []
    assert records[1]["evidence_snippets"] == []
    assert records[0]["evidence_id"] != records[1]["evidence_id"]


def test_full_pipeline_can_validate_supplemental_adaptive_research_records():
    context = _context()
    records = build_normalized_collection_records(
        _single_artifact_root(context.funding_call_id, "adaptive_research"),
        target_funding_call_id=context.funding_call_id,
        target_run_id="batch2-validation-run",
    )
    records[0]["mission_relevance"] = {"passed": True}

    assert validate_normalized_handoff(
        records,
        funding_call_id=context.funding_call_id,
        run_id="batch2-validation-run",
        expected_agents=set(DOWNSTREAM_FUNDING_CONTEXT_AGENTS) | {"adaptive_research"},
    ) == []
    assert any(
        "unknown collection agent" in error
        for error in validate_normalized_handoff(
            records,
            funding_call_id=context.funding_call_id,
            run_id="batch2-validation-run",
            expected_agents=set(DOWNSTREAM_FUNDING_CONTEXT_AGENTS),
        )
    )


def test_normalized_handoff_rejects_cross_call_ids_and_timezone_naive_dates():
    context = _context()
    record = build_normalized_collection_records(
        _single_artifact_root(context.funding_call_id),
        target_funding_call_id=context.funding_call_id,
        target_run_id="batch2-validation-run",
    )[0]
    record["mission_relevance"] = {"passed": True}
    expected = set(DOWNSTREAM_FUNDING_CONTEXT_AGENTS)

    assert validate_normalized_handoff(
        [record],
        funding_call_id=context.funding_call_id,
        run_id="batch2-validation-run",
        expected_agents=expected,
    ) == []

    record["funding_call_ids"] = [context.funding_call_id, "OTHER-CALL"]
    errors = validate_normalized_handoff(
        [record],
        funding_call_id=context.funding_call_id,
        run_id="batch2-validation-run",
        expected_agents=expected,
    )
    assert any("exactly one funding_call_id" in error for error in errors)

    record["funding_call_ids"] = [context.funding_call_id]
    record["collected_at"] = "2026-11-01T10:00:00"
    errors = validate_normalized_handoff(
        [record],
        funding_call_id=context.funding_call_id,
        run_id="batch2-validation-run",
        expected_agents=expected,
    )
    assert any("timezone-naive" in error for error in errors)


def _single_artifact_root(call_id: str, agent_name: str = "literature") -> Path:
    import tempfile

    root = Path(tempfile.mkdtemp()) / "intermediate"
    artifact_dir = root / agent_name
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "signal.md").write_text(
        _artifact("batch2-validation-run", call_id, agent_name),
        encoding="utf-8",
    )
    return root


def test_collector_health_uses_persisted_artifacts_and_quarantines_bad_provenance(tmp_path):
    run_root = tmp_path / "batch2-run"
    literature_dir = run_root / "intermediate" / "literature"
    lab_dir = run_root / "intermediate" / "lab"
    literature_dir.mkdir(parents=True)
    lab_dir.mkdir(parents=True)
    valid_path = literature_dir / "valid.md"
    invalid_path = lab_dir / "stale.md"
    valid_path.write_text(_artifact("current-run", "CALL-001", "literature"), encoding="utf-8")
    invalid_path.write_text(_artifact("previous-run", "CALL-001", "lab"), encoding="utf-8")

    tasks = [
        {"agent_name": "literature", "status": "error", "outputs_count": 0, "errors": ["late wrapper error"], "warnings": []},
        {"agent_name": "lab", "status": "success", "outputs_count": 1, "errors": [], "warnings": []},
    ]
    updated, audit = audit_collector_artifacts(
        run_root,
        tasks,
        expected_agents={"literature", "lab"},
        run_id="current-run",
    )

    by_agent = {item["agent_name"]: item for item in updated}
    assert by_agent["literature"]["status"] == "partial_success"
    assert by_agent["literature"]["outputs_count"] == 1
    assert by_agent["literature"]["persisted_artifact_count"] == 1
    assert by_agent["lab"]["status"] == "error"
    assert by_agent["lab"]["outputs_count"] == 0
    assert not invalid_path.exists()
    assert len(audit["quarantined_artifacts"]) == 1
    quarantined_path = Path(audit["quarantined_artifacts"][0]["quarantined_path"])
    assert quarantined_path.is_file()
    assert "artifact_run_id_mismatch" in audit["quarantined_artifacts"][0]["reasons"]


def test_collector_artifact_audit_quarantines_conflicting_source_provenance(tmp_path):
    run_root = tmp_path / "batch2-run"
    agent_dir = run_root / "intermediate" / "literature"
    agent_dir.mkdir(parents=True)
    artifact_path = agent_dir / "conflicting-source.md"
    artifact = _artifact("current-run", "CALL-001", "literature")
    artifact = artifact.replace(
        "- source_url: https://example.org/research/literature",
        "- source_url: https://different.example.org/incorrect-source",
    )
    artifact_path.write_text(artifact, encoding="utf-8")

    tasks = [{
        "agent_name": "literature",
        "status": "success",
        "outputs_count": 1,
        "output_paths": [str(artifact_path.resolve())],
        "errors": [],
        "warnings": [],
    }]
    updated, audit = audit_collector_artifacts(
        run_root,
        tasks,
        expected_agents={"literature"},
        run_id="current-run",
    )

    assert updated[0]["status"] == "error"
    assert updated[0]["outputs_count"] == 0
    assert not artifact_path.exists()
    assert len(audit["quarantined_artifacts"]) == 1
    assert "artifact_header_and_provenance_source_url_conflict" in (
        audit["quarantined_artifacts"][0]["reasons"]
    )


def test_batch2_prepares_source_registry_once_before_collectors(tmp_path):
    events = []
    captured = {}

    def fake_sync():
        events.append("sync")
        return SimpleNamespace(total_entries=12, active_entries=11, deprecated_entries=1)

    def fake_plan(*, force=False):
        events.append("plan")
        assert force is True
        return SimpleNamespace(total_entries=11, due_entries=11, skipped_entries=0)

    def fake_execute(**kwargs):
        events.append("execute")
        captured.update(kwargs)
        return {
            "status": "completed",
            "tasks": [
                {"agent_name": agent, "status": "error", "outputs_count": 0, "errors": ["fixture failure"]}
                for agent in sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS)
            ],
        }

    with patch("core.batch2.sync_source_registry", side_effect=fake_sync), \
         patch("core.batch2.build_source_refresh_plan", side_effect=fake_plan), \
         patch("core.batch2.execute_batch_run", side_effect=fake_execute):
        report = execute_batch2(
            funding_contexts=[_context()],
            output_root=tmp_path,
            run_id="registry-sync-test",
            execution_mode="parallel",
        )

    assert events == ["sync", "plan", "execute"]
    assert captured["base_input_data"]["defer_registry_sync"] is True
    assert report["source_registry"]["status"] == "ready"


def test_batch2_stops_if_source_registry_preparation_fails(tmp_path):
    with patch("core.batch2.sync_source_registry", side_effect=OSError("registry unavailable")), \
         patch("core.batch2.execute_batch_run") as execute:
        report = execute_batch2(
            funding_contexts=[_context()],
            output_root=tmp_path,
            run_id="registry-failure-test",
        )

    assert report["status"] == "blocked_source_registry"
    assert report["batch3_started"] is False
    assert report["source_registry"]["status"] == "error"
    execute.assert_not_called()


def _artifact(run_id: str, call_id: str, agent_name: str = "literature") -> str:
    import hashlib
    import json

    title = f"Zero-knowledge verification evidence from {agent_name}"
    source_url = f"https://example.org/research/{agent_name}"
    raw_content = "Zero knowledge verification latency increases under high transaction volume and limits settlement throughput. The source reports measurable verification bottlenecks and scalability constraints."
    document_identity = hashlib.sha256(f"{source_url}|{title}".encode("utf-8")).hexdigest()
    provenance_content_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
    machine_payload = {
        "shared_header": {
            "title": title,
            "source": source_url,
            "layer": "Literature",
            "research_area": "RWA",
            "collected_at": "2026-11-01T10:00:00+00:00",
            "agent_name": agent_name,
            "run_id": run_id,
            "funding_call_id": call_id,
            "funding_call_ids": call_id,
            "collection_mode": "configured_scan",
        },
        "provenance": {
            "source_url": source_url,
            "collected_at": "2026-11-01T10:00:00+00:00",
            "agent_name": agent_name,
            "document_identity": document_identity,
            "content_hash": provenance_content_hash,
        },
        "raw_content": raw_content,
    }
    machine_payload_json = json.dumps(machine_payload, ensure_ascii=False)
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
- agent_name: {agent_name}
- document_identity: {document_identity}
- content_hash: {provenance_content_hash}

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
{raw_content}

## Machine-Readable Payload
```json
{machine_payload_json}
```
"""


def test_batch1_handoff_batch2_collection_and_batch3_tagging_end_to_end(tmp_path):
    context = _context()
    batch1_handoff = tmp_path / "batch1" / "workflow" / "funding_contexts.json"
    batch1_handoff.parent.mkdir(parents=True, exist_ok=True)
    batch1_handoff.write_text(
        json.dumps(
            {
                "run_id": "batch1-test-run",
                "funding_contexts": [context.to_dict()],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    from scripts.pipeline_tools.run_batch2 import load_funding_context_handoff

    loaded_contexts, batch1_run_id = load_funding_context_handoff(batch1_handoff)
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

    with patch(
        "core.batch2.sync_source_registry",
        return_value=SimpleNamespace(total_entries=11, active_entries=11, deprecated_entries=0),
    ), patch(
        "core.batch2.build_source_refresh_plan",
        return_value=SimpleNamespace(total_entries=11, due_entries=11, skipped_entries=0),
    ), patch("core.batch2.execute_batch_run", side_effect=fake_execute_batch_run):
        report = execute_batch2(
            funding_contexts=loaded_contexts,
            output_root=tmp_path / "batch2",
            run_id="batch2-test-run",
            batch1_run_id=batch1_run_id,
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
    ledger_path = call_root / "normalized" / "evidence_ledger.db"
    assert validate_evidence_ledger(
        ledger_path,
        records,
        funding_call_id=context.funding_call_id,
        run_id="batch2-test-run",
    ) == []
    with sqlite3.connect(ledger_path) as ledger_connection:
        ledger_connection.execute(
            "DELETE FROM canonical_evidence WHERE evidence_id = ?",
            (records[0]["evidence_id"],),
        )
        ledger_connection.commit()
    corrupted_ledger_errors = validate_evidence_ledger(
        ledger_path,
        records,
        funding_call_id=context.funding_call_id,
        run_id="batch2-test-run",
    )
    assert any("row count" in error for error in corrupted_ledger_errors)
    assert any("evidence_id set" in error for error in corrupted_ledger_errors)

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
                "batch1_run_id": "batch1-fixture-run",
                "batch1_acceptance": {
                    "accepted": True,
                    "status": "ACCEPTED",
                    "batch1_run_id": "batch1-fixture-run",
                    "source_coverage_verified": True,
                    "acceptance": {"status": "ACCEPTED", "accepted": True, "blockers": []},
                },
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


def test_standalone_batch2_rejects_unaccepted_or_mismatched_batch1_handoff(tmp_path):
    from scripts.pipeline_tools.run_batch2 import load_funding_context_handoff

    context = _context()
    handoff = tmp_path / "funding_contexts.json"
    base = {
        "batch1_run_id": "batch1-run",
        "funding_contexts": [context.to_dict()],
    }

    handoff.write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(ValueError, match="ACCEPTED global source-coverage attestation"):
        load_funding_context_handoff(handoff)

    blocked = {
        **base,
        "batch1_acceptance": {
            "accepted": False,
            "status": "BLOCKED",
            "batch1_run_id": "batch1-run",
            "source_coverage_verified": False,
        },
    }
    handoff.write_text(json.dumps(blocked), encoding="utf-8")
    with pytest.raises(ValueError, match="ACCEPTED global source-coverage attestation"):
        load_funding_context_handoff(handoff)

    mismatched = {
        **base,
        "batch1_acceptance": {
            "accepted": True,
            "status": "ACCEPTED",
            "batch1_run_id": "different-run",
            "source_coverage_verified": True,
        },
    }
    handoff.write_text(json.dumps(mismatched), encoding="utf-8")
    with pytest.raises(ValueError, match="ACCEPTED global source-coverage attestation"):
        load_funding_context_handoff(handoff)


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



def test_batch2_refuses_to_reuse_even_an_empty_run_directory(tmp_path):
    context = _context()
    run_root = tmp_path / "empty-existing-run"
    run_root.mkdir()

    with pytest.raises(FileExistsError, match="already exists"):
        execute_batch2(
            funding_contexts=[context],
            output_root=tmp_path,
            run_id="empty-existing-run",
        )


def test_collection_health_ignores_unexpected_agent_names():
    tasks = [
        {"agent_name": f"unexpected-{index}", "status": "success", "outputs_count": 1}
        for index in range(6)
    ]
    healthy, count = evaluate_collection_health(
        tasks,
        min_required_agents=6,
        expected_agents=set(DOWNSTREAM_FUNDING_CONTEXT_AGENTS),
    )
    assert healthy is False
    assert count == 0


def test_normalized_handoff_rejects_invalid_records_and_provenance():
    errors = validate_normalized_handoff(
        [
            "not-an-object",
            {
                "record_id": "r1",
                "evidence_id": "e1",
                "source_id": "s1",
                "content_hash": "h1",
                "title": "Evidence",
                "agent_name": "unknown-agent",
                "run_id": "wrong-run",
                "funding_call_id": "wrong-call",
                "source_url": "not-a-url",
                "normalization_status": "NEEDS_REVIEW",
                "collected_at": "not-a-timestamp",
                "problem_statement": "",
                "context_summary": "",
                "evidence_snippets": "",
                "excerpt_windows": [],
                "mission_relevance": {"passed": False},
            },
        ],
        funding_call_id="CALL-BATCH2-001",
        run_id="batch2-run",
        expected_agents=set(DOWNSTREAM_FUNDING_CONTEXT_AGENTS),
    )
    assert any("not a JSON object" in error for error in errors)
    assert any("mismatched run_id" in error for error in errors)
    assert any("mismatched funding_call_id" in error for error in errors)
    assert any("usable HTTP(S)" in error for error in errors)
    assert any("unknown collection agent" in error for error in errors)
    assert any("invalid or timezone-naive collected_at" in error for error in errors)
    assert any("no usable evidence text" in error for error in errors)
    assert any("field 'evidence_snippets' must be a list" in error for error in errors)
    assert any("mission-relevance" in error for error in errors)


def test_normalized_handoff_accepts_valid_evidence_record():
    agent = sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS)[0]
    record = build_normalized_collection_records(
        _single_artifact_root("CALL-BATCH2-001", agent),
        target_funding_call_id="CALL-BATCH2-001",
        target_run_id="batch2-validation-run",
    )[0]
    record["mission_relevance"] = {"passed": True}
    record["run_id"] = "batch2-run"
    assert validate_normalized_handoff(
        [record],
        funding_call_id="CALL-BATCH2-001",
        run_id="batch2-run",
        expected_agents=set(DOWNSTREAM_FUNDING_CONTEXT_AGENTS),
    ) == []



def test_batch2_blocks_unexpected_collector_tasks(tmp_path):
    context = _context()
    tasks = [
        {"agent_name": agent, "status": "success", "outputs_count": 1}
        for agent in sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS)
    ]
    tasks.append({"agent_name": "unexpected_agent", "status": "success", "outputs_count": 1})

    from batch2_test_helpers import write_test_collector_artifacts
    run_id = "unexpected-agent-run"

    def fake_execute_batch_run(**kwargs):
        write_test_collector_artifacts(
            Path(kwargs["base_input_data"]["pipeline_run_dir"]),
            run_id,
            DOWNSTREAM_FUNDING_CONTEXT_AGENTS,
        )
        return {"status": "completed", "tasks": tasks}

    with patch("core.batch2.execute_batch_run", side_effect=fake_execute_batch_run):
        report = execute_batch2(
            funding_contexts=[context],
            output_root=tmp_path,
            run_id=run_id,
        )

    assert report["status"] == "blocked_collection_health"
    assert report["collector_health"]["healthy_agents"] == 11
    assert report["collector_health"]["unexpected_agents"] == ["unexpected_agent"]
    assert any(
        blocker["code"] == "UNEXPECTED_COLLECTOR_TASKS"
        for blocker in report["blockers"]
    )



def test_path_component_validation_rejects_traversal_and_accepts_generated_ids():
    assert is_safe_path_component("20261101T183000Z")
    assert is_safe_path_component("CALL-AB12_cd.3")
    assert not is_safe_path_component("../outside")
    assert not is_safe_path_component("folder/name")
    assert not is_safe_path_component(r"..\\outside")
    assert not is_safe_path_component("C:\\temp\\run")


def test_batch2_rejects_path_traversal_run_id(tmp_path):
    context = _context()
    with pytest.raises(ValueError, match="run_id must be a safe path component"):
        execute_batch2(
            funding_contexts=[context],
            output_root=tmp_path,
            run_id="../outside",
        )
    assert not (tmp_path.parent / "outside").exists()


def test_batch1_context_rejects_unsafe_funding_call_id():
    context = _context()
    context.funding_call_id = "../outside"
    context.research_context.funding_call_id = "../outside"
    issues = validate_batch1_context(context)
    assert "funding_call_id is not a safe path component" in issues
