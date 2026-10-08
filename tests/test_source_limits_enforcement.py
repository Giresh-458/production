import pytest
from core.source_registry import extract_yaml_limits
from core.recursive_collection import RecursiveCollectionConfig
from core.web_collection import collect_source

def test_source_limits_extraction():
    yaml_data = [
        {
            "url": "https://example.com/one",
            "limits": {
                "max_pages": 5,
                "max_documents": 10,
                "max_depth": 2,
                "max_seconds": 30
            }
        },
        {
            "url": "https://example.com/two",
            "limits": {
                "max_pages": 3,
            }
        }
    ]
    
    limits_by_url = extract_yaml_limits(yaml_data)
    assert limits_by_url["https://example.com/one"]["max_pages"] == 5
    assert limits_by_url["https://example.com/one"]["max_documents"] == 10
    assert limits_by_url["https://example.com/one"]["max_seconds"] == 30
    assert limits_by_url["https://example.com/one"]["max_depth"] == 2
    
    assert limits_by_url["https://example.com/two"]["max_pages"] == 3
    assert "max_documents" not in limits_by_url["https://example.com/two"]

def test_recursive_config_receives_limits():
    config = RecursiveCollectionConfig(
        max_depth=2,
        max_pages=5,
        max_documents=10,
        max_seconds=30
    )
    assert config.max_depth == 2
    assert config.max_pages == 5
    assert config.max_documents == 10
    assert config.max_seconds == 30
