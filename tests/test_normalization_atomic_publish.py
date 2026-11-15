from __future__ import annotations

from pathlib import Path

import pytest

from core.normalization import normalize_intermediate_artifact, save_normalized_collection_records


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
