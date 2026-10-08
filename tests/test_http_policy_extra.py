from unittest.mock import Mock, patch
import requests
from core.http_client import get_response


def test_http_404_is_not_retried():
    r = Mock()
    r.status_code = 404
    r.headers = {}
    r.raise_for_status.side_effect = requests.HTTPError('404')
    with patch('requests.Session.get', return_value=r) as get:
        try:
            get_response('https://example.com/missing', retries=3)
        except requests.HTTPError:
            pass
        assert get.call_count == 1


def test_http_403_is_not_retried():
    r = Mock()
    r.status_code = 403
    r.headers = {}
    r.raise_for_status.side_effect = requests.HTTPError('403')
    with patch('requests.Session.get', return_value=r) as get:
        try:
            get_response('https://example.com/forbidden', retries=3)
        except requests.HTTPError:
            pass
        assert get.call_count == 1


def test_http_500_can_retry():
    r1 = Mock(); r1.status_code = 500; r1.headers = {'Retry-After':'0'}
    r2 = Mock(); r2.status_code = 200; r2.headers = {}; r2.text='ok'
    with patch('requests.Session.get', side_effect=[r1,r2]) as get:
        result=get_response('https://example.com/transient', retries=1)
        assert result.status_code == 200
        assert get.call_count == 2
