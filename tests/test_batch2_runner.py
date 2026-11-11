from __future__ import annotations

import json
from contextlib import closing
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.agent_registry import DOWNSTREAM_FUNDING_CONTEXT_AGENTS
from core.batch2 import audit_collector_artifacts, execute_batch2, evaluate_collection_health, is_safe_path_component, minimum_healthy_collectors, validate_batch1_context, validate_evidence_ledger, validate_normalized_handoff
from core.funding_selection import FundingCallContext
from core.research_schemas import FundingResearchContext
from core.normalization import build_normalized_collection_records, resolve_refresh_context
from core.agent_interface import assign_funding_calls


def _context(call_id: str = "CALL-BATCH2-001") -> FundingCallContext:
    from tests.batch2_test_helpers import build_valid_funding_context

    return build_valid_funding_context(call_id)


def _batch1_acceptance(run_id: str = "batch1-test-run") -> dict:
    return {
        "accepted": True,
        "status": "ACCEPTED",
        "batch1_run_id": run_id,
        "source_coverage_verified": True,
        "acceptance": {"status": "ACCEPTED", "accepted": True, "blockers": [], "blocker_count": 0},
    }


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


def test_collector_artifact_audit_rejects_external_workflow_symlink_before_quarantine_creation(tmp_path):
    run_root = tmp_path / "batch2-run"
    run_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (run_root / "workflow").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are not available on this platform")

    # Root links are rejected before checking whether their target is external or internal.
    with pytest.raises(ValueError, match="workflow root must not be a symlink or redirected path"):
        audit_collector_artifacts(
            run_root,
            [],
            expected_agents={"literature"},
            run_id="current-run",
        )

    assert not (outside / "quarantined_intermediate").exists()


def test_collector_artifact_audit_rejects_malformed_task_entries(tmp_path):
    _, audit = audit_collector_artifacts(
        tmp_path / "batch2-run",
        [None, {"status": "success", "outputs_count": 1}],
        expected_agents={"literature"},
        run_id="current-run",
    )

    assert len(audit["malformed_task_entries"]) == 2
    assert {item["reason"] for item in audit["malformed_task_entries"]} == {
        "collector_task_entry_is_not_an_object",
        "collector_task_agent_name_missing",
    }


def test_collector_artifact_audit_marks_escaping_output_manifest_as_critical(tmp_path):
    run_root = tmp_path / "batch2-run"
    agent_dir = run_root / "intermediate" / "literature"
    agent_dir.mkdir(parents=True)
    artifact_path = agent_dir / "valid.md"
    artifact_path.write_text(_artifact("current-run", "CALL-001"), encoding="utf-8")

    tasks = [{
        "agent_name": "literature",
        "status": "success",
        "outputs_count": 1,
        "output_paths": [str((tmp_path / "outside.md").resolve())],
        "errors": [],
        "warnings": [],
    }]
    _, audit = audit_collector_artifacts(
        run_root,
        tasks,
        expected_agents={"literature"},
        run_id="current-run",
    )

    assert audit["manifest_issues"]
    assert any(
        "reported_output_path_escapes_agent_output_directory" in item["reasons"]
        for item in audit["manifest_issues"]
    )


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


def test_collector_artifact_audit_quarantines_markdown_call_attribution_tampering(tmp_path):
    run_root = tmp_path / "batch2-run"
    agent_dir = run_root / "intermediate" / "literature"
    agent_dir.mkdir(parents=True)
    artifact_path = agent_dir / "wrong-call.md"
    artifact_path.write_text(
        _artifact("current-run", "CALL-001").replace(
            "- funding_call_id: CALL-001",
            "- funding_call_id: CALL-OTHER",
            1,
        ),
        encoding="utf-8",
    )
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
    assert len(audit["quarantined_artifacts"]) == 1
    assert "artifact_markdown_and_machine_payload_funding_call_id_mismatch" in (
        audit["quarantined_artifacts"][0]["reasons"]
    )


def test_collector_artifact_audit_quarantines_markdown_evidence_bundle_tampering(tmp_path):
    run_root = tmp_path / "batch2-run"
    agent_dir = run_root / "intermediate" / "literature"
    agent_dir.mkdir(parents=True)
    artifact_path = agent_dir / "tampered-excerpt.md"
    artifact_path.write_text(
        _artifact("current-run", "CALL-001").replace(
            "- Zero knowledge verification throughput degrades under high transaction volume.",
            "- Fabricated excerpt that is not present in the machine payload.",
            1,
        ),
        encoding="utf-8",
    )
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
    assert len(audit["quarantined_artifacts"]) == 1
    assert "artifact_markdown_and_machine_payload_evidence_evidence_snippets_mismatch" in (
        audit["quarantined_artifacts"][0]["reasons"]
    )


