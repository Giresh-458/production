from pathlib import Path

from core.schemas import resolve_research_area
from core.normalization import normalize_intermediate_artifact
from core.http_client import canonicalize_url


def test_authoritative_routed_area_cannot_be_contaminated_by_keywords():
    resolved, warnings, _ = resolve_research_area(
        explicit_area="DigitalHealthCPS",
        title="Healthcare CPS",
        raw_content="carbon carbon carbon stablecoin zero-knowledge token ESG emissions",
        authoritative_explicit=True,
    )
    assert resolved == "DigitalHealthCPS"
    assert any("retained authoritative routed research area" in w for w in warnings)


def test_non_authoritative_schema_inference_can_still_reclassify():
    resolved, _, _ = resolve_research_area(
        explicit_area="RWA",
        title="Stablecoin depeg and reserve risk",
        raw_content="Stablecoin reserve depeg payment rail settlement vault stablecoin reserve monitoring.",
    )
    assert resolved == "Stablecoins"


def test_legacy_literature_h1_preserves_area():
    path = Path("/tmp/rif_literature_gap.md")
    path.write_text(
        "# Research Gap: DigitalHealthCPS\n\n"
        "## Problem\nHealthcare CPS problem.\n\n"
        "## Gap\nRemaining evaluation gap.\n",
        encoding="utf-8",
    )
    record = normalize_intermediate_artifact(path)
    assert record["research_area"] == "DigitalHealthCPS"


def test_broken_duplicate_source_urls_are_absent_from_registry_files():
    root = Path(__file__).resolve().parents[1]
    for rel in ("sources/company_sources.yaml", "sources/opensource_sources.yaml", "sources/source_registry.json"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "en-us.htmlen-us.html" not in text
        assert "github.com/ros2/ros2/ros2" not in text
        assert "itrust.sutd.edu.sg/itrust-labs_datasets" not in text


def test_url_canonicalization_drops_tracking_only():
    assert canonicalize_url("HTTPS://Example.COM/path?utm_source=x&a=1") == "https://example.com/path?a=1"
