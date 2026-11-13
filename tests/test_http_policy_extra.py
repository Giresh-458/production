from unittest.mock import Mock, patch

import pytest
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

class _RedirectResponse:
    def __init__(self, status_code, *, headers=None, body=b"", url=""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.url = url
        self.closed = False
        self._content = body
        self._content_consumed = True

    @property
    def content(self):
        return self._body

    @property
    def text(self):
        return self._body.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size=65536):
        if self._body:
            yield self._body

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {self.status_code}",
                response=self,
            )


def test_get_response_checks_robots_on_each_redirect_hop(monkeypatch):
    from core.http_client import get_response

    requested = []
    responses = [
        _RedirectResponse(
            302,
            headers={"Location": "https://docs.example.test/final"},
            url="https://start.example.test/resource",
        ),
        _RedirectResponse(
            200,
            headers={"Content-Type": "text/plain"},
            body=b"verified",
            url="https://docs.example.test/final",
        ),
    ]

    class Session:
        def get(self, url, **kwargs):
            requested.append((url, kwargs.get("allow_redirects")))
            return responses.pop(0)

    robots_checks = []
    monkeypatch.setattr(
        "core.robots_policy.check_robots",
        lambda url: (robots_checks.append(url) or True, None, 0.0),
    )
    monkeypatch.setattr(
        "core.robots_policy.wait_for_host_slot",
        lambda *args, **kwargs: True,
    )

    response = get_response(
        "https://start.example.test/resource",
        session=Session(),
        retries=0,
    )

    assert response.status_code == 200
    assert response.text == "verified"
    assert requested == [
        ("https://start.example.test/resource", False),
        ("https://docs.example.test/final", False),
    ]
    assert robots_checks == ["https://docs.example.test/final"]


def test_get_response_rejects_robots_disallowed_redirect_before_request(monkeypatch):
    from core.http_client import get_response

    requested = []
    redirect = _RedirectResponse(
        302,
        headers={"Location": "https://private.example.test/data"},
        url="https://start.example.test/resource",
    )

    class Session:
        def get(self, url, **kwargs):
            requested.append(url)
            return redirect

    monkeypatch.setattr(
        "core.robots_policy.check_robots",
        lambda url: (False, "robots.txt disallows this URL", 0.0),
    )

    with pytest.raises(PermissionError, match="robots_disallow_redirect"):
        get_response(
            "https://start.example.test/resource",
            session=Session(),
            retries=0,
        )

    assert requested == ["https://start.example.test/resource"]
    assert redirect.closed is True


@pytest.mark.parametrize(
    "location",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/admin",
        "http://127.1/admin",
        "http://2130706433/admin",
        "http://0x7f000001/admin",
        "http://0177.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://user:password@external.example.test/private",
        "http://localhost/private",
    ],
)
def test_get_response_rejects_unsafe_redirect_targets(location):
    from core.http_client import get_response

    requested = []
    redirect = _RedirectResponse(
        302,
        headers={"Location": location},
        url="https://start.example.test/resource",
    )

    class Session:
        def get(self, url, **kwargs):
            requested.append(url)
            return redirect

    with pytest.raises(ValueError, match="redirect"):
        get_response(
            "https://start.example.test/resource",
            session=Session(),
            retries=0,
        )

    assert requested == ["https://start.example.test/resource"]
    assert redirect.closed is True


def test_get_response_allow_redirects_false_returns_redirect_without_following(monkeypatch):
    from core.http_client import get_response

    requested = []
    redirect = _RedirectResponse(
        302,
        headers={"Location": "https://other.example.test/"},
        url="https://start.example.test/resource",
    )

    class Session:
        def get(self, url, **kwargs):
            requested.append((url, kwargs.get("allow_redirects")))
            return redirect

    robots_checks = []
    monkeypatch.setattr(
        "core.robots_policy.check_robots",
        lambda url: robots_checks.append(url) or (True, None, 0.0),
    )

    response = get_response(
        "https://start.example.test/resource",
        session=Session(),
        retries=0,
        allow_redirects=False,
    )

    assert response.status_code == 302
    assert requested == [("https://start.example.test/resource", False)]
    assert robots_checks == []


def test_get_response_strips_sensitive_headers_on_cross_origin_redirect(monkeypatch):
    from core.http_client import get_response

    observed_headers = []
    responses = [
        _RedirectResponse(
            302,
            headers={"Location": "https://other.example.test/final"},
            url="https://start.example.test/resource",
        ),
        _RedirectResponse(
            200,
            body=b"ok",
            url="https://other.example.test/final",
        ),
    ]

    class Session:
        def get(self, url, **kwargs):
            observed_headers.append(dict(kwargs.get("headers") or {}))
            return responses.pop(0)

    monkeypatch.setattr(
        "core.robots_policy.check_robots",
        lambda url: (True, None, 0.0),
    )
    monkeypatch.setattr(
        "core.robots_policy.wait_for_host_slot",
        lambda *args, **kwargs: True,
    )

    get_response(
        "https://start.example.test/resource",
        session=Session(),
        headers={
            "Authorization": "Bearer secret",
            "Cookie": "session=secret",
            "X-API-Key": "api-secret",
            "X-Auth-Token": "token-secret",
            "X-Research": "rif",
        },
        retries=0,
    )

    assert observed_headers[0]["Authorization"] == "Bearer secret"
    assert observed_headers[1] == {"X-Research": "rif"}


def test_429_retry_defers_host_before_retry(monkeypatch):
    from core.http_client import get_response

    requested = []
    responses = [
        _RedirectResponse(
            429,
            headers={"Retry-After": "3"},
            url="https://rate.example.test/resource",
        ),
        _RedirectResponse(
            200,
            body=b"ok",
            url="https://rate.example.test/resource",
        ),
    ]

    class Session:
        def get(self, url, **kwargs):
            requested.append(url)
            return responses.pop(0)

    deferred = []
    monkeypatch.setattr(
        "core.http_client._defer_rate_limited_host",
        lambda url, response, attempt, delay_seconds=None: deferred.append(
            (url, attempt, delay_seconds)
        ),
    )
    monkeypatch.setattr(
        "core.http_client._retry_delay",
        lambda response, attempt: 0.0,
    )
    monkeypatch.setattr("core.http_client._sleep_or_abort", lambda delay: None)
    waited = []
    monkeypatch.setattr(
        "core.robots_policy.wait_for_host_slot",
        lambda url, **kwargs: waited.append(url) or True,
    )

    response = get_response(
        "https://rate.example.test/resource",
        session=Session(),
        retries=1,
    )

    assert response.status_code == 200
    assert requested == [
        "https://rate.example.test/resource",
        "https://rate.example.test/resource",
    ]
    assert deferred == [
        ("https://rate.example.test/resource", 0, 0.0),
    ]
    assert waited == ["https://rate.example.test/resource"]


def test_get_response_blocks_cross_origin_redirect_when_session_auth_is_configured():
    from core.http_client import get_response

    requested = []
    redirect = _RedirectResponse(
        302,
        headers={"Location": "https://other.example.test/final"},
        url="https://start.example.test/resource",
    )

    class AuthenticatedSession:
        auth = ("user", "password")

        def get(self, url, **kwargs):
            requested.append(url)
            return redirect

    with pytest.raises(PermissionError, match="authenticated_session"):
        get_response(
            "https://start.example.test/resource",
            session=AuthenticatedSession(),
            retries=0,
        )

    assert requested == ["https://start.example.test/resource"]

