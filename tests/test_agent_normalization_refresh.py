import pytest
import json
import os
import time
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.normalization import ensure_normalized_collection_records, _normalization_input_manifest

def test_agent_normalization_refresh(tmp_path):
    call_id = "CALL-TEST-88"
    run_root = tmp_path / "outputs" / "run_abc"
    call_root = run_root / "calls" / call_id
    call_root.mkdir(parents=True)

    # 1. Setup Selected Funding Call
    workflow_dir = call_root / "workflow"
    workflow_dir.mkdir(parents=True)
    selected_call = {
        "selected": {
            "call_id": call_id,
            "title": "Quantum Networking",
            "organization": "QOrg",
            "research_area": "DePIN",
            "research_context": {
                "funding_call_id": call_id,
                "domain": "DePIN",
                "technology_themes": ["quantum", "entanglement"],
                "funding_priorities": [],
                "target_outcomes": [],
                "investigation_questions": [],
                "inferred_challenges": [],
                "evidence_refs": [],
                "inference_provenance": [],
                "confidence": "high"
            }
        },
        "rejected_alternatives": []
    }
    (workflow_dir / "selected_funding_call.json").write_text(json.dumps(selected_call), encoding="utf-8")

    # 2. Setup Evidence
    # Parent-run intermediate (shared)
    parent_intermediate = run_root / "intermediate"
    parent_intermediate.mkdir(parents=True)

    # Call-local intermediate
    local_intermediate = call_root / "intermediate"
    local_intermediate.mkdir(parents=True)

    # A) Relevant shared evidence (belongs to multiple calls including this one, passes gate)
    (parent_intermediate / "shared_relevant.md").write_text("""# Signal
## Shared Metadata
- title: Quantum entanglement sharing
- source: https://example.org/1
- layer: Literature
- funding_call_ids: CALL-TEST-88, CALL-TEST-OTHER
- research_area: DePIN
- collected_at: 2026-08-24T10:00:00+00:00

## Problem Preview
Quantum entanglement networks.
## Evidence Bundle
### Evidence Snippets
- Quantum entanglement networks.
""", encoding="utf-8")

    # B) Exclusive other-call evidence (should be excluded by ownership)
    (parent_intermediate / "other_exclusive.md").write_text("""# Signal
## Shared Metadata
- title: Quantum entanglement for other call
- source: https://example.org/2
- layer: Literature
- funding_call_id: CALL-TEST-OTHER
- research_area: DePIN
- collected_at: 2026-08-24T10:00:00+00:00

## Problem Preview
Quantum entanglement networks.
## Evidence Bundle
### Evidence Snippets
- Quantum entanglement networks.
""", encoding="utf-8")

    # C) Call-local evidence that passes gate
    (local_intermediate / "local_relevant.md").write_text("""# Signal
## Shared Metadata
- title: Local Quantum Setup
- source: https://example.org/3
- layer: Literature
- funding_call_id: CALL-TEST-88
- research_area: DePIN
- collected_at: 2026-08-24T10:00:00+00:00

## Problem Preview
Quantum entanglement setups locally.
## Evidence Bundle
### Evidence Snippets
- Quantum entanglement setups locally.
""", encoding="utf-8")

    # D) Call-local evidence that FAILS gate (rejected by mission gate)
    (local_intermediate / "local_rejected.md").write_text("""# Signal
## Shared Metadata
- title: Local Unrelated Stuff
- source: https://example.org/4
- layer: Literature
- funding_call_id: CALL-TEST-88
- research_area: DePIN
- collected_at: 2026-08-24T10:00:00+00:00

## Problem Preview
Classic networking.
## Evidence Bundle
### Evidence Snippets
- Classic networking.
""", encoding="utf-8")

    # 3. Create stale normalized records to force refresh
    normalized = call_root / "normalized"
    normalized.mkdir()
    records_json = normalized / "records.json"
    records_json.write_text("[]\n", encoding="utf-8")
    old_time = time.time() - 100
    os.utime(records_json, (old_time, old_time))

    # 4. Invoke refresh path
    ensure_normalized_collection_records(call_root)

    # 5. Verify results
    records = json.loads(records_json.read_text(encoding="utf-8"))

    titles = [r.get("title", "") for r in records]

    # Must retain relevant parent-run and local evidence
    assert "Quantum entanglement sharing" in titles
    assert "Local Quantum Setup" in titles

    # Must exclude other call
    assert "Quantum entanglement for other call" not in titles

    # Must exclude mission-gate rejected evidence
    assert "Local Unrelated Stuff" not in titles

    # Total should be exactly 2
    assert len(records) == 2

    # Validate by_area and by_layer consistency
    by_area = json.loads((normalized / "by_area.json").read_text(encoding="utf-8"))
    assert len(by_area.get("DePIN", [])) == 2

    by_layer = json.loads((normalized / "by_layer.json").read_text(encoding="utf-8"))
    assert len(by_layer.get("Literature", [])) == 2

    # 6. Verify ordinary global output-root refreshes continue to work
    global_normalized = run_root / "normalized"
    global_normalized.mkdir()
    global_records_json = global_normalized / "records.json"
    global_records_json.write_text("[]\n", encoding="utf-8")
    os.utime(global_records_json, (old_time, old_time))

    ensure_normalized_collection_records(run_root)
    global_records = json.loads(global_records_json.read_text(encoding="utf-8"))
    # The global run will process parent_intermediate (Shared & Other) but skip local_intermediate, and no gate is applied
    global_titles = [r.get("title", "") for r in global_records]
    assert "Quantum entanglement sharing" in global_titles
    assert "Quantum entanglement for other call" in global_titles
    assert "Local Quantum Setup" not in global_titles
    assert len(global_records) == 2
