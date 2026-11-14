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


def test_lab_configured_collection_enforces_source_page_budget_and_telemetry(tmp_path, monkeypatch):
    import sqlite3

    from agents import lab_agent
    from core.crawl_context import CrawlContext, current_crawl_context

    sources_path = tmp_path / "labs_sources.yaml"
    sources_path.write_text(
        "labs:\n"
        "  - name: Test Lab\n"
        "    url: https://example.org/\n"
        "    focus_pages: [/research, /projects, /publications]\n"
        "    limits:\n"
        "      max_pages: 1\n"
        "      max_depth: 1\n"
        "      max_seconds: 30\n",
        encoding="utf-8",
    )
    source = lab_agent.LabSource(
        name="Test Lab",
        url="https://example.org/",
        focus_pages=["/research", "/projects", "/publications"],
    )
    monkeypatch.setattr(lab_agent, "load_sources", lambda _path: [source])
    fetched = []
    monkeypatch.setattr(
        lab_agent,
        "fetch_html",
        lambda url: fetched.append(url) or "<html><body>research evidence</body></html>",
    )
    monkeypatch.setattr(
        lab_agent,
        "extract_page_text",
        lambda _html: ("Research", "The laboratory publishes cryptography research evidence."),
    )

    context = CrawlContext()
    token = current_crawl_context.set(context)
    warnings = []
    try:
        documents = lab_agent.build_auto_documents(
            sources_path,
            tmp_path / "labs.db",
            warnings=warnings,
        )
    finally:
        current_crawl_context.reset(token)

    assert len(documents) == 1
    assert fetched == ["https://example.org/research"]
    assert context.pages_fetched == 1
    assert context.source_limits_telemetry == [
        {
            "source_name": "Test Lab",
            "url": "https://example.org/",
            "source_limit_configured": True,
            "source_limit_matched": True,
            "max_pages": 1,
            "max_depth": 1,
            "max_seconds": 30,
            "global_fallback_used": False,
        }
    ]
    assert any("source_max_pages" in warning for warning in warnings)


def test_lab_collection_force_refresh_bypasses_permanent_page_exists_skip(monkeypatch):
    import sqlite3

    from agents import lab_agent

    connection = sqlite3.connect(":memory:")
    lab_agent.init_labs_db(connection)
    existing_url = "https://example.org/research"
    connection.execute(
        "INSERT INTO lab_pages (lab_name, page_url, page_title, path, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("Test Lab", existing_url, "Old title", "old.md", "2026-01-01T00:00:00+00:00"),
    )
    connection.commit()

    source = lab_agent.LabSource(
        name="Test Lab",
        url="https://example.org/",
        focus_pages=["/research"],
    )
    fetched = []
    monkeypatch.setattr(
        lab_agent,
        "fetch_html",
        lambda url: fetched.append(url) or "<html><body>new research</body></html>",
    )
    monkeypatch.setattr(
        lab_agent,
        "extract_page_text",
        lambda _html: ("Updated research", "Newly retrieved laboratory research evidence."),
    )

    assert lab_agent.collect_lab_document(source, connection) is None
    assert fetched == []

    refreshed = lab_agent.collect_lab_document(
        source,
        connection,
        source_limits={"max_pages": 1, "max_seconds": 30},
        force_refresh=True,
    )
    assert refreshed is not None
    assert refreshed.pages[0].title == "Updated research"
    assert fetched == [existing_url]
