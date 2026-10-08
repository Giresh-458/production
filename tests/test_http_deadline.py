import pytest
import time
import requests
from unittest.mock import Mock
from core.http_client import get_response, _sleep_or_abort
from core.crawl_context import CrawlContext, current_crawl_context
from requests.models import Response

def test_deadline_expires_during_retry(monkeypatch):
    ctx = CrawlContext(deadline=time.time() + 0.1)
    token = current_crawl_context.set(ctx)
    try:
        mock_session = Mock()
        mock_resp = Response()
        mock_resp.status_code = 429
        mock_resp.headers = {"Retry-After": "1"}
        mock_session.get.return_value = mock_resp
        
        with pytest.raises(TimeoutError, match="Deadline exceeded during retry sleep"):
            get_response("http://example.com", session=mock_session)
    finally:
        current_crawl_context.reset(token)

def test_deadline_expires_during_response_reading(monkeypatch):
    ctx = CrawlContext(deadline=time.time() + 0.1)
    token = current_crawl_context.set(ctx)
    try:
        mock_session = Mock()
        mock_resp = Response()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Length": "1000"}
        
        def slow_iter_content(*args, **kwargs):
            yield b"chunk1"
            time.sleep(0.15)
            yield b"chunk2"
        
        mock_resp.iter_content = slow_iter_content
        mock_resp.close = Mock()
        mock_session.get.return_value = mock_resp
        
        with pytest.raises(TimeoutError):
            get_response("http://example.com", session=mock_session)
    finally:
        current_crawl_context.reset(token)
        
def test_effective_timeout_scaling():
    ctx = CrawlContext(deadline=time.time() + 1.0)
    token = current_crawl_context.set(ctx)
    try:
        from core.http_client import _apply_context_timeout
        eff = _apply_context_timeout((8.0, 20.0))
        
        assert isinstance(eff, tuple)
        assert eff[0] <= 1.0
        assert eff[1] <= 1.0
    finally:
        current_crawl_context.reset(token)