def test_collector_artifact_audit_quarantines_markdown_raw_content_tampering(tmp_path):
    run_root = tmp_path / "batch2-run"
    agent_dir = run_root / "intermediate" / "literature"
    agent_dir.mkdir(parents=True)
    artifact_path = agent_dir / "tampered.md"
    original = (
        "Zero knowledge verification latency increases under high transaction volume "
        "and limits settlement throughput. The source reports measurable verification "
        "bottlenecks and scalability constraints."
    )
    artifact_path.write_text(
        _artifact("current-run", "CALL-001").replace(
            "## Raw Content\n" + original,
            "## Raw Content\nTampered Markdown evidence that is not covered by the machine payload hash.",
            1,
        ),
        encoding="utf-8",
    )

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
    assert not artifact_path.exists()
    assert len(audit["quarantined_artifacts"]) == 1
    assert "artifact_markdown_and_machine_payload_raw_content_mismatch" in (
        audit["quarantined_artifacts"][0]["reasons"]
    )


def test_batch2_prepares_source_registry_only_for_downstream_agents_before_collectors(tmp_path):
    events = []
    captured = {}

    def fake_sync(*, agent_filter):
        events.append(("sync", agent_filter))
        return SimpleNamespace(total_entries=20, active_entries=18, deprecated_entries=2)

    def fake_plan(*, force=False, agent_filter=None, output_path=None):
        events.append(("plan", agent_filter))
        assert force is True
        assert agent_filter in DOWNSTREAM_FUNDING_CONTEXT_AGENTS
        assert output_path is not None
        Path(output_path).write_text(
            json.dumps({
                "generated_at": "2026-11-03T00:00:00+00:00",
                "filters": {"agent_filter": agent_filter, "due_only": False, "force": True},
                "stats": {"total_entries": 1, "due_entries": 1, "skipped_entries": 0},
                "entries": [{"agent_name": agent_filter, "source_id": f"url:https://{agent_filter}.example.org"}],
            }),
            encoding="utf-8",
        )
        return SimpleNamespace(
            total_entries=1,
            due_entries=1,
            skipped_entries=0,
            refresh_plan_path=str(output_path),
        )

    def fake_execute(**kwargs):
        events.append(("execute", None))
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
            batch1_acceptance=_batch1_acceptance(),
            funding_contexts=[_context()],
            output_root=tmp_path,
            run_id="registry-sync-test",
            execution_mode="parallel",
        )

    expected_events = []
    for agent in sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS):
        expected_events.extend([("sync", agent), ("plan", agent)])
    expected_events.append(("execute", None))
    assert events == expected_events
    assert captured["base_input_data"]["defer_registry_sync"] is True
    assert report["source_registry"]["status"] == "ready"
    assert set(report["source_registry"]["agents_synced"]) == DOWNSTREAM_FUNDING_CONTEXT_AGENTS
    assert report["source_registry"]["refresh_plan_entries"] == 11
    combined_plan = Path(report["source_registry"]["refresh_plan_path"])
    assert combined_plan.exists()
    plan_payload = json.loads(combined_plan.read_text(encoding="utf-8"))
    assert set(plan_payload["agents"]) == DOWNSTREAM_FUNDING_CONTEXT_AGENTS
    assert len(plan_payload["entries"]) == 11
    assert {entry["agent_name"] for entry in plan_payload["entries"]} == DOWNSTREAM_FUNDING_CONTEXT_AGENTS


