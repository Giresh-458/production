from __future__ import annotations

from pathlib import Path

import pytest

from core.normalization import save_normalized_collection_records


def _artifact(evidence: str) -> str:
    return f"""# Atomic publication test

## Shared Metadata
- Title: Atomic publication test
- Source: https://example.org/research/atomic
- Funding Call Id: CALL-ATOMIC
- Run Id: RUN-ATOMIC
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
