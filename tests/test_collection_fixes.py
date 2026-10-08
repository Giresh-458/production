import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from agents.db import db_connect, init_papers_db, upsert_paper_record
from agents.funding_agent import determine_status
from agents.opensource_agent import classify_github_item
from agents import literature_agent
from core.intelligence import compute_confidence
from core.semantic_retrieval import semantic_scores


class CollectionFixTests(unittest.TestCase):
    def test_literature_title_is_not_unique(self):
        db_path = Path(tempfile.mkdtemp()) / "papers.db"
        connection = db_connect(db_path)
        init_papers_db(connection)
        upsert_paper_record(connection, title="Same title", authors="A", year=2025,
                            research_area="RWA", path="a", url="https://doi.org/10/a", doi="10/a",
                            summary="x", static_tags=[], keyword_tags=[], concept_tags=[])
        upsert_paper_record(connection, title="Same title", authors="B", year=2026,
                            research_area="RWA", path="b", url="https://doi.org/10/b", doi="10/b",
                            summary="y", static_tags=[], keyword_tags=[], concept_tags=[])
        rows = connection.execute("SELECT title, doi FROM papers WHERE title='Same title'").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["doi"] for row in rows}, {"10/a", "10/b"})
        connection.close()

    def test_semantic_retrieval_prefers_relevant_document(self):
        scores = semantic_scores("zero knowledge verification scalability", [
            "a recipe for cooking rice",
            "zero knowledge verification at scale",
        ])
        self.assertGreater(scores[1], scores[0])

    def test_funding_status_is_normalized(self):
        self.assertEqual(determine_status(None, "2026-01-01", "2099-12-31"), "OPEN_CALL")
        self.assertEqual(determine_status(None, "2099-01-01", "2099-02-01"), "UPCOMING_CALL")
        self.assertEqual(determine_status(None, "2020-01-01", "2020-02-01"), "CLOSED_CALL")
        self.assertEqual(determine_status("funded", None, None), "FUNDED_PROJECT")

    def test_github_signal_classification(self):
        self.assertEqual(classify_github_item("verification too slow", "latency becomes impractical at scale"), "performance_limitation")
        self.assertEqual(classify_github_item("security vulnerability", "exploit allows attack"), "security_problem")
        self.assertEqual(classify_github_item("README typo", "documentation fix"), "documentation")

    def test_commercial_only_evidence_is_capped(self):
        for layer in ("Investment", "Hackathon"):
            result = compute_confidence({
                "layers_covered": [layer],
                "input_files": ["a", "b"],
                "source_urls": ["u1", "u2"],
                "synthesis_method": "local_llm",
                "synthesis_date": datetime.now(UTC).isoformat(),
            })
            self.assertEqual(result["label"], "Low")
            self.assertEqual(result["score"], 40)

    def test_literature_manual_document_has_nonempty_required_authors(self):
        paper = literature_agent.Paper(
            title="Manual paper",
            summary="A blockchain research problem with scalability and verification constraints.",
            url="manual://paper",
            published=datetime.now(UTC),
            authors=["Unknown"],
            source="Manual",
            source_id="manual:paper",
            doi=None,
            venue=None,
            retrieval_timestamp=datetime.now(UTC).isoformat(),
        )
        self.assertTrue(paper.authors)


if __name__ == "__main__":
    unittest.main()
