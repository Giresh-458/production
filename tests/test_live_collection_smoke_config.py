from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.live_collection_smoke import (
    AGENT_CONFIGS,
    PER_AGENT_DEADLINE_SECONDS,
    PREFERRED_SOURCES,
    _prepare_one_source_config,
)


def _source_entries(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if value.get("name") and value.get("url"):
            found.append(value)
        for child in value.values():
            found.extend(_source_entries(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_source_entries(child))
    return found


@pytest.mark.parametrize("agent", sorted(AGENT_CONFIGS))
def test_smoke_config_contains_exactly_one_bounded_real_source(agent: str, tmp_path: Path):
    report: dict[str, Any] = {}
    path = _prepare_one_source_config(agent, tmp_path, report)
    configured = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = _source_entries(configured)

    assert len(entries) == 1, (agent, entries)
    assert entries[0]["url"] == report["configured_source"]["url"]
    assert entries[0]["url"].startswith(("https://", "http://"))
    assert entries[0]["limits"]["max_pages"] == 1
    assert entries[0]["limits"]["max_documents"] == 1
    assert entries[0]["limits"]["max_depth"] == 1
    assert entries[0]["limits"]["max_seconds"] == PER_AGENT_DEADLINE_SECONDS


@pytest.mark.parametrize(
    ("agent", "expected_name"),
    [
        ("company", "Securitize"),
        ("expert", "IISc CSA Faculty"),
        ("lab", "Chainlink Labs"),
        ("practitioner", "Ethereum Research"),
    ],
)
def test_preferred_sources_respect_declared_priority(agent: str, expected_name: str, tmp_path: Path):
    report: dict[str, Any] = {}
    _prepare_one_source_config(agent, tmp_path, report)
    assert report["configured_source"]["name"] == expected_name
    assert PREFERRED_SOURCES[agent][0] == expected_name
