from __future__ import annotations

import core.llm_provider as llm_provider

# tests/conftest.py has an autouse fixture that replaces llm_provider.generate
# for the rest of the suite. Keep the real function before that fixture runs so
# these provider-unit tests exercise the implementation instead of the global mock.
_REAL_GENERATE = llm_provider.generate


def test_qwen3_generation_disables_thinking(monkeypatch):
    captured: dict = {}

    monkeypatch.setattr(
        llm_provider,
        "_settings",
        lambda: {
            "model": "qwen3:14b",
            "endpoint": "http://ollama:11434/api/generate",
            "timeout": 30,
            "retries": 1,
        },
    )

    def fake_send_request(payload, endpoint, timeout):
        captured["payload"] = payload
        captured["endpoint"] = endpoint
        captured["timeout"] = timeout
        return {"response": "RIF_OK"}

    monkeypatch.setattr(llm_provider, "_send_request", fake_send_request)

    result = _REAL_GENERATE(" Say hello ")

    assert result == "RIF_OK"
    assert captured["payload"]["model"] == "qwen3:14b"
    assert captured["payload"]["prompt"] == "Say hello"
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["think"] is False
    assert captured["endpoint"] == "http://ollama:11434/api/generate"


def test_non_qwen3_generation_keeps_existing_payload(monkeypatch):
    captured: dict = {}

    monkeypatch.setattr(
        llm_provider,
        "_settings",
        lambda: {
            "model": "llama3.1:8b",
            "endpoint": "http://ollama:11434/api/generate",
            "timeout": 30,
            "retries": 1,
        },
    )

    def fake_send_request(payload, endpoint, timeout):
        captured["payload"] = payload
        return {"response": "RIF_OK"}

    monkeypatch.setattr(llm_provider, "_send_request", fake_send_request)

    result = _REAL_GENERATE("Say hello")

    assert result == "RIF_OK"
    assert "think" not in captured["payload"]
