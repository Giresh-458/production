import urllib.request
import urllib.error
import urllib.parse
from core.novelty_search import OpenAlexSearcher
from unittest.mock import patch

def test_openalex_failure_modes():
    searcher = OpenAlexSearcher()
    
    # 2. Timeout -> unavailable, UNKNOWN
    def mock_timeout(*args, **kwargs):
        import requests
        raise requests.exceptions.Timeout("timeout")

    with patch('core.http_client.fetch_json', side_effect=mock_timeout):
        res_timeout = searcher.search("zero knowledge cryptography authentication protocol")
        assert res_timeout["search_mode"] == "unavailable"
        assert res_timeout["novelty_status"] == "UNKNOWN"

    # 3. HTTP Error -> unavailable, UNKNOWN
    def mock_http_error(*args, **kwargs):
        import requests
        resp = requests.Response()
        resp.status_code = 500
        raise requests.exceptions.HTTPError("Internal Error", response=resp)

    with patch('core.http_client.fetch_json', side_effect=mock_http_error):
        res_http = searcher.search("zero knowledge cryptography authentication protocol")
        assert res_http["search_mode"] == "unavailable"
        assert res_http["novelty_status"] == "UNKNOWN"

    # 4. Malformed Response -> unavailable, UNKNOWN
    def mock_malformed(*args, **kwargs):
        raise ValueError("Invalid JSON returned")

    with patch('core.http_client.fetch_json', side_effect=mock_malformed):
        res_malformed = searcher.search("zero knowledge cryptography authentication protocol")
        assert res_malformed["search_mode"] == "unavailable"
        assert res_malformed["novelty_status"] == "UNKNOWN"

