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



def test_github_snapshot_uses_repository_api_and_preserves_activity(monkeypatch):
    from agents import opensource_agent

    requested = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get_response(url, **kwargs):
        requested.append(url)
        if url.endswith("/repos/example/project"):
            payload = {
                "full_name": "example/project",
                "description": "A protocol research implementation",
                "stargazers_count": 123,
                "forks_count": 12,
                "open_issues_count": 2,
            }
        elif url.endswith("/issues"):
            payload = [{
                "title": "Improve verifier throughput",
                "body": "A benchmark shows a scalability bottleneck.",
                "html_url": "https://github.com/example/project/issues/1",
                "labels": [{"name": "performance"}],
                "updated_at": "2026-11-01T00:00:00Z",
            }]
        elif url.endswith("/pulls"):
            payload = [{"title": "Parallel verification", "html_url": "https://github.com/example/project/pull/2"}]
        elif url.endswith("/releases"):
            payload = [{"name": "v1.2.0", "tag_name": "v1.2.0", "published_at": "2026-10-01T00:00:00Z"}]
        elif url.endswith("/commits"):
            payload = [{"commit": {"message": "Improve verification performance"}, "html_url": "https://github.com/example/project/commit/abc"}]
        elif url.endswith("/security-advisories"):
            payload = [{"summary": "Example advisory", "severity": "high"}]
        else:
            raise AssertionError(f"Unexpected GitHub API URL: {url}")
        return FakeResponse(payload)

    monkeypatch.setattr(opensource_agent, "load_requests_and_bs4", lambda: (None, None))
    monkeypatch.setattr("core.http_client.get_response", fake_get_response)

    source = opensource_agent.OpenSourceSource(
        name="Example project",
        type="github_repo",
        url="https://github.com/example/project",
        focus="protocol scalability",
    )
    content, metadata = opensource_agent.fetch_github_snapshot(source)

    assert "GitHub repository: example/project" in content
    assert "Improve verifier throughput" in content
    assert "Parallel verification" in content
    assert "v1.2.0" in content
    assert metadata["github_activity"]["open_issues"] == 1
    assert metadata["github_activity"]["open_pull_requests"] == 1
    assert metadata["github_activity"]["security_advisories"] == 1
    assert len(requested) == 6
