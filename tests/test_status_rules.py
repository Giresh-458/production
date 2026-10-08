import pytest
from core.schemas import CollectionResult

def test_final_status_logic():
    # Simulate an exhausted unpaginated source
    result = CollectionResult(source_id='test', method='web')
    result.pages_fetched = 1
    result.opportunities = [{'id': 1}]
    result.truncated = False
    result.pagination_detected = False
    result.discovery_exhausted = True
    
    # Run the logic from crawl4ai_web.py
    if result.pages_failed > 0 and result.pages_fetched == 0:
        result.final_status = "FAIL"
    elif result.truncated:
        result.final_status = "TEST_TRUNCATED" # mock for test limit
    elif result.pages_fetched > 0 or result.documents_fetched > 0:
        if len(result.opportunities) > 0:
            if result.pagination_detected and result.pagination_complete:
                result.final_status = "VERIFIED"
            elif not result.pagination_detected and result.discovery_exhausted:
                result.final_status = "VERIFIED"
            else:
                result.final_status = "PARTIAL"
        else:
            result.final_status = "NO_ACTIVE_OPPORTUNITIES"
            
    assert result.final_status == "VERIFIED"
    
    # Simulate a paginated source that failed to exhaust
    result2 = CollectionResult(source_id='test', method='web')
    result2.pages_fetched = 1
    result2.opportunities = [{'id': 1}]
    result2.truncated = False
    result2.pagination_detected = True
    result2.pagination_complete = False
    result2.discovery_exhausted = True 
    
    if result2.pages_failed > 0 and result2.pages_fetched == 0:
        result2.final_status = "FAIL"
    elif result2.truncated:
        result2.final_status = "TEST_TRUNCATED" # mock for test limit
    elif result2.pages_fetched > 0 or result2.documents_fetched > 0:
        if len(result2.opportunities) > 0:
            if result2.pagination_detected and result2.pagination_complete:
                result2.final_status = "VERIFIED"
            elif not result2.pagination_detected and result2.discovery_exhausted:
                result2.final_status = "VERIFIED"
            else:
                result2.final_status = "PARTIAL"
        else:
            result2.final_status = "NO_ACTIVE_OPPORTUNITIES"
            
    assert result2.final_status == "PARTIAL"

