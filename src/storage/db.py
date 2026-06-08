"""
SQL layer for tenders.db.

Schema is designed for SQLite now, Postgres later — no SQLite-isms in queries
except for the upsert syntax (ON CONFLICT DO UPDATE), which Postgres also supports.
To migrate: change the connection string in get_connection(); everything else stays.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

from src.storage.models import Match, Tender

DDL = """
CREATE TABLE IF NOT EXISTS tenders (
    id              TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT,
    category        TEXT,
    reference_no    TEXT,
    detail_url      TEXT NOT NULL,
    status          TEXT,
    posted_date     DATE,
    closing_date    DATE,
    raw             JSON,
    bid_categories  JSON,
    first_seen_at   TIMESTAMP NOT NULL,
    last_seen_at    TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tender_id         TEXT NOT NULL REFERENCES tenders(id),
    matched_keywords  JSON NOT NULL,
    categories        JSON NOT NULL,
    top_tier          INTEGER NOT NULL,
    score             REAL NOT NULL,
    confidence        TEXT NOT NULL,
    relevance_label   TEXT,
    created_at        TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tenders_source       ON tenders(source_id);
CREATE INDEX IF NOT EXISTS idx_tenders_status       ON tenders(status);
CREATE INDEX IF NOT EXISTS idx_tenders_closing_date ON tenders(closing_date);
CREATE INDEX IF NOT EXISTS idx_matches_tender_id    ON matches(tender_id);
CREATE INDEX IF NOT EXISTS idx_matches_confidence   ON matches(confidence);
"""


@contextmanager
def get_connection(db_path: str) -> Generator[sqlite3.Connection, None, None]:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply additive schema migrations without dropping data."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(tenders)")}
    if "bid_categories" not in existing_cols:
        conn.execute("ALTER TABLE tenders ADD COLUMN bid_categories JSON")


def init_db(db_path: str) -> None:
    with get_connection(db_path) as conn:
        conn.executescript(DDL)
        _migrate(conn)


def upsert_tender(conn: sqlite3.Connection, tender: Tender) -> bool:
    """Insert or update a tender. Returns True if this is a newly seen tender."""
    now = datetime.utcnow().isoformat()
    existing = conn.execute(
        "SELECT id FROM tenders WHERE id = ?", (tender.id,)
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE tenders SET
                title          = ?,
                description    = ?,
                category       = ?,
                reference_no   = ?,
                detail_url     = ?,
                status         = ?,
                posted_date    = ?,
                closing_date   = ?,
                raw            = ?,
                bid_categories = ?,
                last_seen_at   = ?
            WHERE id = ?""",
            (
                tender.title, tender.description, tender.category,
                tender.reference_no, tender.detail_url, tender.status,
                tender.posted_date, tender.closing_date,
                tender.raw_json(), tender.bid_categories_json(), now,
                tender.id,
            ),
        )
        return False
    else:
        conn.execute(
            """INSERT INTO tenders (
                id, source_id, source_name, title, description, category,
                reference_no, detail_url, status, posted_date, closing_date,
                raw, bid_categories, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tender.id, tender.source_id, tender.source_name,
                tender.title, tender.description, tender.category,
                tender.reference_no, tender.detail_url, tender.status,
                tender.posted_date, tender.closing_date,
                tender.raw_json(), tender.bid_categories_json(), now, now,
            ),
        )
        return True


def insert_match(conn: sqlite3.Connection, match: Match) -> None:
    """Delete any prior match for this tender and insert fresh results."""
    conn.execute("DELETE FROM matches WHERE tender_id = ?", (match.tender_id,))
    conn.execute(
        """INSERT INTO matches (
            tender_id, matched_keywords, categories, top_tier,
            score, confidence, relevance_label, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            match.tender_id, match.matched_keywords_json(),
            match.categories_json(), match.top_tier,
            match.score, match.confidence, match.relevance_label,
            match.created_at.isoformat(),
        ),
    )


def get_open_matched_tenders(conn: sqlite3.Connection) -> list[dict]:
    """Return all tenders with a match, joined, ordered for dashboard output."""
    rows = conn.execute(
        """SELECT
            t.id, t.source_id, t.source_name, t.title, t.description,
            t.category, t.reference_no, t.detail_url, t.status,
            t.posted_date, t.closing_date, t.first_seen_at,
            t.bid_categories,
            m.matched_keywords, m.categories, m.top_tier,
            m.score, m.confidence, m.relevance_label
        FROM tenders t
        JOIN matches m ON t.id = m.tender_id
        WHERE t.status = 'Open'
        ORDER BY
            CASE m.confidence WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END,
            t.closing_date ASC NULLS LAST"""
    ).fetchall()
    return [dict(r) for r in rows]
