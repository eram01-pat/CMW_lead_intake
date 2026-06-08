"""
Static dashboard builder. Reads matched tenders from DB and renders index.html.
Published to GitHub Pages by the Actions workflow.
"""

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from src.storage.db import get_connection, get_open_matched_tenders

logger = logging.getLogger(__name__)

CATEGORY_NAMES = {
    1: "Fleet & Vehicle Washing",
    2: "Parking-Structure & Floor",
    3: "Pressure-Washing & Exterior",
    4: "Graffiti & Specialty Surface",
    5: "Industrial / Facility Cleaning",
    6: "Municipal / Contract Umbrella",
}

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _highlight_keywords(text: str, keywords: list[str], max_len: int) -> str:
    """Return a snippet with matched keywords wrapped in <mark> tags."""
    if not text:
        return ""

    # Find the first keyword occurrence to anchor the snippet
    snippet_start = 0
    for kw in keywords:
        idx = text.lower().find(kw.lower())
        if idx >= 0:
            snippet_start = max(0, idx - 100)
            break

    snippet = text[snippet_start : snippet_start + max_len]
    if snippet_start > 0:
        snippet = "…" + snippet
    if snippet_start + max_len < len(text):
        snippet = snippet + "…"

    # Escape HTML
    snippet = (
        snippet
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    # Wrap matched keywords in <mark>
    for kw in keywords:
        escaped_kw = re.escape(kw)
        snippet = re.sub(
            r"(?i)\b" + escaped_kw + r"\b",
            lambda m: f"<mark>{m.group()}</mark>",
            snippet,
        )

    return snippet


def _days_until(d: date | str | None) -> int | None:
    if d is None:
        return None
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d)
        except ValueError:
            return None
    return (d - date.today()).days


def build(
    db_path: str,
    output_path: str,
    snippet_length: int = 400,
    closing_soon_days: int = 7,
    run_started_at: datetime | None = None,
) -> None:
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with get_connection(db_path) as conn:
        rows = get_open_matched_tenders(conn)

        # Count total open tenders in DB for the header stat
        total_open = conn.execute(
            "SELECT COUNT(*) FROM tenders WHERE status = 'Open'"
        ).fetchone()[0]

        source_count = conn.execute(
            "SELECT COUNT(DISTINCT source_id) FROM tenders WHERE status = 'Open'"
        ).fetchone()[0]

    if run_started_at is None:
        run_started_at = datetime.utcnow()

    # How recently is "new" — tenders first seen in the last 24h of this run
    new_cutoff = run_started_at.replace(hour=0, minute=0, second=0, microsecond=0)

    template_data: list[dict] = []
    for row in rows:
        keywords = json.loads(row["matched_keywords"])
        cats = json.loads(row["categories"])

        days = _days_until(row["closing_date"])
        first_seen = row["first_seen_at"]
        if isinstance(first_seen, str):
            try:
                first_seen = datetime.fromisoformat(first_seen)
            except ValueError:
                first_seen = None

        cat_label = CATEGORY_NAMES.get(cats[0], "") if cats else ""
        # If multiple categories, pick the most specific (lowest number)
        if cats:
            cat_label = CATEGORY_NAMES.get(min(cats), "")

        template_data.append({
            "title":          row["title"],
            "detail_url":     row["detail_url"],
            "source_name":    row["source_name"],
            "reference_no":   row["reference_no"] or "",
            "category":       row["category"] or "",
            "category_label": cat_label,
            "posted_date":    row["posted_date"],
            "closing_date":   row["closing_date"],
            "confidence":     row["confidence"],
            "matched_keywords": keywords,
            "snippet":        _highlight_keywords(
                                  row["description"] or "", keywords, snippet_length
                              ),
            "is_new":         bool(first_seen and first_seen >= new_cutoff),
            "days_until_close": days,
            "closing_soon":   bool(days is not None and 0 <= days <= closing_soon_days),
        })

    all_sources = sorted({t["source_name"] for t in template_data})
    all_categories = sorted({t["category_label"] for t in template_data if t["category_label"]})

    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=False)
    tmpl = env.get_template("index.html.j2")

    html = tmpl.render(
        generated_at=run_started_at.strftime("%Y-%m-%d %H:%M UTC"),
        total_open=total_open,
        source_count=source_count,
        tenders=template_data,
        sources=all_sources,
        categories=all_categories,
    )

    Path(output_path).write_text(html, encoding="utf-8")
    logger.info("Dashboard written to %s (%d matched tenders)", output_path, len(template_data))
