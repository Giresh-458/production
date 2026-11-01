from __future__ import annotations

import json
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
