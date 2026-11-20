"""Batch-3 contract: tagging -> clustering -> trends must cover the Batch-2 handoff."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from core.batch3 import processing_area, run_batch3_for_call, validate_batch3_outputs

CALL = "HORIZON-NATURE-2027-02-02"
RUN = "20261120T044310013067Z"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _call_root(tmp_path: Path, n: int = 2) -> Path:
    root = tmp_path / "run" / "calls" / CALL
    records = [
        {"record_id": f"r{i}", "evidence_id": f"e{i}", "funding_call_id": CALL, "run_id": RUN, "research_area": "ESG"}
        for i in range(n)
    ]
    _write(root / "normalized" / "records.json", records)
    for name in ("by_area.json", "by_layer.json", "errors.json", "skipped_artifacts.json", "input_manifest.json"):
        _write(root / "normalized" / name, [] if name in {"errors.json", "skipped_artifacts.json"} else {})
    return root


def _write_outputs(root: Path, ids: list[str], *, call=CALL, run=RUN, trend_records=None, clusters=None) -> None:
    _write(root / "processed" / "tags" / "tagged_records.json",
           [{"record_id": i, "funding_call_id": call, "run_id": run} for i in ids])
    clusters = clusters if clusters is not None else [
        {"cluster_id": f"ESG-{n}", "funding_call_id": call, "members": [{"record_id": i}]} for n, i in enumerate(ids, 1)
    ]
    _write(root / "processed" / "clusters" / "clusters.json", clusters)
    _write(root / "processed" / "trends" / "multi-area-trends.json", {
        "funding_call_id": call,
        "counts": {"records": len(ids) if trend_records is None else trend_records, "clusters": len(clusters)},
    })


def test_complete_outputs_are_valid(tmp_path):
    root = _call_root(tmp_path)
    _write_outputs(root, ["r0", "r1"])
    assert validate_batch3_outputs(root, CALL) == []


def test_untagged_record_is_a_violation(tmp_path):
    root = _call_root(tmp_path)
    _write_outputs(root, ["r0"])
    issues = validate_batch3_outputs(root, CALL)
    assert any("tagging does not cover" in i for i in issues)
    assert any("clusters do not cover" in i for i in issues)


def test_wrong_call_or_lost_run_identity_is_a_violation(tmp_path):
    root = _call_root(tmp_path)
    _write_outputs(root, ["r0", "r1"], call="OTHER-CALL", run=None)
    issues = validate_batch3_outputs(root, CALL)
    assert any("different funding_call_id" in i for i in issues)
    assert any("run_id" in i for i in issues)


def test_trend_count_mismatch_and_missing_clusters_are_violations(tmp_path):
    root = _call_root(tmp_path)
    _write_outputs(root, ["r0", "r1"], trend_records=5, clusters=[])
    issues = validate_batch3_outputs(root, CALL)
    assert "clustering produced no clusters" in issues
    assert any("counts 5 records" in i for i in issues)


def test_empty_handoff_is_blocked_before_any_stage_runs(tmp_path):
    root = _call_root(tmp_path, n=0)
    calls = []
    report = run_batch3_for_call(root, SimpleNamespace(funding_call_id=CALL), lambda *a: calls.append(a))
    assert report["status"] == "blocked_missing_handoff" and calls == []


def test_zero_output_partial_success_is_not_accepted(tmp_path):
    """Stages may report partial_success with nothing processed; the contract must catch it."""
    root = _call_root(tmp_path)
    report = run_batch3_for_call(
        root, SimpleNamespace(funding_call_id=CALL),
        lambda agent, *a: {"status": "partial_success", "outputs": [], "warnings": ["No normalized records matched."]},
    )
    assert report["status"] == "blocked_validation"
    assert report["issues"]


def test_stage_that_modifies_the_handoff_is_blocked(tmp_path):
    root = _call_root(tmp_path)

    def stage(agent, mode, area, payload, ctx):
        if agent == "tagging":
            _write(root / "normalized" / "records.json", [])  # destroys the Batch-2 handoff
        _write_outputs(root, ["r0", "r1"])
        return {"status": "success", "outputs": [1]}

    report = run_batch3_for_call(root, SimpleNamespace(funding_call_id=CALL), stage)
    assert report["status"] == "blocked_validation"
    assert "Batch-3 modified the Batch-2 normalized handoff" in report["issues"]


def test_failed_stage_stops_the_sequence(tmp_path):
    root = _call_root(tmp_path)
    seen = []

    def stage(agent, *a):
        seen.append(agent)
        return {"status": "error"}

    report = run_batch3_for_call(root, SimpleNamespace(funding_call_id=CALL), stage)
    assert report["status"] == "blocked_processing" and seen == ["tagging"]


def test_processing_area_matches_pipeline_rule():
    assert processing_area([{"research_area": "ESG"}, {"research_area": "ESG"}]) == "ESG"
    assert processing_area([{"research_area": "ESG"}, {"research_area": "DePIN"}]) is None
    assert processing_area([{"research_area": "ESG"}, {"research_area": ""}]) is None


def test_real_stages_produce_a_valid_batch3_on_a_normalized_handoff(tmp_path):
    """End-to-end with the real tagging/clustering/trend agents (no LLM)."""
    from core.normalization import save_normalized_collection_records
    from run_pipeline import run_stage
    from tests.test_batch4_normalization import make_artifact_md

    outputs = tmp_path / "outputs"
    (outputs / "intermediate").mkdir(parents=True)
    snippets = [
        "Current carbon-credit MRV methods lack verifiable emissions data, which limits trust in carbon markets.",
        "Living labs report that ecosystem restoration pilots face implementation bottlenecks and wildfire risk.",
    ]
    for i, snippet in enumerate(snippets):
        (outputs / "intermediate" / f"a{i}.md").write_text(
            make_artifact_md(title=f"Evidence {i}", source=f"https://example.org/{i}", funding_call_id=CALL,
                             run_id=RUN, problem=snippet, evidence_snippets=[snippet]),
            encoding="utf-8",
        )
    save_normalized_collection_records(outputs, outputs)
    report = run_batch3_for_call(outputs, SimpleNamespace(funding_call_id=CALL), run_stage)
    assert report["status"] == "ready_for_batch4", report


def test_call_scoped_normalization_ignores_batch1_funding_artifacts(tmp_path):
    """Live regression: in run_pipeline.py the refresh check scanned
    <run>/intermediate/funding (Batch-1 output), which the pipeline never
    normalizes, so the manifests differed and Batch-3 re-normalized the handoff."""
    from core.normalization import _normalization_input_manifest
    from tests.test_batch4_normalization import make_artifact_md

    inter = tmp_path / "run" / "intermediate"
    for sub, name in (("funding", "call.md"), ("literature", "paper.md")):
        (inter / sub).mkdir(parents=True)
        (inter / sub / name).write_text(make_artifact_md(funding_call_id=CALL, run_id=RUN), encoding="utf-8")
    (inter / "shared.md").write_text(make_artifact_md(funding_call_id=CALL, run_id=RUN), encoding="utf-8")

    def names(manifest):
        return sorted(Path(a["path"]).name for a in manifest["artifacts"])

    out = tmp_path / "run" / "calls" / CALL
    # Call-scoped: Batch-1 funding artifacts excluded, everything else kept.
    assert names(_normalization_input_manifest([inter], out, CALL, RUN)) == ["paper.md", "shared.md"]
    # Same manifest as the producers' per-collector roots plus shared files.
    # Unscoped normalization is unchanged and still sees the funding folder.
    assert "call.md" in names(_normalization_input_manifest([inter], tmp_path / "x", None, RUN))
