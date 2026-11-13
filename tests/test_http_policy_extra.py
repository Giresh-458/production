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



def test_http_date_retry_after_beyond_wait_budget_is_not_ignored():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime
    from core.http_client import _retry_delay

    response = Mock()
    response.headers = {
        "Retry-After": format_datetime(datetime.now(UTC) + timedelta(seconds=600), usegmt=True)
    }
    try:
        _retry_delay(response, 0)
    except RuntimeError as exc:
        assert "too long to wait" in str(exc)
    else:
        raise AssertionError("An oversized HTTP-date Retry-After must not be replaced with backoff")
