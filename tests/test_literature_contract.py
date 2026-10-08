from core.collection_schemas import validate_agent_specific_body

def test_manual_literature_record_published_none():
    body = {
        "published": None,
        "authors": "John Doe",
        "score": 10.0,
        "relevance_score": 5.0,
        "impact_score": 5.0,
        "recency_score": 0.0,
        "problem_clarity_score": 0.0,
    }
    
    errors = validate_agent_specific_body("literature", body)
    assert not errors, f"Expected no validation errors, got: {errors}"
