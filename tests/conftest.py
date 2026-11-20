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
