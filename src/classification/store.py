"""
Database access for the industry classification run.

Deliberately independent of src/storage/db.py so the daily pipeline and this
one-off analysis share no code path.

Two rules this module keeps:
  1. The `tenders` table is READ ONLY here — never inserted, updated, or
     altered.
  2. Everything written goes to `tender_industry`, a separate table this
     module owns. The pipeline never reads it, so it cannot affect a daily run.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, Optional, Sequence

import psycopg2
import psycopg2.extras

from src.classification.taxonomy import TAXONOMY_VERSION

logger = logging.getLogger(__name__)

CACHE_TABLE = "tender_industry"

CACHE_DDL = f"""
CREATE TABLE IF NOT EXISTS {CACHE_TABLE} (
    tender_id           TEXT PRIMARY KEY,
    industry            TEXT        NOT NULL,
    industry_group      TEXT        NOT NULL,
    secondary_industry  TEXT,
    confidence          TEXT,
    note                TEXT,
    model               TEXT,
    taxonomy_version    TEXT,
    classified_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tender_industry_industry
    ON {CACHE_TABLE}(industry);
CREATE INDEX IF NOT EXISTS idx_tender_industry_group
    ON {CACHE_TABLE}(industry_group);
"""

_TENDER_COLUMNS = """
    id, source_id, source_name, title, description, category,
    reference_no, detail_url, status, posted_date, closing_date,
    bid_categories, llm_decision, llm_reason, first_seen_at
"""


@contextmanager
def get_connection() -> Generator[psycopg2.extensions.connection, None, None]:
    """Yield a psycopg2 connection built from DATABASE_URL."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set — the classifier needs the Neon "
            "connection string to read the tenders table."
        )
    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_cache_table(conn) -> None:
    """Create this module's own cache table. Never touches `tenders`."""
    with conn.cursor() as cur:
        cur.execute(CACHE_DDL)
    conn.commit()


def fetch_tenders(conn, statuses: Optional[Sequence[str]] = None) -> list[dict]:
    """
    Read every tender (optionally filtered by status). Read-only.

    Statuses are matched case-insensitively so 'open' and 'Open' both work.
    """
    query = f"SELECT {_TENDER_COLUMNS} FROM tenders"
    params: list = []
    if statuses:
        query += " WHERE LOWER(status) = ANY(%s)"
        params.append([s.lower() for s in statuses])
    query += " ORDER BY source_name, closing_date NULLS LAST, title"

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]

    logger.info("Read %d tenders from the database", len(rows))
    return rows


def fetch_cached(conn, taxonomy_version: str = TAXONOMY_VERSION) -> dict[str, dict]:
    """
    Return cached classifications keyed by tender id.

    Rows produced under a different taxonomy version are ignored, so bumping
    TAXONOMY_VERSION automatically re-classifies everything.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT tender_id, industry, industry_group, secondary_industry,
                       confidence, note, model, classified_at
                  FROM {CACHE_TABLE}
                 WHERE taxonomy_version = %s""",
            (taxonomy_version,),
        )
        cached = {r["tender_id"]: dict(r) for r in cur.fetchall()}

    logger.info("Found %d cached classifications for taxonomy v%s", len(cached), taxonomy_version)
    return cached


def cache_table_exists(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) AS reg", (CACHE_TABLE,))
        return cur.fetchone()["reg"] is not None


def save_classifications(conn, classifications, model: str) -> int:
    """Upsert a batch of classifications into the cache table."""
    if not classifications:
        return 0

    now = datetime.now(timezone.utc)
    rows = [
        (
            c.tender_id, c.industry, c.industry_group, c.secondary_industry,
            c.confidence, c.note, model, TAXONOMY_VERSION, now,
        )
        for c in classifications
    ]

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            f"""INSERT INTO {CACHE_TABLE} (
                    tender_id, industry, industry_group, secondary_industry,
                    confidence, note, model, taxonomy_version, classified_at
                ) VALUES %s
                ON CONFLICT (tender_id) DO UPDATE SET
                    industry           = EXCLUDED.industry,
                    industry_group     = EXCLUDED.industry_group,
                    secondary_industry = EXCLUDED.secondary_industry,
                    confidence         = EXCLUDED.confidence,
                    note               = EXCLUDED.note,
                    model              = EXCLUDED.model,
                    taxonomy_version   = EXCLUDED.taxonomy_version,
                    classified_at      = EXCLUDED.classified_at""",
            rows,
        )
    conn.commit()
    return len(rows)
