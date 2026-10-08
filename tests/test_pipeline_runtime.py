from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import json

from core import source_registry
from core.batch_runner import build_batch_tasks
from core.funding_selection import FundingCallContext
from core.normalization import normalize_intermediate_artifact


@dataclass
class FakeSource:
    url: str


def _registry_payload(url: str, *, area_hints=None, last_checked=None):
    return {
        "generated_at": "",
        "entries": [{
            "agent_name": "company",
            "name": "Test Source",
            "url": url,
            "area_hints": area_hints or ["DigitalHealthCPS"],
            "status": "active",
            "fetch_frequency": "weekly",
            "last_checked": last_checked or datetime.now(UTC).isoformat(),
        }],
    }


def test_live_pipeline_context_forces_recent_sources_and_routes_by_area(tmp_path: Path):
    url = "https://example.org/source"
    registry_path = tmp_path / "source_registry.json"
    registry_path.write_text(json.dumps(_registry_payload(url)), encoding="utf-8")
    src = FakeSource(url)

    source_registry.current_source_area.set("DigitalHealthCPS")
    source_registry.current_force_source_refresh.set(True)
    try:
        selected = source_registry.apply_refresh_policy("company", [src], registry_path=registry_path)
    finally:
        source_registry.current_force_source_refresh.set(False)
        source_registry.current_source_area.set(None)

    assert selected == [src]


def test_area_routing_rejects_registry_source_from_wrong_domain(tmp_path: Path):
    url = "https://example.org/source"
    registry_path = tmp_path / "source_registry.json"
    registry_path.write_text(json.dumps(_registry_payload(url, area_hints=["RWA"])), encoding="utf-8")
    src = FakeSource(url)

    token = source_registry.current_source_area.set("DigitalHealthCPS")
    try:
        selected = source_registry.apply_refresh_policy("company", [src], registry_path=registry_path, force=True)
    finally:
        source_registry.current_source_area.reset(token)

    assert selected == []


def test_batch_task_preserves_explicit_force_refresh_flag():
    tasks = build_batch_tasks(
        agent_names=["lab"],
        area_names=["DigitalHealthCPS"],
        mode="configured_scan",
        base_input_data={"force_refresh": True},
    )
    assert tasks[0]["input_data"]["force_refresh"] is True


def test_legacy_funding_markdown_focus_area_is_not_normalized_as_unscoped(tmp_path: Path):
    artifact = tmp_path / "disha.md"
    artifact.write_text(
        "\n".join([
            "        # Grant: DISHA 3.0",
            "        ## Focus Area",
            "        DigitalHealthCPS",
            "        ## Source Details",
            "        Source: manual://test",
            "        ## Layer",
            "        Funding",
        ]),
        encoding="utf-8",
    )
    normalized = normalize_intermediate_artifact(artifact)
    assert normalized["research_area"] == "DigitalHealthCPS"


def test_funding_refresh_never_returns_empty_when_all_registry_sources_are_recent(tmp_path: Path):
    from core.source_registry import apply_refresh_policy
    url = "https://example.org/funding"
    registry_path = tmp_path / "source_registry.json"
    registry_path.write_text(json.dumps({"generated_at": "", "entries": [{
        "agent_name": "funding", "name": "Test Funding", "url": url,
        "status": "active", "fetch_frequency": "weekly",
        "last_checked": datetime.now(UTC).isoformat(),
    }]}), encoding="utf-8")
    src = FakeSource(url)
    selected = apply_refresh_policy("funding", [src], registry_path=registry_path)
    assert selected == [src]


def test_refresh_policy_matches_canonical_equivalent_urls(tmp_path):
    from datetime import UTC, datetime, timedelta
    from core import source_registry

    registry = tmp_path / "registry.json"
    now = datetime.now(UTC)
    payload = {
        "generated_at": now.isoformat(),
        "entries": [{
            "source_id": "company:test:1",
            "agent_name": "company",
            "name": "Test Source",
            "url": "https://example.org/source",
            "status": "active",
            "last_checked": (now - timedelta(days=10)).isoformat(),
            "last_changed": (now - timedelta(days=10)).isoformat(),
            "fetch_frequency": "weekly",
            "relevance_score": 0.7,
            "trust_score": 0.7,
        }],
    }
    registry.write_text(__import__("json").dumps(payload), encoding="utf-8")
    selected = source_registry.apply_refresh_policy(
        "company",
        [{"name": "Test Source", "url": "HTTPS://Example.ORG/source?utm_source=test"}],
        registry_path=registry,
        reference_time=now,
    )
    assert len(selected) == 1
