import pytest
from core.web_collection import _is_soft_404

def test_valid_chainlink_style_page_not_soft_404():
    valid_html = '''<html><head><title>Chainlink Grant Program | Chainlink</title></head><body><h1>Chainlink Grant Program</h1><p>We are looking for developers.</p><p>''' + ('text ' * 100) + '''</p></body></html>'''
    assert not _is_soft_404(valid_html, "Chainlink Grant Program | Chainlink", "Chainlink Grant Program We are looking for developers. " + "text "*100)

def test_genuine_soft_404_is_rejected():
    invalid_html = '''<html><head><title>Page not found | Chainlink</title></head><body><p>We can't seem to find the page you're looking for.</p></body></html>'''
    assert _is_soft_404(invalid_html, "Page not found | Chainlink", "We can't seem to find the page you're looking for.")

def test_canonical_soft_404_rejected():
    invalid_html = '''<html><head><title>Some Title</title><link rel="canonical" href="https://chain.link/404" /></head><body><p>Not found content.</p></body></html>'''
    assert _is_soft_404(invalid_html, "Some Title", "Not found content.")
