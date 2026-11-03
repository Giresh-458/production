from __future__ import annotations

from agents import regulation_agent
from core.recursive_collection import _score_link, source_focus_link_keywords


def test_source_focus_keywords_keep_mica_crawl_off_unrelated_corporate_reporting():
    mica_keywords = source_focus_link_keywords(
        "EU MiCA",
        "crypto-assets, stablecoins, CASPs",
    )
    csrd_keywords = source_focus_link_keywords(
        "CSRD",
        "sustainability disclosure, auditability, ESG reporting",
    )

    unrelated_url = (
        "https://finance.ec.europa.eu/company-reporting-and-auditing/"
        "company-reporting/corporate-sustainability-reporting_en"
    )
    related_url = "https://finance.ec.europa.eu/digital-finance/stablecoins"

    assert _score_link(unrelated_url, "Corporate sustainability reporting", mica_keywords) < 2
    assert _score_link(related_url, "Stablecoins", mica_keywords) >= 2
    assert _score_link(unrelated_url, "Corporate sustainability reporting", csrd_keywords) >= 2


def test_regulation_configured_scan_uses_the_source_focus_for_recursive_links(monkeypatch, tmp_path):
    source = regulation_agent.RegulationSource(
        name="EU MiCA",
        url="https://finance.ec.europa.eu/digital-finance/crypto-assets_en",
        focus="crypto-assets, stablecoins, CASPs",
        source_type="Regulation",
        evidence_type="Official Regulation",
    )
    captured = {}

    monkeypatch.setattr(regulation_agent, "load_sources", lambda _path: [source])
    monkeypatch.setattr(regulation_agent, "collect_structured_source", lambda *_args: None)
    monkeypatch.setattr(regulation_agent, "fetch_html", lambda _url: "<html><body>MiCA</body></html>")

    def fake_collect_recursive_pages_with_review(**kwargs):
        captured["config"] = kwargs["config"]
        return [object()], {"review_id": "review-eu-mica"}

    monkeypatch.setattr(
        regulation_agent,
        "collect_recursive_pages_with_review",
        fake_collect_recursive_pages_with_review,
    )
    monkeypatch.setattr(regulation_agent, "save_recursive_review", lambda _review: None)
    monkeypatch.setattr(
        regulation_agent,
        "combine_recursive_pages",
        lambda _pages, **_kwargs: ("EU MiCA", "Crypto-assets and stablecoins regulation for CASPs."),
    )
    monkeypatch.setattr(regulation_agent, "is_relevant", lambda *_args: True)
    monkeypatch.setattr(regulation_agent, "update_recursive_review", lambda *_args, **_kwargs: None)

    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("{}\n", encoding="utf-8")
    documents = regulation_agent.build_auto_documents(sources_path)

    assert len(documents) == 1
    keywords = captured["config"].link_keywords
    assert "crypto-assets" in keywords
    assert "stablecoins" in keywords
    assert "casps" in keywords
    assert "reporting" not in keywords
