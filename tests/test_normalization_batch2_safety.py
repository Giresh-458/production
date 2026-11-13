from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.normalization import build_normalized_collection_records, save_normalized_collection_records


def test_conflicting_singular_funding_owner_cannot_be_overridden_by_shared_ids(tmp_path: Path):
    intermediate = tmp_path / "intermediate"
    intermediate.mkdir()
    artifact = intermediate / "conflicting-owner.md"
    artifact.write_text(
        """# Shared evidence

## Shared Metadata
- title: Resilient decentralized infrastructure
- source: https://example.org/evidence
- agent_name: literature
- layer: Literature
- funding_call_id: CALL-B
- funding_call_ids: CALL-A, CALL-B
- run_id: run-current
- research_area: DePIN
- collected_at: 2026-11-13T10:00:00+00:00

## Problem
Reliability under intermittent connectivity remains an unresolved research challenge.

## Evidence Bundle
### Evidence Snippets
- Reliability under intermittent connectivity remains an unresolved research challenge.
""",
        encoding="utf-8",
    )

    records = build_normalized_collection_records(
        intermediate,
        target_funding_call_id="CALL-A",
        target_run_id="run-current",
    )
    assert records == []

    records_for_owner = build_normalized_collection_records(
        intermediate,
        target_funding_call_id="CALL-B",
        target_run_id="run-current",
    )
    assert len(records_for_owner) == 1
    assert records_for_owner[0]["funding_call_id"] == "CALL-B"


def test_failed_normalization_write_preserves_previous_valid_output(monkeypatch, tmp_path: Path):
    intermediate = tmp_path / "intermediate"
    intermediate.mkdir()
    output_root = tmp_path / "run"
    existing = output_root / "normalized"
    existing.mkdir(parents=True)
    records_path = existing / "records.json"
    records_path.write_text('[{"record_id":"previous-valid"}]\n', encoding="utf-8")

    original_write_text = Path.write_text

    def fail_new_records(self, data, *args, **kwargs):
        if self.name == "records.json" and ".normalized-stage-" in str(self.parent):
            raise OSError("simulated disk write failure")
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_new_records)

    with pytest.raises(OSError, match="simulated disk write failure"):
        save_normalized_collection_records(intermediate, output_root)

    assert json.loads(records_path.read_text(encoding="utf-8")) == [{"record_id": "previous-valid"}]