def test_resolve_refresh_context_missing_file(tmp_path):
    from core.normalization import resolve_refresh_context
    import pytest
    outputs_root = tmp_path / "outputs" / "pipeline_run" / "run1" / "calls" / "CALL-TEST-MISSING"
    outputs_root.mkdir(parents=True)

    with pytest.raises(FileNotFoundError) as exc:
        resolve_refresh_context(outputs_root)
    assert "Cannot safely refresh collection records for call" in str(exc.value)

def test_resolve_refresh_context_malformed_file(tmp_path):
    from core.normalization import resolve_refresh_context
    import pytest
    outputs_root = tmp_path / "outputs" / "pipeline_run" / "run1" / "calls" / "CALL-TEST-MALFORMED"
    workflow_dir = outputs_root / "workflow"
    workflow_dir.mkdir(parents=True)

    (workflow_dir / "selected_funding_call.json").write_text("{malformed}", encoding="utf-8")

    with pytest.raises(RuntimeError) as exc:
        resolve_refresh_context(outputs_root)
    assert "failed to reconstruct mission context" in str(exc.value)

def test_normalization_cache_invalidates_same_mtime_edits_and_deleted_artifacts(tmp_path: Path):
    from core.normalization import save_normalized_collection_records

    run_root = tmp_path / "outputs" / "run_cache"
    intermediate = run_root / "intermediate"
    intermediate.mkdir(parents=True)
    artifact = intermediate / "signal.md"

    def write_artifact(title: str) -> str:
        return f"""# {title}

## Shared Metadata
- Title: {title}
- Source: https://example.org/research-signal
- Layer: Literature
- Run Id: run-cache
- Collected At: 2026-10-16T10:00:00+00:00

## Problem
Evidence about research system reliability and integration for {title}.

## Evidence Bundle
### Evidence Snippets
- Research system reliability evidence for {title} contains enough detail to be retained.
"""

    artifact.write_text(write_artifact("Signal Alpha"), encoding="utf-8")
    save_normalized_collection_records(intermediate, run_root)
    records_path = run_root / "normalized" / "records.json"
    assert [r["title"] for r in json.loads(records_path.read_text(encoding="utf-8"))] == ["Signal Alpha"]

    original_stat = artifact.stat()
    artifact.write_text(write_artifact("Signal Bravo"), encoding="utf-8")
    # Simulate a restored/old timestamp: content hashing must still invalidate the cache.
    os.utime(artifact, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    ensure_normalized_collection_records(run_root)
    assert [r["title"] for r in json.loads(records_path.read_text(encoding="utf-8"))] == ["Signal Bravo"]

    # A deleted raw artifact must not survive in normalized output as stale evidence.
    artifact.unlink()
    ensure_normalized_collection_records(run_root)
    assert json.loads(records_path.read_text(encoding="utf-8")) == []
    assert json.loads((run_root / "normalized" / "by_area.json").read_text(encoding="utf-8")) == {}
    assert json.loads((run_root / "normalized" / "by_layer.json").read_text(encoding="utf-8")) == {}

def test_call_refresh_excludes_artifacts_from_other_runs(tmp_path: Path):
    call_id = "CALL-RUN-SCOPE"
    run_id = "run-current"
    run_root = tmp_path / "outputs" / run_id
    call_root = run_root / "calls" / call_id
    (call_root / "workflow").mkdir(parents=True)
    (run_root / "intermediate").mkdir(parents=True)
    (call_root / "intermediate").mkdir(parents=True)

    selected = {
        "run_id": run_id,
        "selected": {
            "call_id": call_id,
            "title": "Quantum Research Call",
            "program_name": "Quantum Research Call",
            "organization": "QOrg",
            "research_area": "DePIN",
            "research_context": {
                "funding_call_id": call_id,
                "domain": "DePIN",
                "technology_themes": ["quantum"],
                "funding_priorities": [],
                "target_outcomes": [],
                "investigation_questions": [],
                "inferred_challenges": [],
                "evidence_refs": [],
                "inference_provenance": "test",
                "confidence": "high",
            },
        },
    }
    (call_root / "workflow" / "selected_funding_call.json").write_text(
        json.dumps(selected), encoding="utf-8"
    )

    def artifact(title: str, artifact_run_id: str, source: str) -> str:
        return f"""# {title}

## Shared Metadata
- title: {title}
- source: {source}
- layer: Literature
- funding_call_id: {call_id}
- funding_call_ids: {call_id}
- research_area: DePIN
- run_id: {artifact_run_id}
- collected_at: 2026-10-16T10:00:00+00:00

## Problem Preview
Quantum research system reliability and integration evidence for {title}.

## Evidence Bundle
### Evidence Snippets
- Quantum research system reliability evidence supporting the selected program.
"""

    (run_root / "intermediate" / "old-run.md").write_text(
        artifact("Old Run Evidence", "run-previous", "https://example.org/old"),
        encoding="utf-8",
    )
    (run_root / "intermediate" / "current-run.md").write_text(
        artifact("Current Run Evidence", run_id, "https://example.org/current"),
        encoding="utf-8",
    )

    def accept(_record, _context):
        return {"passed": True, "reason": "controlled test"}

    with patch("core.mission_gate.relevance", side_effect=accept):
        ensure_normalized_collection_records(call_root)

    records = json.loads((call_root / "normalized" / "records.json").read_text(encoding="utf-8"))
    assert [record["title"] for record in records] == ["Current Run Evidence"]
    assert records[0]["run_id"] == run_id

