"""robots.txt fetch: one bounded retry on connection faults; still fail closed after."""
import pytest
import requests

from core import robots_policy


class _Resp:
    status_code = 200
    content = b"User-agent: *\nAllow: /\n"
    headers = {}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    robots_policy.clear_robots_cache()
    monkeypatch.setattr(robots_policy, "wait_for_host_slot", lambda *a, **k: True)
    monkeypatch.setattr(robots_policy.time, "sleep", lambda s: None)
    yield
    robots_policy.clear_robots_cache()


def test_transient_dns_failure_is_retried_once_then_policy_is_enforced(monkeypatch):
    calls = []

    def fake_get_response(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            # Verbatim class of failure observed live for www.sbir.gov.
            raise requests.ConnectionError("Failed to resolve 'www.sbir.gov' ([Errno 11002] getaddrinfo failed)")
        return _Resp()

    monkeypatch.setattr(robots_policy, "get_response", fake_get_response)
    allowed, reason, _ = robots_policy.check_robots("https://www.sbir.gov/topics?status=Open", cache={})
    assert allowed is True and reason is None
    assert calls == ["https://www.sbir.gov/robots.txt"] * 2


def test_persistent_network_failure_still_fails_closed(monkeypatch):
    calls = []

    def fake_get_response(url, **kwargs):
        calls.append(url)
        raise requests.ConnectionError("getaddrinfo failed")

    monkeypatch.setattr(robots_policy, "get_response", fake_get_response)
    allowed, reason, _ = robots_policy.check_robots("https://www.sbir.gov/topics", cache={})
    assert allowed is False
    assert "could not be fetched" in reason
    assert len(calls) == 2  # bounded: exactly one retry


def test_http_errors_are_not_retried(monkeypatch):
    calls = []

    class _ServerError(_Resp):
        status_code = 503

    def fake_get_response(url, **kwargs):
        calls.append(url)
        return _ServerError()

    monkeypatch.setattr(robots_policy, "get_response", fake_get_response)
    allowed, reason, _ = robots_policy.check_robots("https://www.sbir.gov/topics", cache={})
    assert allowed is False and "HTTP 503" in reason
    assert len(calls) == 1
