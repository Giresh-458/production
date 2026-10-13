from __future__ import annotations

import pickle
from pathlib import Path

from agents import funding_agent
from core.schemas import CollectionResult


def _source() -> funding_agent.FundingSource:
    return funding_agent.FundingSource(
        name="Sample Funding Source",
        url="https://funding.example.org/calls",
        tier="tier_1",
        focus="DePIN research grants",
        collection={"mode": "web", "discovery": {"max_depth": 1, "max_pages": 1}},
        research_domains=["DePIN"],
    )


def _run_scan(monkeypatch, tmp_path: Path, *, checkpoint_path: Path | None = None):
    source = _source()
    seen_source_batches: list[list[str]] = []

    monkeypatch.setattr(funding_agent, "load_sources", lambda _path: [source])

    import agents.funding_collector as funding_collector

    def fake_collect_auto_sources(sources, area=None, warnings=None, is_test_mode=False):
        seen_source_batches.append([item.name for item in sources])
        for item in sources:
            yield CollectionResult(
                source_id=item.name,
                method="web",
                final_status="NO_ACTIVE_OPPORTUNITIES",
            )

    monkeypatch.setattr(funding_collector, "collect_auto_sources", fake_collect_auto_sources)

    payload = {
        "sources_path": str(tmp_path / "sources.yaml"),
        "output_dir": str(tmp_path / "output"),
        "db_path": str(tmp_path / "funding.db"),
        "analysis_mode": "collect_only",
        "force_refresh": True,
        "require_domain_mappings": True,
        "run_id": "fresh-run",
    }
    if checkpoint_path is not None:
        payload["checkpoint_path"] = str(checkpoint_path)

    result = funding_agent.run_agent("configured_scan", input_data=payload)
    return seen_source_batches, result, source


def test_funding_scan_ignores_legacy_checkpoint_from_previous_run(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    # This is the shape of the legacy shared checkpoint, which has no run
    # identity or source-config fingerprint.
    (scratch / "funding_checkpoint.pkl").write_bytes(
        pickle.dumps({
            "completed_sources": {"Sample Funding Source"},
            "collection_results": [],
            "documents": [],
        })
    )

    seen_batches, result, source = _run_scan(monkeypatch, tmp_path)

    assert seen_batches == [[source.name]]
    assert result["collection_results"][0]["source_id"] == source.name


def test_funding_scan_ignores_checkpoint_when_source_config_fingerprint_differs(
    monkeypatch, tmp_path: Path
):
    monkeypatch.chdir(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    checkpoint = scratch / "funding_checkpoint.pkl"
    checkpoint.write_bytes(
        pickle.dumps({
            "run_id": "fresh-run",
            "source_fingerprint": "fingerprint-from-another-catalogue",
            "completed_sources": {"Sample Funding Source"},
            "collection_results": [],
            "documents": [],
        })
    )

    seen_batches, result, source = _run_scan(
        monkeypatch, tmp_path, checkpoint_path=checkpoint
    )

    assert seen_batches == [[source.name]]
    assert result["collection_results"][0]["source_id"] == source.name


def test_funding_scan_resumes_checkpoint_for_same_run_and_source_config(
    monkeypatch, tmp_path: Path
):
    monkeypatch.chdir(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source = _source()
    fingerprint = funding_agent._funding_sources_fingerprint([source])
    checkpoint = scratch / "funding_checkpoint_fresh-run.pkl"
    checkpoint.write_bytes(
        pickle.dumps({
            "run_id": "fresh-run",
            "source_fingerprint": fingerprint,
            "completed_sources": {source.name},
            "collection_results": [
                CollectionResult(
                    source_id=source.name,
                    method="web",
                    final_status="NO_ACTIVE_OPPORTUNITIES",
                )
            ],
            "documents": [],
        })
    )

    seen_batches, result, _ = _run_scan(monkeypatch, tmp_path)

    assert seen_batches == [[]]
    assert result["collection_results"][0]["source_id"] == source.name
