from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents.utils import cleaned_text

DEFAULT_DB_PATH = Path("outputs/literature") / "literature_memory.db"
CONCEPT_VOCAB = [
    "trust",
    "verification",
    "privacy",
    "scalability",
    "compliance",
    "identity",
    "interoperability",
    "security",
    "governance",
    "efficiency",
    "latency",
    "auditability",
    "resilience",
    "tokenization",
]


def db_connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def ensure_columns(connection: sqlite3.Connection, table: str, required_columns: dict[str, str]) -> None:
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in required_columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    connection.commit()


def _paper_identity_key(*, doi: str = "", source_id: str = "", url: str = "", title: str = "", authors: str = "", year: int = 0) -> str:
    import hashlib
    value = ""
    if doi.strip():
        value = "doi:" + doi.strip().lower()
    elif source_id.strip():
        value = "source:" + source_id.strip().lower()
    elif url.strip():
        value = "url:" + url.strip().rstrip("/").lower()
    else:
        author_key = re.sub(r"[^a-z0-9]+", " ", authors.lower()).strip()
        title_key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        value = f"bib:{title_key}|{author_key}|{year}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _migrate_papers_table(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(papers)").fetchall()}
    if not columns or "paper_key" in columns:
        return
    connection.execute("ALTER TABLE papers RENAME TO papers_legacy")
    connection.execute(
        """
        CREATE TABLE papers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_key TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            authors TEXT NOT NULL,
            year INTEGER NOT NULL,
            research_area TEXT NOT NULL,
            path TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '',
            source_id TEXT NOT NULL DEFAULT '',
            doi TEXT,
            summary TEXT NOT NULL DEFAULT '',
            static_tags TEXT NOT NULL,
            keyword_tags TEXT NOT NULL,
            concept_tags TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    legacy_cols = {row[1] for row in connection.execute("PRAGMA table_info(papers_legacy)").fetchall()}
    rows = connection.execute("SELECT * FROM papers_legacy").fetchall()
    for row in rows:
        data = dict(row)
        title = str(data.get("title", ""))
        authors = str(data.get("authors", ""))
        year = int(data.get("year") or 0)
        url = str(data.get("url", ""))
        key = _paper_identity_key(url=url, title=title, authors=authors, year=year)
        try:
            connection.execute(
                """
                INSERT OR IGNORE INTO papers
                (paper_key,title,authors,year,research_area,path,url,source_id,doi,summary,static_tags,keyword_tags,concept_tags,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    key, title, authors, year, str(data.get("research_area","")),
                    str(data.get("path","")), url, "", None,
                    str(data.get("summary","")), str(data.get("static_tags","[]")),
                    str(data.get("keyword_tags","[]")), str(data.get("concept_tags","[]")),
                    str(data.get("created_at","")),
                ),
            )
        except sqlite3.Error:
            continue
    connection.execute("DROP TABLE papers_legacy")
    connection.commit()


