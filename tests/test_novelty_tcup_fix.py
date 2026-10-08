import pytest
from core.novelty_search import perform_novelty_search
import core.novelty_search

def test_raw_metadata_rejected():
    problem = "Title: Tribal Colleges and Universities Program (TCUP) Agency: Number: 21-595 Details: { 'description': 'We fund tribal colleges.' }"
    res = perform_novelty_search(problem, use_external=True)
    assert res["query_executed"] is False
    assert res["novelty_status"] == "NO_RESULT"

def test_insufficient_problem_rejected():
    res = perform_novelty_search("Insufficient problem signal", use_external=True)
    assert res["query_executed"] is False
    assert res["novelty_status"] == "NO_RESULT"
    
def test_short_problem_rejected():
    res = perform_novelty_search("the tribal problem", use_external=True)
    assert res["query_executed"] is False
    assert res["novelty_status"] == "NO_RESULT"

def test_generic_title_overlap(monkeypatch):
    problem = "quantum encryption architecture scalability for enterprise networks"
    def mock_fetch(*args, **kwargs):
        return {"results": [{"title": "Quantum system architecture evaluation program", "id": "url", "authorships": []}]}, None
    import core.http_client
    monkeypatch.setattr(core.http_client, "fetch_json", mock_fetch)
    
    res = perform_novelty_search(problem, use_external=True)
    assert res["query_executed"] is True
    assert res["novelty_status"] == "NO_OVERLAP_IN_SEARCH"

def test_substantive_overlap(monkeypatch):
    problem = "quantum encryption architecture scalability for enterprise networks"
    def mock_fetch(*args, **kwargs):
        return {"results": [{"title": "Scalability of quantum encryption architecture in enterprise networks", "id": "url", "authorships": []}]}, None
    import core.http_client
    monkeypatch.setattr(core.http_client, "fetch_json", mock_fetch)
    
    res = perform_novelty_search(problem, use_external=True)
    assert res["query_executed"] is True
    assert res["novelty_status"] == "OVERLAP_FOUND"
    assert res["strongest_overlap"] == "Scalability of quantum encryption architecture in enterprise networks"

def test_valid_problem_no_overlap(monkeypatch):
    problem = "quantum encryption architecture scalability for enterprise networks"
    def mock_fetch(*args, **kwargs):
        return {"results": [{"title": "Biology cell division mapping", "id": "url", "authorships": []}]}, None
    import core.http_client
    monkeypatch.setattr(core.http_client, "fetch_json", mock_fetch)
    
    res = perform_novelty_search(problem, use_external=True)
    assert res["query_executed"] is True
    assert res["novelty_status"] == "NO_OVERLAP_IN_SEARCH"
