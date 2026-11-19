from __future__ import annotations

import pytest

from agents import company_agent, hackathon_agent, opensource_agent, regulation_agent


EVIDENCE_TEXT = (
    "The production system has a measurable zero-knowledge verification bottleneck. "
    "Verification latency increases under high transaction volume, reducing throughput. "
    "The source describes the architecture, operational constraints, observed failures, "
    "and the need for scalable proof verification and reliable settlement."
)


@pytest.mark.parametrize(
    ("module", "builder", "args"),
    [
        (
            company_agent,
            company_agent.build_manual_url_document,
            ("https://example.org/company", "Example Company", "Enterprise adopter", "RWA", "settlement"),
        ),
        (
            regulation_agent,
            regulation_agent.build_manual_url_document,
            (
                "https://example.org/regulation",
                "Example Regulation",
                "Example Authority",
                "Regulation",
                "Guidance",
                "RWA",
                "compliance",
            ),
        ),
        (
            opensource_agent,
            opensource_agent.build_manual_url_document,
            (
                "https://example.org/project",
                "Example Project",
                "Documentation",
                "Technical Issue",
                "RWA",
                "verification",
            ),
        ),
        (
            hackathon_agent,
            hackathon_agent.build_manual_url_document,
            (
                "https://example.org/challenge",
                "Example Challenge",
                "Hackathon",
                "Problem Statement",
                "Example Organizer",
                "RWA",
                "verification",
            ),
        ),
    ],
)
def test_recursive_manual_url_builders_use_default_limits_without_yaml(
    monkeypatch, module, builder, args
):
    """Manual URL collection must not reference configured-scan-only limits."""
    captured = {}

    def fake_collect_recursive_pages(**kwargs):
        captured["config"] = kwargs["config"]
        return [object()]

    monkeypatch.setattr(module, "fetch_html", lambda _url: "<html><body>evidence</body></html>")
    monkeypatch.setattr(module, "collect_recursive_pages", fake_collect_recursive_pages)
    monkeypatch.setattr(
        module,
        "combine_recursive_pages",
        lambda _pages, **_kwargs: ("Specific technical source", EVIDENCE_TEXT),
    )
    monkeypatch.setattr(module, "is_relevant", lambda *_args, **_kwargs: True)

    document = builder(*args)

    assert document.url == args[0]
    assert EVIDENCE_TEXT in document.content
    assert captured["config"].max_depth == module.RECURSIVE_CONFIG.max_depth
    assert captured["config"].max_pages == module.RECURSIVE_CONFIG.max_pages
