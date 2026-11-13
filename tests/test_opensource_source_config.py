from pathlib import Path

import yaml

from agents.opensource_agent import _github_repo_parts


ROOT = Path(__file__).resolve().parents[1]


def _walk_sources(node):
    if isinstance(node, dict):
        if node.get("type") == "github_repo":
            yield node
        for value in node.values():
            yield from _walk_sources(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_sources(value)


def test_configured_github_sources_are_individual_repository_urls():
    config_path = ROOT / "sources" / "opensource_sources.yaml"
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    sources = list(_walk_sources(payload.get("opensource_sources", {})))

    assert sources, "The Open Source Agent must retain configured GitHub sources."
    invalid = [
        {"name": item.get("name"), "url": item.get("url")}
        for item in sources
        if _github_repo_parts(str(item.get("url", ""))) is None
    ]
    assert not invalid, f"GitHub sources must identify owner/repository URLs: {invalid}"


def test_github_repository_parser_rejects_organization_and_lookalike_urls():
    assert _github_repo_parts("https://github.com/ethereum/go-ethereum") == (
        "ethereum",
        "go-ethereum",
    )
    assert _github_repo_parts("https://github.com/ethereum/go-ethereum/tree/master") == (
        "ethereum",
        "go-ethereum",
    )
    assert _github_repo_parts("https://github.com/ethereum/go-ethereum.git") == (
        "ethereum",
        "go-ethereum",
    )
    assert _github_repo_parts("https://github.com/ethereum") is None
    assert _github_repo_parts("https://github.com.evil.example/ethereum/go-ethereum") is None
    assert _github_repo_parts("https://evil.example/?next=https://github.com/ethereum/go-ethereum") is None
    assert _github_repo_parts("javascript://github.com/ethereum/go-ethereum") is None



def test_source_registry_sync_uses_current_opensource_repository_urls(tmp_path):
    from core.source_registry import sync_source_registry

    registry_path = tmp_path / "source_registry.json"
    result = sync_source_registry(
        sources_dir=ROOT / "sources",
        registry_path=registry_path,
        agent_filter="opensource",
    )
    assert result.active_entries > 0

    import json

    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    active = [
        entry for entry in registry["entries"]
        if entry.get("agent_name") == "opensource" and entry.get("active_config")
    ]
    assert active
    assert all(_github_repo_parts(entry["url"]) is not None for entry in active if entry["source_type"] == "github_repo")
    assert not any(
        entry.get("url") in {
            "https://github.com/iden3",
            "https://github.com/starkware-libs",
            "https://github.com/filecoin-project",
            "https://github.com/helium",
            "https://github.com/flashbots",
            "https://github.com/jito-foundation",
            "https://github.com/makerdao",
        }
        for entry in active
    )
