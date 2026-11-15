from __future__ import annotations

from pathlib import Path

import pytest

from core.normalization import (
    ensure_normalized_collection_records,
    normalize_intermediate_artifact,
    save_normalized_collection_records,
)
from core.agent_interface import save_intermediate_markdown


def _artifact(evidence: str, run_id: str = "RUN-ATOMIC") -> str:
    return f"""# Atomic publication test

## Shared Metadata
- Title: Atomic publication test
- Source: https://example.org/research/atomic
- Funding Call Id: CALL-ATOMIC
- Run Id: {run_id}
- Layer: Literature
- Collected At: 2026-11-15T10:00:00Z

## Problem
A sufficiently detailed research problem statement for atomic publication.

## Evidence Bundle
### Evidence Snippets
- {evidence}
"""


def test_ledger_failure_preserves_previous_normalized_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = tmp_path / "outputs"
    intermediate = outputs / "intermediate"
    intermediate.mkdir(parents=True)
    artifact = intermediate / "source.md"
    artifact.write_text(
        _artifact("The original evidence statement is long enough to become a claim."),
        encoding="utf-8",
    )

    published = save_normalized_collection_records(outputs, outputs)
    normalized_dir = outputs / "normalized"
    before = {
        path.name: path.read_bytes()
        for path in normalized_dir.iterdir()
        if path.is_file()
    }
    assert set(before) == {
        "records.json",
        "input_manifest.json",
        "by_area.json",
        "by_layer.json",
        "evidence_ledger.db",
        "errors.json",
    }

    artifact.write_text(
        _artifact("A changed evidence statement triggers a fresh normalization generation."),
        encoding="utf-8",
    )

    def fail_ledger_insert(*args, **kwargs):
        raise RuntimeError("injected ledger write failure")

    monkeypatch.setattr("core.normalization.upsert_canonical_evidence", fail_ledger_insert)

    with pytest.raises(RuntimeError, match="previous normalized outputs were preserved"):
        save_normalized_collection_records(outputs, outputs)

    after = {
        path.name: path.read_bytes()
        for path in normalized_dir.iterdir()
        if path.is_file()
    }
    assert after == before
    assert not list(outputs.glob(".normalized-stage-*"))
    assert not list(outputs.glob(".normalized-backup-*"))



def test_evidence_content_hash_includes_evidence_snippets(tmp_path: Path) -> None:
    intermediate = tmp_path / "outputs" / "intermediate"
    intermediate.mkdir(parents=True)
    first = intermediate / "first.md"
    second = intermediate / "second.md"
    first.write_text(
        _artifact("The source reports that the protocol reduced latency by twenty percent."),
        encoding="utf-8",
    )
    second.write_text(
        _artifact("The source reports that the protocol increased latency by twenty percent."),
        encoding="utf-8",
    )

    first_record = normalize_intermediate_artifact(first)
    second_record = normalize_intermediate_artifact(second)

    assert first_record["source_id"] == second_record["source_id"]
    assert first_record["content_hash"] != second_record["content_hash"]


