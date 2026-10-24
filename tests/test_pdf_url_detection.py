from core.crawl4ai_web import _is_pdf_url


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
