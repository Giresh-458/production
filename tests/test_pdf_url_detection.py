from core.crawl4ai_web import _is_climate_kic_download_url, _is_pdf_url


def test_pdf_detection_accepts_standard_pdf_paths():
    assert _is_pdf_url("https://example.org/files/call-guidelines.pdf")
    assert _is_pdf_url("https://example.org/files/call-guidelines.PDF?download=1")


def test_pdf_detection_accepts_explicit_netsuite_download_marker():
    url = (
        "https://6740820.app.netsuite.com/core/media/media.nl?"
        "id=711659&c=6740820&h=example&_xt=.pdf"
    )
    assert _is_pdf_url(url)


def test_pdf_detection_does_not_treat_arbitrary_query_urls_as_pdfs():
    assert not _is_pdf_url("https://example.org/media.nl?id=711659")
    assert not _is_pdf_url("https://example.org/call?format=pdf")

def test_robots_audit_distinguishes_denial_from_unverified_policy():
    from core.crawl4ai_web import _robots_gap_reason

    assert _robots_gap_reason("robots.txt disallows this URL") == "robots_policy_disallowed"
    assert _robots_gap_reason(
        "could not verify robots.txt policy (RuntimeError: robots.txt returned HTTP 429)"
    ) == "robots_policy_unverified"



def test_climate_kic_extensionless_download_routes_are_documents():
    assert _is_climate_kic_download_url("https://www.climate-kic.org/download/76438/")
    assert _is_climate_kic_download_url(
        "https://www.climate-kic.org/download/65405/?tmstv=1792753854"
    )
    assert not _is_climate_kic_download_url("https://www.climate-kic.org/open-call/example/")
    assert not _is_climate_kic_download_url("https://example.org/download/76438/")