def test_deduplicated_evidence_retains_all_observed_run_ids(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    intermediate = outputs / "intermediate"
    intermediate.mkdir(parents=True)
    evidence = "This stable evidence statement is repeated across two collection runs."
    (intermediate / "run-a.md").write_text(_artifact(evidence, "RUN-A"), encoding="utf-8")
    (intermediate / "run-b.md").write_text(_artifact(evidence, "RUN-B"), encoding="utf-8")

    paths = save_normalized_collection_records(outputs, outputs)
    import json

    records = json.loads(paths["records"].read_text(encoding="utf-8"))
    assert len(records) == 1
    assert records[0]["observation_count"] == 2
    assert records[0]["run_ids"] == ["RUN-A", "RUN-B"]


def test_atomic_text_writer_preserves_existing_file_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.file_lock import atomic_write_text

    target = tmp_path / "artifact.md"
    target.write_text("previous complete artifact", encoding="utf-8")

    def fail_replace(*args, **kwargs):
        raise OSError("injected replace failure")

    monkeypatch.setattr("core.file_lock.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        atomic_write_text(target, "new artifact")

    assert target.read_text(encoding="utf-8") == "previous complete artifact"
    assert not list(tmp_path.glob("artifact.md.*.tmp"))


@pytest.mark.parametrize(
    "source_url",
    [
        "https://user:secret@example.org/research",
        "https://example.org/path with spaces",
        "https://example.org:99999/research",
    ],
)
def test_invalid_source_urls_are_marked_for_review(
    tmp_path: Path, source_url: str
) -> None:
    path = tmp_path / "artifact.md"
    path.write_text(
        _artifact("This evidence statement is sufficiently long for normalization.").replace(
            "https://example.org/research/atomic", source_url
        ),
        encoding="utf-8",
    )

    record = normalize_intermediate_artifact(path)
    assert record["source_url"] == ""
    assert record["canonical_url"] == ""
    assert record["normalization_status"] == "NEEDS_REVIEW"


def test_normalization_schema_version_is_recorded_and_fingerprinted(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    intermediate = outputs / "intermediate"
    intermediate.mkdir(parents=True)
    (intermediate / "source.md").write_text(
        _artifact("A stable evidence statement for normalization schema versioning."),
        encoding="utf-8",
    )

    paths = save_normalized_collection_records(outputs, outputs)
    import json

    manifest = json.loads((outputs / "normalized" / "input_manifest.json").read_text(encoding="utf-8"))
    records = json.loads(paths["records"].read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert records[0]["normalization_schema_version"] == 2



def test_intermediate_writer_keeps_distinct_run_snapshots(tmp_path: Path) -> None:
    def payload(run_id: str) -> dict:
        return {
            "shared_header": {
                "title": "Run snapshot",
                "source": "https://example.org/run-snapshot",
                "layer": "Custom",
                "research_area": "DePIN",
                "collected_at": "2026-11-15T10:00:00Z",
                "agent_name": "custom",
                "agent_version": "v1",
                "collection_mode": "manual_text",
                "run_id": run_id,
            },
            "evidence_bundle": {
                "page_title": "Run snapshot",
                "section_headings": [],
                "key_paragraphs": ["A substantive paragraph describing the collected source."],
                "bullet_lists": [],
                "quoted_passages": [],
                "links_found": [],
                "named_entities_or_programs": [],
                "evidence_snippets": ["A substantive evidence snippet for the run snapshot."],
                "excerpt_windows": [],
            },
            "metadata": {"artifact_schema": "shared-metadata-header-plus-agent-body-v1"},
            "provenance": {
                "source_url": "https://example.org/run-snapshot",
                "collected_at": "2026-11-15T10:00:00Z",
            },
            "problem_intelligence": {"selected_problem": "A sufficiently specific research problem."},
            "agent_specific_body": {},
            "raw_content": "A substantive source body used to generate the intermediate artifact.",
        }

    first = save_intermediate_markdown(payload=payload("RUN-A"), output_dir=tmp_path / "intermediate")
    second = save_intermediate_markdown(payload=payload("RUN-B"), output_dir=tmp_path / "intermediate")

    assert first != second
    assert first.exists()
    assert second.exists()



def test_dedupe_merges_call_ids_and_raw_artifact_provenance(tmp_path: Path) -> None:
    import json
    import sqlite3

    outputs = tmp_path / "outputs"
    intermediate = outputs / "intermediate"
    intermediate.mkdir(parents=True)
    evidence = "The same shared evidence is attributed to multiple explicit funding calls."
    first = _artifact(evidence, "RUN-A").replace(
        "- Funding Call Id: CALL-ATOMIC",
        "- Funding Call Ids: CALL-A, CALL-B",
    )
    second = _artifact(evidence, "RUN-B").replace(
        "- Funding Call Id: CALL-ATOMIC",
        "- Funding Call Ids: CALL-B, CALL-C",
    )
    (intermediate / "first.md").write_text(first, encoding="utf-8")
    (intermediate / "second.md").write_text(second, encoding="utf-8")

    paths = save_normalized_collection_records(outputs, outputs)
    records = json.loads(paths["records"].read_text(encoding="utf-8"))
    assert len(records) == 1
    record = records[0]
    assert record["funding_call_ids"] == ["CALL-A", "CALL-B", "CALL-C"]
    assert record["run_ids"] == ["RUN-A", "RUN-B"]
    assert len(record["raw_record_paths"]) == 2

    with sqlite3.connect(outputs / "normalized" / "evidence_ledger.db") as conn:
        row = conn.execute("SELECT provenance FROM canonical_evidence").fetchone()
        groups = conn.execute("SELECT COUNT(*) FROM independence_groups").fetchone()[0]
    provenance = json.loads(row[0])
    assert provenance["funding_call_ids"] == ["CALL-A", "CALL-B", "CALL-C"]
    assert len(provenance["raw_record_paths"]) == 2
    assert groups == 1


def test_latest_content_version_wins_when_collection_timestamps_match(tmp_path: Path) -> None:
    outputs = tmp_path / "outputs"
    intermediate = outputs / "intermediate"
    intermediate.mkdir(parents=True)
    evidence = "A stable evidence snippet shared by two content versions."
    first = _artifact(evidence).replace(
        "A sufficiently detailed research problem statement for atomic publication.",
        "Earlier context says the research system has a narrow capability.",
    )
    second = _artifact(evidence).replace(
        "A sufficiently detailed research problem statement for atomic publication.",
        "Later context says the research system has a broader capability.",
    )
    (intermediate / "a.md").write_text(first, encoding="utf-8")
    (intermediate / "z.md").write_text(second, encoding="utf-8")

    paths = save_normalized_collection_records(outputs, outputs)
    import json

    records = json.loads(paths["records"].read_text(encoding="utf-8"))
    assert len(records) == 1
    assert "broader capability" in records[0]["problem_statement"]
    assert len(records[0]["content_versions"]) == 2


def test_missing_or_corrupt_ledger_forces_cache_rebuild(tmp_path: Path) -> None:
    import sqlite3

    outputs = tmp_path / "outputs"
    intermediate = outputs / "intermediate"
    intermediate.mkdir(parents=True)
    (intermediate / "source.md").write_text(
        _artifact("A complete evidence statement for the cache consistency test."),
        encoding="utf-8",
    )
    save_normalized_collection_records(outputs, outputs)

    ledger = outputs / "normalized" / "evidence_ledger.db"
    ledger.unlink()
    ensure_normalized_collection_records(outputs)
    assert ledger.is_file()
    assert sqlite3.connect(ledger).execute(
        "SELECT COUNT(*) FROM canonical_evidence"
    ).fetchone()[0] == 1

    ledger.write_bytes(b"not a valid sqlite database")
    ensure_normalized_collection_records(outputs)
    with sqlite3.connect(ledger) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM canonical_evidence").fetchone()[0] == 1
