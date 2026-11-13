import requests

from core import robots_policy


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None, url="https://example.test/robots.txt"):
        self.status_code = status_code
        self.content = text.encode("utf-8")
        self.headers = headers or {}
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            response.headers.update(self.headers)
            raise requests.HTTPError(f"HTTP {self.status_code}", response=response)


def test_successful_robots_rules_are_enforced(monkeypatch):
    robots_policy.clear_robots_cache()
    monkeypatch.setattr(
        robots_policy,
        "get_response",
        lambda *args, **kwargs: FakeResponse(text="User-agent: RIF\nDisallow: /private\nAllow: /private/public"),
    )
    assert robots_policy.check_robots("https://example.test/private/file")[0] is False
    assert robots_policy.check_robots("https://example.test/private/public/info")[0] is True


def test_robots_404_is_unavailable_and_may_allow(monkeypatch):
    robots_policy.clear_robots_cache()

    def missing(*args, **kwargs):
        FakeResponse(404).raise_for_status()

    monkeypatch.setattr(robots_policy, "get_response", missing)
    allowed, reason, delay = robots_policy.check_robots("https://example.test/open")
    assert allowed is True
    assert reason is None
    assert delay == 0.0


def test_robots_429_is_fail_closed_and_respects_retry_after_cache(monkeypatch):
    robots_policy.clear_robots_cache()
    calls = []

    def rate_limited(*args, **kwargs):
        calls.append(args[0])
        FakeResponse(429, headers={"Retry-After": "120"}).raise_for_status()

    monkeypatch.setattr(robots_policy, "get_response", rate_limited)
    allowed, reason, _ = robots_policy.check_robots("https://example.test/open")
    assert allowed is False
    assert "429" in reason
    assert "Retry-After=120" in reason

    # The 429 response is cached for at least the server-requested wait.
    second_allowed, second_reason, _ = robots_policy.check_robots("https://example.test/other")
    assert second_allowed is False
    assert second_reason == reason
    assert len(calls) == 1


def test_robots_server_error_is_fail_closed(monkeypatch):
    robots_policy.clear_robots_cache()

    def unavailable(*args, **kwargs):
        FakeResponse(503).raise_for_status()

    monkeypatch.setattr(robots_policy, "get_response", unavailable)
    allowed, reason, _ = robots_policy.check_robots("https://example.test/open")
    assert allowed is False
    assert "503" in reason


def test_robots_network_error_is_fail_closed(monkeypatch):
    robots_policy.clear_robots_cache()
    monkeypatch.setattr(
        robots_policy,
        "get_response",
        lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("offline")),
    )
    allowed, reason, _ = robots_policy.check_robots("https://example.test/open")
    assert allowed is False
    assert "could not be fetched" in reason



def test_robots_policy_rejects_urls_with_credentials():
    allowed, reason, _ = robots_policy.check_robots("https://user:secret@example.test/private")
    assert allowed is False
    assert "credential-free" in reason



def test_rate_limit_cooldown_blocks_new_requests_until_deadline(monkeypatch):
    import time

    robots_policy.clear_robots_cache()
    url = "https://example.test/limited"
    robots_policy.defer_host(url, 120.0)
    assert robots_policy.wait_for_host_slot(
        url,
        deadline=time.time() + 0.1,
    ) is False
