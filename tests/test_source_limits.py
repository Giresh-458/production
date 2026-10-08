import yaml
from core.source_registry import extract_yaml_limits

def test_source_config_parser():
    yaml_fixture = """
    test_sources:
      category_a:
        - name: source-one
          url: https://example.com/a
          limits:
            max_pages: 17
            max_documents: 5
            max_depth: 3
            max_seconds: 42
    """
    data = yaml.safe_load(yaml_fixture)
    limits_by_url = extract_yaml_limits(data)
    
    assert "https://example.com/a" in limits_by_url
    limits = limits_by_url["https://example.com/a"]
    assert limits["max_pages"] == 17
    assert limits["max_documents"] == 5
    assert limits["max_depth"] == 3
    assert limits["max_seconds"] == 42
    assert limits["_source_name"] == "source-one"