def init_papers_db(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS papers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_key TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            authors TEXT NOT NULL,
            year INTEGER NOT NULL,
            research_area TEXT NOT NULL,
            path TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '',
            source_id TEXT NOT NULL DEFAULT '',
            doi TEXT,
            summary TEXT NOT NULL DEFAULT '',
            static_tags TEXT NOT NULL,
            keyword_tags TEXT NOT NULL,
            concept_tags TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    _migrate_papers_table(connection)



def parse_markdown_section(markdown: str, heading: str) -> str:
    pattern = rf"## {re.escape(heading)}\n(.*?)(?:\n## |\Z)"
    match = re.search(pattern, markdown, flags=re.DOTALL)
    if not match:
        return ""
    return cleaned_text(match.group(1).strip())


def parse_markdown_tags(markdown: str) -> list[str]:
    section = parse_markdown_section(markdown, "Tags")
    return [tag for tag in section.split() if tag.startswith("#")]


def paper_exists(
    connection: sqlite3.Connection,
    *,
    title: str = "",
    url: str = "",
    doi: str = "",
    source_id: str = "",
    authors: str = "",
    year: int = 0,
) -> bool:
    key = _paper_identity_key(doi=doi, source_id=source_id, url=url, title=title, authors=authors, year=year)
    row = connection.execute("SELECT 1 FROM papers WHERE paper_key = ?", (key,)).fetchone()
    return row is not None


def upsert_paper_record(
    connection: sqlite3.Connection,
    *,
    title: str,
    authors: str,
    year: int,
    research_area: str,
    path: str,
    url: str,
    source_id: str = "",
    doi: str | None = None,
    summary: str,
    static_tags: list[str],
    keyword_tags: list[str],
    concept_tags: list[str],
) -> None:
    key = _paper_identity_key(doi=doi or "", source_id=source_id, url=url, title=title, authors=authors, year=year)
    connection.execute(
        """
        INSERT INTO papers
        (paper_key,title,authors,year,research_area,path,url,source_id,doi,summary,static_tags,keyword_tags,concept_tags,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(paper_key) DO UPDATE SET
            title=excluded.title,
            authors=excluded.authors,
            year=excluded.year,
            research_area=excluded.research_area,
            path=excluded.path,
            url=excluded.url,
            source_id=excluded.source_id,
            doi=excluded.doi,
            summary=excluded.summary,
            static_tags=excluded.static_tags,
            keyword_tags=excluded.keyword_tags,
            concept_tags=excluded.concept_tags
        """,
        (
            key, title, authors, year, research_area, path, url, source_id, doi, summary,
            json.dumps(static_tags), json.dumps(keyword_tags), json.dumps(concept_tags),
            "",
        ),
    )
    connection.commit()



def bootstrap_memory_from_outputs(
    connection: sqlite3.Connection,
    output_dir: Path,
    static_tags_by_area: dict[str, str],
) -> None:
    for markdown_path in output_dir.glob("*.md"):
        try:
            content = markdown_path.read_text(encoding="utf-8")
        except OSError:
            continue

        title_match = re.search(r"# Paper: (.+)", content)
        if not title_match:
            continue

        title = cleaned_text(title_match.group(1))
        authors = parse_markdown_section(content, "Authors")
        year_text = parse_markdown_section(content, "Year")
        area = parse_markdown_section(content, "Area") or "Unknown"
        url = parse_markdown_section(content, "URL")
        summary = parse_markdown_section(content, "Summary")
        tags = parse_markdown_tags(content)
        static_tags = [tag for tag in tags if tag in {"#Literature", static_tags_by_area.get(area, "")}]
        concept_tags = [tag for tag in tags if tag.lstrip("#").lower() in CONCEPT_VOCAB]
        keyword_tags = [tag for tag in tags if tag not in static_tags and tag not in concept_tags]
        try:
            year = int(year_text)
        except (ValueError, TypeError):
            year = 0  # UNKNOWN — do not fabricate current year

        upsert_paper_record(
            connection,
            title=title,
            authors=authors,
            year=year,
            research_area=area,
            path=str(markdown_path),
            url=url,
            summary=summary,
            static_tags=static_tags,
            keyword_tags=keyword_tags,
            concept_tags=concept_tags,
        )


def save_paper_record(
    connection: sqlite3.Connection,
    *,
    title: str,
    authors: str,
    year: int,
    research_area: str,
    path: str,
    url: str,
    summary: str,
    tags: list[str],
    concept_tags: list[str],
    static_tags_by_area: dict[str, str],
    source_id: str = "",
    doi: str | None = None,
) -> None:
    static_tags = [tag for tag in tags if tag in {"#Literature", static_tags_by_area.get(research_area, "")}]
    keyword_tags = [tag for tag in tags if tag not in static_tags and tag not in concept_tags]
    upsert_paper_record(
        connection,
        title=title,
        authors=authors,
        year=year,
        research_area=research_area,
        path=path,
        url=url,
        source_id=source_id,
        doi=doi,
        summary=summary,
        static_tags=static_tags,
        keyword_tags=keyword_tags,
        concept_tags=concept_tags,
    )


def fetch_catalog_entries(connection: sqlite3.Connection, exclude_title: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT title, research_area, url, summary, keyword_tags, concept_tags FROM papers WHERE title != ?",
        (exclude_title,),
    ).fetchall()
    entries: list[dict[str, Any]] = []
    for row in rows:
        entries.append(
            {
                "title": row["title"],
                "research_area": row["research_area"],
                "url": row["url"],
                "summary": row["summary"],
                "keyword_tags": json.loads(row["keyword_tags"] or "[]"),
                "concept_tags": json.loads(row["concept_tags"] or "[]"),
            }
        )
    return entries
