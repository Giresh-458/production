import pytest
import core.structured_sources as ss

def test_filecoin_github_semantics(monkeypatch):
    issues = [
        {"title": "Open Grant Proposal: Web3 App", "state": "open", "labels": [{"name": "Open Grant"}], "html_url": "url1"},
        {"title": "Open Grant Proposal: Rejected", "state": "closed", "labels": [], "html_url": "url2"},
        {"title": "RFP Application: Explorer", "state": "closed", "labels": [{"name": "Approved"}], "html_url": "url3"},
        {"title": "How to apply?", "state": "open", "labels": [{"name": "question"}], "html_url": "url4"},
    ]
    
    def fake_get_response(url, **kwargs):
        class R:
            def json(self): return issues
        return R()
        
    monkeypatch.setattr("core.structured_sources.fetch_json", lambda *a, **k: ({"full_name": "filecoin/devgrants"}, None))
    monkeypatch.setattr("core.http_client.get_response", fake_get_response)
    
    res = ss._github("https://github.com/filecoin-project/devgrants")
    text = res.text
    assert "Open Grant Request: Open Grant Proposal: Web3 App | url1" in text
    assert "Closed/Rejected Request: Open Grant Proposal: Rejected | url2" in text
    assert "Approved Grant: RFP Application: Explorer | url3" in text
    assert "Informational Issue: How to apply? | url4" in text
