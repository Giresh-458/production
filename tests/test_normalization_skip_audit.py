from __future__ import annotations

import json
import pytest
from pathlib import Path

from core.normalization import save_normalized_collection_records


def _artifact(
    *,
    title: str,
    run_id: str,
    funding_call_id: str = "",
    funding_call_ids: str = "",
) -> str:
    return f"""# {title}

## Shared Metadata
- title: {title}
- source: https://example.org/evidence/{title.lower().replace(' ', '-')}
- layer: Literature
- research_area: RWA
- collected_at: 2026-11-01T10:00:00+00:00
- agent_name: literature
- run_id: {run_id}
- funding_call_id: {funding_call_id}
- funding_call_ids: {funding_call_ids}
- collection_mode: configured_scan

## Provenance
- source_url: https://example.org/evidence/{title.lower().replace(' ', '-')}

## Problem
Zero knowledge verification throughput degrades under high transaction volume and limits secure settlement.

## Evidence Bundle
### Evidence Snippets
- Zero knowledge verification throughput degrades under high transaction volume and limits secure settlement.
"""


def test_normalization_reports_skips_and_never_overrides_explicit_call_owner(tmp_path: Path):
    run_id = "run-normalization-audit"
    target_call = "CALL-A"
    intermediate = tmp_path / "intermediate"
    intermediate.mkdir()

    (intermediate / "explicit-other-owner.md").write_text(
        _artifact(
            title="Explicit Other Owner",
            run_id=run_id,
            funding_call_id="CALL-B",
            funding_call_ids="CALL-A, CALL-B",
        ),
        encoding="utf-8",
    )
    (intermediate / "shared-unassigned.md").write_text(
        _artifact(
            title="Shared Unassigned Evidence",
            run_id=run_id,
            funding_call_ids="CALL-A, CALL-B",
        ),
        encoding="utf-8",
    )
    (intermediate / "ambiguous-many-calls.md").write_text(
        _artifact(
            title="Ambiguous Multi-call Evidence",
            run_id=run_id,
            funding_call_ids="CALL-A, CALL-B, CALL-C, CALL-D",
        ),
        encoding="utf-8",
    )
    (intermediate / "stale-run.md").write_text(
        _artifact(
            title="Stale Run Evidence",
            run_id="previous-run",
            funding_call_id=target_call,
            funding_call_ids=target_call,
        ),
        encoding="utf-8",
    )
    (intermediate / "README.md").write_text(
        "# Intermediate Folder Notes\n\nThis is documentation, not collected evidence.\n",
        encoding="utf-8",
    )

    paths = save_normalized_collection_records(
        intermediate,
        tmp_path / "calls" / target_call,
        target_call,
        target_run_id=run_id,
    )

    records = json.loads(paths["records"].read_text(encoding="utf-8"))
    skipped = json.loads(paths["skipped_artifacts"].read_text(encoding="utf-8"))
    errors = json.loads(paths["errors"].read_text(encoding="utf-8"))

    assert [record["title"] for record in records] == ["Shared Unassigned Evidence"]
    assert records[0]["funding_call_id"] == target_call
    assert records[0]["run_id"] == run_id
    assert errors == []

    reasons = {item["reason"] for item in skipped}
    assert "explicit_funding_call_id_mismatch" in reasons
    assert "ambiguous_multi_call_attribution" in reasons
    assert "run_id_mismatch" in reasons
    assert "not_intermediate_artifact" in reasons


def test_legacy_normalization_cache_is_rebuilt_when_skip_audit_is_missing(tmp_path: Path):
    run_root = tmp_path / "run-cache-audit"
    intermediate = run_root / "intermediate"
    intermediate.mkdir(parents=True)
    (intermediate / "signal.md").write_text(
        _artifact(
            title="Cache Audit Evidence",
            run_id="run-cache-audit",
            funding_call_id="",
            funding_call_ids="",
        ),
        encoding="utf-8",
    )

    save_normalized_collection_records(intermediate, run_root)
    skipped_path = run_root / "normalized" / "skipped_artifacts.json"
    assert skipped_path.exists()
    skipped_path.unlink()

    from core.normalization import ensure_normalized_collection_records

    outputs = ensure_normalized_collection_records(run_root)
    assert outputs["skipped_artifacts"].exists()
    assert json.loads(outputs["records"].read_text(encoding="utf-8"))[0]["title"] == "Cache Audit Evidence"


