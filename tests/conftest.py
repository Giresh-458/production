import pytest
import core.llm_provider as llm_provider

@pytest.fixture(autouse=True)
def mock_llm_provider(monkeypatch):
    def fake_generate(prompt: str, model: str | None = None) -> str:
        return '{"same_cluster": false, "confidence": 0.5, "reason": "mocked", "supported": true, "aligned": true}'
    monkeypatch.setattr(llm_provider, "generate", fake_generate)


@pytest.fixture
def mocked_batch3_outputs(monkeypatch):
    """For orchestration tests whose mocked run_stage writes no processing files.

    The real Batch-3 output contract is exercised in tests/test_batch3_contract.py;
    tests that mock tagging/clustering/trend opt into this fixture explicitly.
    """
    import core.batch3 as batch3

    monkeypatch.setattr(batch3, "validate_batch3_outputs", lambda call_root, funding_call_id: [])


@pytest.fixture(autouse=True)
def _isolate_source_registry_writes(monkeypatch, tmp_path):
    """Tests must never rewrite the tracked sources/source_registry.json.

    Batch-2 registry preparation and agent runs call sync_source_registry() with its
    default path (the real file); found by running the suite with the file read-only
    (tests/test_adaptive_area_filtering.py, tests/test_batch2_runner.py). Redirect the
    default to a per-test copy; an explicit registry_path is still honoured.
    """
    import shutil
    import core.agent_registry as agent_registry
    import core.batch2 as batch2
    import core.source_registry as source_registry

    original = source_registry.sync_source_registry
    temp_registry = tmp_path / "_isolated_registry" / "source_registry.json"
    temp_registry.parent.mkdir(parents=True, exist_ok=True)
    if source_registry.SOURCE_REGISTRY_PATH.exists():
        shutil.copyfile(source_registry.SOURCE_REGISTRY_PATH, temp_registry)

    def isolated_sync(*args, **kwargs):
        kwargs.setdefault("registry_path", temp_registry)
        return original(*args, **kwargs)

    for module in (source_registry, batch2, agent_registry):
        monkeypatch.setattr(module, "sync_source_registry", isolated_sync)
    yield


@pytest.fixture(autouse=True, scope="session")
def _tracked_registry_unchanged_guard():
    """Fail the run if any test still modifies the tracked registry file."""
    from core.source_registry import SOURCE_REGISTRY_PATH

    before = SOURCE_REGISTRY_PATH.read_bytes() if SOURCE_REGISTRY_PATH.exists() else None
    yield
    after = SOURCE_REGISTRY_PATH.read_bytes() if SOURCE_REGISTRY_PATH.exists() else None
    if before is not None and after != before:
        SOURCE_REGISTRY_PATH.write_bytes(before)  # restore, then fail loudly
        pytest.fail("A test modified the tracked sources/source_registry.json (restored).", pytrace=False)


@pytest.fixture(autouse=True)
def _lexical_mission_gate_in_unit_tests(monkeypatch):
    """Unit tests run the mission gate lexical-only (deterministic, no model load).

    Production defaults the semantic pass on (core.mission_gate); tests that cover
    the semantic tier set RIF_SEMANTIC_GATE explicitly.
    """
    monkeypatch.setenv("RIF_SEMANTIC_GATE", "false")
