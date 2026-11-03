from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


def test_regulation_source_config_loads_explicit_esg_area(monkeypatch, tmp_path):
    sources_path = tmp_path / "regulation_sources.yaml"
    sources_path.write_text(
        "regulation_sources:\n"
        "  eu:\n"
        "    - name: CSRD\n"
        "      url: https://finance.example.org/csrd\n"
        "      area: ESG\n"
        "      focus: sustainability disclosure, auditability, ESG reporting\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(regulation_agent, "apply_refresh_policy", lambda _agent, sources: sources)

    sources = regulation_agent.load_sources(sources_path)

    assert len(sources) == 1
    assert sources[0].name == "CSRD"
    assert sources[0].area == "ESG"


def test_explicit_regulation_source_area_beats_incidental_asset_words(monkeypatch, tmp_path):
    source = regulation_agent.RegulationSource(
        name="CSRD",
        url="https://finance.example.org/csrd",
        focus="sustainability disclosure, auditability, ESG reporting",
        source_type="Regulation",
        evidence_type="Official Regulation",
        area="ESG",
    )
    captured = {}
    monkeypatch.setattr(regulation_agent, "load_sources", lambda _path: [source])
    monkeypatch.setattr(
        regulation_agent,
        "collect_structured_source",
        lambda *_args: SimpleNamespace(
            url=source.url,
            title="Corporate sustainability reporting",
            text=(
                "The standard requires sustainability disclosures, emissions "
                "assurance, and reporting about tokenized assets."
            ),
        ),
    )

    def fake_is_relevant(_text, area_hint):
        captured["area_hint"] = area_hint
        return True

    monkeypatch.setattr(regulation_agent, "is_relevant", fake_is_relevant)
    sources_path = tmp_path / "sources.yaml"
    sources_path.write_text("{}\n", encoding="utf-8")

    documents = regulation_agent.build_auto_documents(sources_path)

    assert len(documents) == 1
    assert captured["area_hint"] == "ESG"
    assert documents[0].area_hint == "ESG"