def test_failed_normalization_generation_preserves_previous_outputs(tmp_path: Path, monkeypatch):
    from core import normalization

    run_root = tmp_path / "atomic-normalization"
    intermediate = run_root / "intermediate"
    intermediate.mkdir(parents=True)
    (intermediate / "signal.md").write_text(
        _artifact(
            title="Atomic Publication Evidence",
            run_id="run-atomic-publication",
        ),
        encoding="utf-8",
    )

    paths = save_normalized_collection_records(intermediate, run_root)
    normalized_root = run_root / "normalized"
    published_names = (
        "records.json",
        "input_manifest.json",
        "by_area.json",
        "by_layer.json",
        "evidence_ledger.db",
        "errors.json",
        "skipped_artifacts.json",
    )
    before = {
        name: (normalized_root / name).read_bytes()
        for name in published_names
    }

    original_write = normalization.atomic_write_json

    def fail_during_staging(path, payload):
        if Path(path).name == "by_area.json":
            raise OSError("injected staging failure")
        return original_write(path, payload)

    monkeypatch.setattr(normalization, "atomic_write_json", fail_during_staging)
    with pytest.raises(OSError, match="injected staging failure"):
        save_normalized_collection_records(intermediate, run_root)

    assert {
        name: (normalized_root / name).read_bytes()
        for name in published_names
    } == before
    assert not list(run_root.glob(".normalized.staging-*"))


def test_normalization_cache_rebuilds_when_ledger_relationships_are_missing(tmp_path: Path):
    import sqlite3

    from core.normalization import (
        _normalization_cache_ledger_matches_records,
        ensure_normalized_collection_records,
    )

    run_root = tmp_path / "run-ledger-relationships"
    intermediate = run_root / "intermediate"
    intermediate.mkdir(parents=True)
    artifact = _artifact(
        title="Ledger Relationship Evidence",
        run_id="run-ledger-relationships",
    ).replace(
        "## Evidence Bundle\n",
        "## Evidence Bundle\n"
        "### Named Entities or Programs\n"
        "- National Institute of Standards and Technology\n",
    )
    (intermediate / "signal.md").write_text(artifact, encoding="utf-8")

    paths = save_normalized_collection_records(intermediate, run_root)
    records = json.loads(paths["records"].read_text(encoding="utf-8"))
    ledger_path = run_root / "normalized" / "evidence_ledger.db"

    connection = sqlite3.connect(ledger_path)
    before_claim_links = connection.execute(
        "SELECT COUNT(*) FROM evidence_claims"
    ).fetchone()[0]
    before_entity_links = connection.execute(
        "SELECT COUNT(*) FROM evidence_entities"
    ).fetchone()[0]
    assert before_claim_links > 0
    assert before_entity_links > 0

    connection.execute("DELETE FROM evidence_claims")
    connection.execute("DELETE FROM evidence_entities")
    connection.commit()
    connection.close()

    assert not _normalization_cache_ledger_matches_records(ledger_path, records)

    ensure_normalized_collection_records(run_root)
    connection = sqlite3.connect(ledger_path)
    after_claim_links = connection.execute(
        "SELECT COUNT(*) FROM evidence_claims"
    ).fetchone()[0]
    after_entity_links = connection.execute(
        "SELECT COUNT(*) FROM evidence_entities"
    ).fetchone()[0]
    connection.close()

    assert after_claim_links == before_claim_links
    assert after_entity_links == before_entity_links


def test_normalization_marks_tampered_modern_artifact_needs_review(tmp_path: Path):
    from datetime import UTC, datetime

    from core.agent_interface import (
        build_intermediate_artifact_payload,
        save_intermediate_markdown,
    )
    from core.normalization import normalize_intermediate_artifact

    payload, _ = build_intermediate_artifact_payload(
        agent="data_availability",
        layer="Data Availability",
        mode="manual_text",
        area="RWA",
        title="Verified dataset catalog",
        source="https://example.org/verified-catalog",
        content=(
            "The dataset catalog documents data access, schema coverage, "
            "update frequency, provenance, and benchmark reproducibility "
            "for research teams."
        ),
        collected_at=datetime(2026, 11, 14, 10, 0, tzinfo=UTC),
    )
    payload["agent_specific_body"] = {
        "source_type": "dataset_catalog",
        "evidence_type": "dataset_listing",
        "focus": "RWA",
    }
    payload["shared_header"]["funding_call_id"] = "CALL-PROVENANCE-CHECK-001"
    payload["shared_header"]["funding_call_ids"] = ["CALL-PROVENANCE-CHECK-001"]
    payload["shared_header"]["run_id"] = "run-provenance-check"

    artifact_path = save_intermediate_markdown(payload=payload, output_dir=tmp_path)
    original = artifact_path.read_text(encoding="utf-8")
    assert "update frequency" in original
    artifact_path.write_text(
        original.replace("update frequency", "tampered update frequency", 1),
        encoding="utf-8",
    )

    record = normalize_intermediate_artifact(artifact_path)
    assert record["normalization_status"] == "NEEDS_REVIEW"
    assert "provenance_content_hash_mismatch" in record["provenance_validation_issues"]
