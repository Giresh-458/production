import pytest
import core.llm_provider as llm_provider

@pytest.fixture(autouse=True)
def mock_llm_provider(monkeypatch):
    def fake_generate(prompt: str, model: str | None = None) -> str:
        return '{"same_cluster": false, "confidence": 0.5, "reason": "mocked", "supported": true, "aligned": true}'
    monkeypatch.setattr(llm_provider, "generate", fake_generate)
