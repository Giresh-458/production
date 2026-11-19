import importlib

import pytest

from core.shared_crawl_manager import CrawlResult, SharedCrawlManager


AGENTS = [
    ("lab", "LabAgentError"),
    ("expert", "ExpertAgentError"),
    ("failure", "FailureAgentError"),
    ("company", "CompanyAgentError"),
    ("hackathon", "HackathonAgentError"),
    ("investment", "InvestmentAgentError"),
    ("regulation", "RegulationAgentError"),
    ("opensource", "OpenSourceAgentError"),
    ("practitioner", "PractitionerAgentError"),
    ("data_availability", "DataAvailabilityAgentError"),
]


@pytest.mark.parametrize("agent_name,error_name", AGENTS)
@pytest.mark.parametrize(
    "status_code,error,retry_after",
    [
        (0, "robots.txt disallows this URL", None),
        (429, "http_status_429: rate limited", "120"),
    ],
)
def test_agent_fetch_html_propagates_shared_crawler_failures(
    monkeypatch, agent_name, error_name, status_code, error, retry_after
):
    module = importlib.import_module(f"agents.{agent_name}_agent")
    expected_error = getattr(module, error_name)
    url = "https://example.test/private"

    class FakeManager:
        def fetch(self, requested_url):
            assert requested_url == url
            return CrawlResult(
                canonical_url=url,
                final_url=url,
                status_code=status_code,
                content_type="",
                title="",
                html="",
                clean_text="",
                content_hash="",
                fetch_method="http",
                from_cache=False,
                error=error,
                retry_after=retry_after,
            )

    monkeypatch.setattr(
        SharedCrawlManager,
        "get",
        classmethod(lambda cls: FakeManager()),
    )

    with pytest.raises(expected_error, match="robots|429"):
        module.fetch_html(url)
