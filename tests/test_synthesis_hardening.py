from __future__ import annotations

from agents import synthesis_agent as sa


def assessment(*, independent=2, layers=2, positive=0, negative=2):
    return {
        "independent_evidence_units": independent,
        "research_evidence_layers": layers,
        "positive_signal_count": positive,
        "negative_signal_count": negative,
    }


def evidence():
    return [
        {"excerpt": "The method fails under high load and remains a bottleneck."},
        {"excerpt": "The limitation is unresolved in current evaluations."},
    ]


def test_clear_gap_is_decided_without_llm(monkeypatch):
    def fail_llm(_prompt):
        raise AssertionError("LLM should not be called for a clear case")

    monkeypatch.setattr(sa, "llm_generate", fail_llm)
    result = sa._synthesis_decision(assessment(), problem="verification scalability", evidence_inputs=evidence())
    assert result["decision"] == "provisional_gap_supported"
    assert result["llm_review"]["used"] is False


def test_ambiguous_gap_uses_llm(monkeypatch):
    calls = []

    def fake_llm(prompt):
        calls.append(prompt)
        return '{"gap_supported": true, "confidence": 0.88, "status": "supported", "reason": "Independent evidence supports the limitation."}'

    monkeypatch.setattr(sa, "llm_generate", fake_llm)
    result = sa._synthesis_decision(
        assessment(independent=1, layers=1, positive=1, negative=1),
        problem="verification scalability",
        evidence_inputs=evidence(),
    )
    assert result["decision"] == "provisional_gap_supported"
    assert result["llm_review"]["used"] is True
    assert calls and "verification scalability" in calls[0]


def test_ambiguous_gap_llm_rejection_blocks_gap(monkeypatch):
    monkeypatch.setattr(
        sa,
        "llm_generate",
        lambda prompt: '{"gap_supported": false, "confidence": 0.91, "status": "contested", "reason": "Existing solutions address the limitation."}',
    )
    result = sa._synthesis_decision(
        assessment(independent=1, layers=1, positive=2, negative=1),
        problem="verification scalability",
        evidence_inputs=evidence(),
    )
    assert result["decision"] == "contested_gap"
    assert result["gap_supported"] is False


def test_llm_unavailable_marks_ambiguity_uncertain(monkeypatch):
    def unavailable(_prompt):
        raise RuntimeError("Ollama unavailable")

    monkeypatch.setattr(sa, "llm_generate", unavailable)
    result = sa._synthesis_decision(
        assessment(independent=1, layers=1, positive=1, negative=1),
        problem="verification scalability",
        evidence_inputs=evidence(),
    )
    assert result["decision"] == "uncertain"
    assert result["gap_supported"] is False
    assert result["llm_review"]["available"] is False
