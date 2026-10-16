"""Regression tests for funding-record persistence identity."""

import sqlite3
from datetime import UTC, datetime

from agents.funding_agent import (
    FundingDocument, FundingOpportunity, funding_exists, init_funding_db, process_documents,
)


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


def test_rescanning_same_source_url_refreshes_status_deadline_and_eligibility(tmp_path) -> None:
    source_url = "https://example.org/calls/live-grant"
    output_dir = tmp_path / "intermediate" / "funding"
    db_path = tmp_path / "funding_memory.db"
    document = FundingDocument(
        organization="Example Funder",
        title="Live Research Grant",
        source=source_url,
        source_type="web",
        content="Funding call eligibility and application details.",
        year=2026,
    )

    def opportunity(*, status: str, deadline: str, eligibility: str) -> FundingOpportunity:
        now = datetime.now(UTC).isoformat()
        return FundingOpportunity(
            funding_body="Example Funder",
            program_name="Live Research Grant",
            call_id="CALL-LIVE-1",
            status=status,
            opening_date="2026-01-01",
            deadline=deadline,
            funding_amount="$10,000",
            currency="USD",
            duration=None,
            eligibility=eligibility,
            geography="Global",
            trl=None,
            research_priorities=["DePIN"],
            required_partners=None,
            deliverables=None,
            evaluation_criteria=None,
            eligible_costs=None,
            application_url="https://example.org/calls/live-grant/apply",
            opportunity_type="FUNDING_CALL",
            focus_area="DePIN",
            keywords=["DePIN"],
            research_context=None,
            source_method="web",
            source_url=source_url,
            source_title="Live Research Grant",
            retrieved_at=now,
            published_at=None,
            last_verified_at=now,
            evidence={"status": status},
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    process_documents(
        [(document, opportunity(
            status="OPEN_CALL",
            deadline="2030-12-31",
            eligibility="Eligible applicants must be legal entities established in an eligible country.",
        ))],
        output_dir,
        db_path,
        run_id="first-scan",
    )
    process_documents(
        [(document, opportunity(
            status="CLOSED_CALL",
            deadline="2026-01-31",
            eligibility="Only registered research institutions may apply.",
        ))],
        output_dir,
        db_path,
        run_id="second-scan",
    )

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT status, deadline, eligibility FROM funding_data WHERE url = ?",
            (source_url,),
        ).fetchall()

    assert rows == [(
        "CLOSED_CALL",
        "2026-01-31",
        "Only registered research institutions may apply.",
    )]
