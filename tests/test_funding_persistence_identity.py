"""Regression tests for funding-record persistence identity."""

import sqlite3

from agents.funding_agent import funding_exists, init_funding_db


def _insert_funding_row(connection: sqlite3.Connection, *, title: str, url: str) -> None:
    connection.execute(
        """
        INSERT INTO funding_data (
            organization, title, year, problem, keywords, url, focus_area,
            requirements, problem_signature, source_type, path, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "Example Funder", title, 2026, "Research problem", "[]", url,
            "General", "", "", "Web", "record.md", "2026-10-14T00:00:00+00:00",
        ),
    )
    connection.commit()


def test_same_title_with_distinct_source_urls_is_not_a_duplicate() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        init_funding_db(connection)
        _insert_funding_row(
            connection,
            title="Research Grants",
            url="https://example.org/calls/round-one",
        )

        assert not funding_exists(
            connection,
            "Research Grants",
            "https://example.org/calls/round-two",
        )
    finally:
        connection.close()


def test_same_source_url_is_a_duplicate_even_if_title_changes() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        init_funding_db(connection)
        _insert_funding_row(
            connection,
            title="Old title",
            url="https://example.org/calls/round-one",
        )

        assert funding_exists(
            connection,
            "Updated title",
            "https://example.org/calls/round-one",
        )
    finally:
        connection.close()