def test_batch2_stops_if_source_registry_preparation_fails(tmp_path):
    with patch("core.batch2.sync_source_registry", side_effect=OSError("registry unavailable")), \
         patch("core.batch2.execute_batch_run") as execute:
        report = execute_batch2(
            batch1_acceptance=_batch1_acceptance(),
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
        "evidence_bundle": {
            "page_title": title,
            "section_headings": [],
            "key_paragraphs": [
                "Zero knowledge verification latency increases under high transaction volume and limits settlement throughput."
            ],
            "bullet_lists": [],
            "quoted_passages": [],
            "links_found": [],
            "named_entities_or_programs": [],
            "evidence_snippets": [
                "Zero knowledge verification throughput degrades under high transaction volume."
            ],
            "excerpt_windows": [
                "The benchmark reports verification latency and throughput constraints."
            ],
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
### Evidence Type
- Scholarly publication record

## Machine Metadata
- source_hints: {{"published_at": "2026-10-01"}}

## Machine-Readable Payload
```json
{machine_payload_json}
```

## Raw Content
{raw_content}
"""


def test_batch1_handoff_batch2_collection_and_normalization_handoff_end_to_end(tmp_path):
    context = _context()
    batch1_handoff = tmp_path / "batch1" / "workflow" / "funding_contexts.json"
    batch1_handoff.parent.mkdir(parents=True, exist_ok=True)
    batch1_handoff.write_text(
        json.dumps(
            {
                "batch1_run_id": "batch1-test-run",
                "batch1_acceptance": _batch1_acceptance("batch1-test-run"),
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
        "core.batch2.prepare_batch2_source_registry",
        return_value={
            "status": "ready",
            "agents_synced": sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS),
            "refresh_plan_entries": 11,
            "refresh_due_entries": 11,
            "refresh_skipped_entries": 0,
            "refresh_plan_path": str(tmp_path / "batch2" / "batch2-test-run" / "workflow" / "source_refresh_plan.json"),
        },
    ), patch("core.batch2.execute_batch_run", side_effect=fake_execute_batch_run):
        report = execute_batch2(
            batch1_acceptance=_batch1_acceptance(),
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
    with closing(sqlite3.connect(ledger_path)) as ledger_connection:
        provenance_json = ledger_connection.execute(
            "SELECT provenance FROM canonical_evidence WHERE evidence_id = ?",
            (records[0]["evidence_id"],),
        ).fetchone()[0]
        provenance = json.loads(provenance_json)
        provenance["source_url"] = "https://wrong.example.org/tampered"
        ledger_connection.execute(
            "UPDATE canonical_evidence SET provenance = ? WHERE evidence_id = ?",
            (json.dumps(provenance), records[0]["evidence_id"]),
        )
        ledger_connection.commit()
    provenance_errors = validate_evidence_ledger(
        ledger_path,
        records,
        funding_call_id=context.funding_call_id,
        run_id="batch2-test-run",
    )
    assert any("provenance source_url mismatch" in error for error in provenance_errors)

    with closing(sqlite3.connect(ledger_path)) as ledger_connection:
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

    # Batch-2's acceptance boundary is the validated normalized handoff.
    # Deliberately do not invoke any Batch-3 agent in this test.
    assert report["status"] == "ready_for_batch3"
    assert report["batch3_started"] is False
    assert call_report["status"] == "ready_for_batch3"
    assert len(records) == 11

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


def test_batch2_never_marks_empty_mission_relevance_as_ready(tmp_path):
    context = _context()

    def fake_execute_batch_run(**kwargs):
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
        "core.batch2.prepare_batch2_source_registry",
        return_value={
            "status": "ready",
            "agents_synced": sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS),
            "refresh_plan_entries": 11,
            "refresh_due_entries": 11,
            "refresh_skipped_entries": 0,
            "refresh_plan_path": str(tmp_path / "batch2" / "empty-mission-run" / "workflow" / "source_refresh_plan.json"),
        },
    ), patch("core.batch2.execute_batch_run", side_effect=fake_execute_batch_run), patch(
        "core.batch2.mission_relevance",
        return_value={
            "passed": False,
            "score": 0.0,
            "matched_terms": [],
            "matched_phrases": [],
            "reason": "insufficient substantive mission overlap",
        },
    ):
        report = execute_batch2(
            batch1_acceptance=_batch1_acceptance(),
            funding_contexts=[context],
            output_root=tmp_path / "batch2",
            run_id="empty-mission-run",
            batch1_run_id="batch1-test-run",
            execution_mode="sequential",
            max_workers=1,
            timeout_budget_seconds=60,
        )

    call_report = report["funding_calls"][0]
    assert report["status"] == "incomplete_no_handoff"
    assert report["batch3_started"] is False
    assert call_report["status"] == "no_relevant_evidence"
    assert call_report["normalized_records"] == 0
    assert call_report["mission_relevance_gate"]["input_records"] == 11
    assert call_report["mission_relevance_gate"]["accepted_records"] == 0
    assert call_report["mission_relevance_gate"]["rejected_records"] == 11
    assert len(call_report["mission_relevance_gate"]["details"]) == 11

    call_root = tmp_path / "batch2" / "empty-mission-run" / "calls" / context.funding_call_id
    assert json.loads((call_root / "normalized" / "records.json").read_text(encoding="utf-8")) == []
    assert (call_root / "normalized" / "input_manifest.json").is_file()
    assert (call_root / "normalized" / "evidence_ledger.db").is_file()


def test_batch2_rejects_duplicate_call_ids(tmp_path):
    context = _context()
    with pytest.raises(ValueError, match="must be unique"):
        execute_batch2(
            batch1_acceptance=_batch1_acceptance(),
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
            batch1_acceptance=_batch1_acceptance(),
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
            batch1_acceptance=_batch1_acceptance(),
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
                    "acceptance": {"status": "ACCEPTED", "accepted": True, "blockers": [], "blocker_count": 0},
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
    with pytest.raises(ValueError, match="ACCEPTED global Batch-1 source-coverage attestation"):
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
    with pytest.raises(ValueError, match="ACCEPTED global Batch-1 source-coverage attestation"):
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
    with pytest.raises(ValueError, match="ACCEPTED global Batch-1 source-coverage attestation"):
        load_funding_context_handoff(handoff)




def test_batch2_independent_execution_accepts_exactly_validated_contexts_without_global_source_attestation(tmp_path):
    context = _context()
    validation = {
        "status": "VALIDATED",
        "scope": "selected_contexts_only",
        "batch1_run_id": "batch1-independent-run",
        "validated_funding_call_ids": [context.funding_call_id],
    }
    with patch("core.batch2.execute_batch_run", return_value={
        "status": "dry_run",
        "tasks": [
            {"agent_name": agent, "status": "planned", "outputs_count": 0}
            for agent in sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS)
        ],
    }):
        report = execute_batch2(
            funding_contexts=[context],
            context_validation=validation,
            batch1_run_id="batch1-independent-run",
            output_root=tmp_path,
            run_id="batch2-independent-dry-run",
            dry_run=True,
        )
    assert report["status"] == "dry_run"
    assert report["batch1_run_id"] == "batch1-independent-run"
    assert report["batch3_started"] is False


def test_batch2_independent_context_validation_rejects_call_id_mismatch(tmp_path):
    context = _context()
    validation = {
        "status": "VALIDATED",
        "scope": "selected_contexts_only",
        "batch1_run_id": "batch1-independent-run",
        "validated_funding_call_ids": ["A-DIFFERENT-CALL"],
    }
    with pytest.raises(ValueError, match="does not match the supplied Batch-1 run and funding-call IDs"):
        execute_batch2(
            funding_contexts=[context],
            context_validation=validation,
            batch1_run_id="batch1-independent-run",
            output_root=tmp_path,
            run_id="batch2-invalid-context-validation",
            dry_run=True,
        )


def test_standalone_batch2_loads_selected_context_validation_without_global_acceptance(tmp_path):
    from scripts.pipeline_tools.run_batch2 import load_funding_context_handoff

    context = _context()
    handoff = tmp_path / "funding_contexts.json"
    handoff.write_text(json.dumps({
        "batch1_run_id": "batch1-context-only-run",
        "context_validation": {
            "status": "VALIDATED",
            "scope": "selected_contexts_only",
            "batch1_run_id": "batch1-context-only-run",
            "validated_funding_call_ids": [context.funding_call_id],
        },
        "funding_contexts": [context.to_dict()],
    }), encoding="utf-8")
    loaded, run_id = load_funding_context_handoff(handoff)
    assert run_id == "batch1-context-only-run"
    assert [item.funding_call_id for item in loaded] == [context.funding_call_id]

def test_batch2_api_rejects_missing_or_mismatched_global_acceptance(tmp_path):
    context = _context()
    with pytest.raises(ValueError, match="ACCEPTED global Batch-1 attestation"):
        execute_batch2(
            funding_contexts=[context],
            output_root=tmp_path / "missing-attestation",
            run_id="missing-attestation-run",
        )

    with pytest.raises(ValueError, match="does not match batch1_run_id"):
        execute_batch2(
            funding_contexts=[context],
            output_root=tmp_path / "mismatched-attestation",
            run_id="mismatched-attestation-run",
            batch1_run_id="batch1-run-a",
            batch1_acceptance=_batch1_acceptance("batch1-run-b"),
        )


def test_batch2_rejects_context_that_does_not_pass_batch1_handoff_gates(tmp_path):
    context = _context()
    invalid = FundingCallContext.from_dict({**context.to_dict(), "status": "CLOSED_CALL"})

    with pytest.raises(ValueError, match="not confirmed currently open"):
        execute_batch2(
            batch1_acceptance=_batch1_acceptance(),
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
            batch1_acceptance=_batch1_acceptance(),
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


def test_collection_health_preserves_six_of_eleven_live_threshold():
    expected_agents = sorted(DOWNSTREAM_FUNDING_CONTEXT_AGENTS)
    assert len(expected_agents) == 11
    assert minimum_healthy_collectors(len(expected_agents)) == 6

    tasks = [
        {
            "agent_name": agent,
            "status": "success" if index < 6 else "error",
            "outputs_count": 1 if index < 6 else 0,
        }
        for index, agent in enumerate(expected_agents)
    ]

    healthy, count = evaluate_collection_health(
        tasks,
        min_required_agents=minimum_healthy_collectors(len(expected_agents)),
        expected_agents=set(expected_agents),
    )
    assert healthy is True
    assert count == 6

    tasks[5]["status"] = "error"
    tasks[5]["outputs_count"] = 0
    healthy, count = evaluate_collection_health(
        tasks,
        min_required_agents=minimum_healthy_collectors(len(expected_agents)),
        expected_agents=set(expected_agents),
    )
    assert healthy is False
    assert count == 5


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


def test_literature_and_lab_records_supply_required_evidence_classification():
    from agents.literature_agent import Paper
    from agents.lab_agent import LabDocument
    from core.agent_interface import _document_metadata

    paper = Paper(
        title="Research paper",
        summary="A measurable research result.",
        url="https://example.org/paper",
        published=None,
        authors=["Researcher"],
        source="OpenAlex",
        source_id="url:https://example.org/paper",
        doi=None,
        venue=None,
        retrieval_timestamp="2026-11-02T10:00:00+00:00",
    )
    lab = LabDocument(
        name="Research Lab",
        institution="Example University",
        base_url="https://example.org/lab",
        source_type="Institutional profile",
        pages=[],
    )

    paper_metadata = _document_metadata(paper)
    lab_metadata = _document_metadata(lab)
    assert paper_metadata["source_type"] == "Research publication"
    assert paper_metadata["evidence_type"] == "Scholarly publication record"
    assert lab_metadata["evidence_type"] == "Research laboratory profile"


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

    from tests.batch2_test_helpers import write_test_collector_artifacts
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
            batch1_acceptance=_batch1_acceptance(),
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
            batch1_acceptance=_batch1_acceptance(),
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


def test_batch2_cli_imports_project_when_launched_outside_repository_root(tmp_path):
    repository_root = Path(__file__).resolve().parents[1]
    script = repository_root / "scripts" / "pipeline_tools" / "run_batch2.py"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Run RIF Batch-2 collection and normalization" in result.stdout


def test_batch2_context_rejects_non_url_research_evidence_reference():
    context = _context()
    context.research_context.evidence_refs = ("not-a-source-reference",)
    issues = validate_batch1_context(context)
    assert (
        "research context must contain at least one usable HTTP(S) evidence reference"
        in issues
    )


def test_batch2_context_accepts_source_grounded_http_evidence_reference():
    context = _context()
    context.research_context.evidence_refs = (
        "https://example.org/funding-call#eligibility",
    )
    assert validate_batch1_context(context) == []



def test_batch2_context_rejects_missing_call_title():
    context = _context()
    context.call_title = "   "
    assert "funding call title is missing" in validate_batch1_context(context)


@pytest.mark.parametrize("symlink_location", ["agent_root", "nested_directory"])
def test_internal_agent_symlinks_are_quarantined_without_moving_target_evidence(
    tmp_path, symlink_location
):
    from tests.batch2_test_helpers import write_test_collector_artifacts

    run_root = tmp_path / "batch2-run"
    write_test_collector_artifacts(run_root, "current-run", {"lab"})
    lab_root = run_root / "intermediate" / "lab"
    lab_file = next(lab_root.glob("*.md"))
    original_content = lab_file.read_text(encoding="utf-8")
    literature_root = run_root / "intermediate" / "literature"

    try:
        if symlink_location == "agent_root":
            literature_root.symlink_to(lab_root, target_is_directory=True)
            symlink_path = literature_root
            reported_alias = literature_root / lab_file.name
        else:
            literature_root.mkdir(parents=True)
            symlink_path = literature_root / "lab-alias"
            symlink_path.symlink_to(lab_root, target_is_directory=True)
            reported_alias = symlink_path / lab_file.name
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are not available on this platform")

    tasks = [
        {
            "agent_name": "literature",
            "status": "success",
            "outputs_count": 1,
            "output_paths": [str(reported_alias)],
            "errors": [],
            "warnings": [],
        },
        {
            "agent_name": "lab",
            "status": "success",
            "outputs_count": 1,
            "output_paths": [str(lab_file)],
            "errors": [],
            "warnings": [],
        },
    ]

    _, audit = audit_collector_artifacts(
        run_root,
        tasks,
        expected_agents={"literature", "lab"},
        run_id="current-run",
    )

    # The symlink alias is quarantined, but the real Lab artifact is untouched.
    assert not symlink_path.is_symlink()
    assert lab_file.is_file()
    assert lab_file.read_text(encoding="utf-8") == original_content
    assert audit["quarantine_failures"] == []
    assert audit["manifest_issues"]
    quarantined_reasons = [
        reason
        for item in audit["quarantined_artifacts"]
        for reason in item.get("reasons", [])
    ]
    assert (
        "agent_output_directory_is_symlink" in quarantined_reasons
        or "symlinked_agent_artifact_path" in quarantined_reasons
    )


@pytest.mark.parametrize(
    "redirected_root,target_name",
    [
        ("intermediate", "other-intermediate"),
        ("workflow", "other-workflow"),
        ("workflow/quarantined_intermediate", "other-quarantine"),
    ],
)
def test_collector_artifact_audit_rejects_internal_redirected_roots(
    tmp_path, redirected_root, target_name
):
    run_root = tmp_path / "batch2-run"
    run_root.mkdir()
    target = run_root / target_name
    target.mkdir()
    root_path = run_root / redirected_root
    root_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        root_path.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are not available on this platform")

    with pytest.raises(ValueError, match="must not be a symlink or redirected path"):
        audit_collector_artifacts(
            run_root,
            [],
            expected_agents={"literature"},
            run_id="current-run",
        )

    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_collection_finalizer_preserves_changed_content_for_same_title_and_source(tmp_path):
    from datetime import UTC, datetime
    from core.agent_interface import finalize_collection_agent_response
    from core.normalization import normalize_intermediate_artifact

    source = "https://example.org/research/technical-report"
    first_content = (
        "The report documents a measured interoperability limitation across distributed "
        "systems, including a reproducible benchmark, test conditions, and observed failure "
        "rates. The original version records a baseline result for the system under load."
    )
    revised_content = (
        "The revised report documents the same interoperability limitation across distributed "
        "systems, but adds a second benchmark, updated test conditions, and a measured reduction "
        "in failure rates after the protocol change."
    )
    first_document = {
        "title": "Distributed Systems Interoperability Report",
        "source": source,
        "content": first_content,
    }
    revised_document = {
        "title": "Distributed Systems Interoperability Report",
        "source": source,
        "content": revised_content,
    }
    # Exact duplicates collapse, but a changed body from the same URL is retained.
    documents = [first_document, dict(first_document), revised_document]

    response = finalize_collection_agent_response(
        agent="lab",
        layer="Lab",
        mode="configured_scan",
        area="DePIN",
        documents=documents,
        output_dir=tmp_path / "intermediate" / "lab",
        started_at=datetime(2026, 11, 11, tzinfo=UTC),
        run_id="same-source-content-version-test",
    )

    assert response["status"] in {"success", "partial_success"}
    assert len(response["outputs"]) == 2
    paths = [Path(item["markdown_path"]) for item in response["outputs"]]
    assert len(set(paths)) == 2
    records = [normalize_intermediate_artifact(path) for path in paths]
    assert len({record["content_hash"] for record in records}) == 2
    assert {record["source_url"] for record in records} == {source}


def test_manual_text_artifact_is_preserved_but_never_marked_external_or_batch3_ready(tmp_path):
    from datetime import UTC, datetime
    from agents.lab_agent import build_manual_text_document
    from core.agent_interface import finalize_collection_agent_response
    from core.normalization import normalize_intermediate_artifact

    run_id = "manual-text-provenance-run"
    run_root = tmp_path / run_id
    context = _context("CALL-MANUAL-TEXT")
    from tests.test_all_collection_agents import TEXTS

    document = build_manual_text_document(
        TEXTS["lab"],
        "Manual Lab Evidence",
        None,
        None,
    )
    response = finalize_collection_agent_response(
        agent="lab",
        layer="Lab",
        mode="manual_text",
        area="DePIN",
        documents=[document],
        output_dir=run_root / "intermediate" / "lab",
        started_at=datetime(2026, 11, 11, tzinfo=UTC),
        funding_context=context,
        run_id=run_id,
    )
    assert response["status"] in {"success", "partial_success"}
    assert len(response["outputs"]) == 1
    artifact_path = Path(response["outputs"][0]["markdown_path"])

    task = {
        "agent_name": "lab",
        "status": response["status"],
        "outputs_count": len(response["outputs"]),
        "output_paths": [str(artifact_path)],
        "errors": [],
        "warnings": [],
    }
    tasks, audit = audit_collector_artifacts(
        run_root,
        [task],
        expected_agents={"lab"},
        run_id=run_id,
    )

    assert audit["manifest_issues"] == []
    assert audit["quarantined_artifacts"] == []
    assert audit["agents"][0]["manual_input_artifacts"] == 1
    assert any("user-supplied inputs" in warning for warning in tasks[0]["warnings"])

    record = normalize_intermediate_artifact(artifact_path)
    assert record["source_url"] == ""
    assert record["normalization_status"] == "NEEDS_REVIEW"
    handoff_errors = validate_normalized_handoff(
        [record],
        funding_call_id=context.funding_call_id,
        run_id=run_id,
        expected_agents={"lab"},
    )
    assert any("HTTP(S) source URL" in error for error in handoff_errors)


def test_optional_adaptive_research_artifacts_are_audited_before_normalization(tmp_path):
    from datetime import UTC, datetime
    from unittest.mock import patch
    from core.agent_interface import finalize_collection_agent_response
    from run_pipeline import audit_optional_adaptive_research_artifacts

    run_id = "adaptive-artifact-audit-run"
    call_root = tmp_path / "calls" / "CALL-ADAPTIVE"
    context = _context("CALL-ADAPTIVE")
    document = {
        "title": "Distributed Systems Reliability Study",
        "source": "https://example.org/research/reliability-study",
        "content": (
            "The study identifies a reproducible reliability limitation in distributed "
            "systems under concurrent load. Controlled experiments report failure rates, "
            "latency measurements, and repeatable validation conditions."
        ),
    }
    with patch(
        "core.agent_interface.evaluate_collection_quality",
        return_value={"passed": True, "score": 1.0, "reasons": [], "warnings": []},
    ):
        response = finalize_collection_agent_response(
            agent="adaptive_research",
            layer="Unscoped",
            mode="configured_scan",
            area="Unscoped",
            documents=[document],
            output_dir=call_root / "intermediate" / "adaptive_research",
            started_at=datetime(2026, 11, 11, tzinfo=UTC),
            funding_context=context,
            funding_contexts=[context],
            run_id=run_id,
            default_source=context.source_url,
        )

    assert len(response["outputs"]) == 1
    audit = audit_optional_adaptive_research_artifacts(call_root, response, run_id)

    assert audit["status"] == "passed"
    assert audit["manifest_issues"] == []
    assert audit["quarantined_artifacts"] == []
    assert audit["agents"][0]["agent_name"] == "adaptive_research"
    assert audit["agents"][0]["persisted_valid_artifacts"] == 1
    assert (call_root / "workflow" / "adaptive_research_artifact_audit.json").is_file()


def test_optional_adaptive_stage_still_rejects_redirected_roots_without_artifacts(tmp_path):
    from run_pipeline import audit_optional_adaptive_research_artifacts

    call_root = tmp_path / "call-root"
    call_root.mkdir()
    redirected_target = call_root / "other-workflow"
    redirected_target.mkdir()
    workflow_root = call_root / "workflow"
    try:
        workflow_root.symlink_to(redirected_target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are not available on this platform")

    with pytest.raises(RuntimeError, match="Per-call artifact-root audit failed"):
        audit_optional_adaptive_research_artifacts(
            call_root,
            None,
            "adaptive-root-audit-run",
        )
