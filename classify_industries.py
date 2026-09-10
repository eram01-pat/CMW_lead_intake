"""
One-off industry classification of every tender in the database.

Reads the tenders table (read-only), asks Claude which industry each tender
belongs to, and writes an Excel workbook summarising how many tenders fall
into each industry.

This is a standalone analysis tool. It shares no code with the daily
pipeline, never writes to the `tenders` table, and is never run on a
schedule — the workflow that drives it is manual-dispatch only.

Run:
  python classify_industries.py                       # classify everything, write XLSX
  python classify_industries.py --dry-run             # show what would be done, no API calls
  python classify_industries.py --limit 50            # small test run
  python classify_industries.py --status Open         # only open tenders
  python classify_industries.py --force               # ignore cache, re-classify all
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from src.classification.classifier import classify
from src.classification.report import build_workbook, log_summary, write_markdown_summary
from src.classification.store import (
    ensure_cache_table,
    fetch_cached,
    fetch_tenders,
    get_connection,
    save_classifications,
)
from src.classification.taxonomy import TAXONOMY_VERSION, UNCLASSIFIED, group_for

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("classify")

DEFAULT_MODEL = "claude-opus-5"
FAILED_NOTE = "Classification call failed — re-run to retry this tender."


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify every tender in the database by industry and write an Excel summary.",
    )
    parser.add_argument(
        "--output", default="tender_industries.xlsx",
        help="Path of the Excel workbook to write (default: tender_industries.xlsx)",
    )
    parser.add_argument(
        "--summary-md", default="",
        help="Also write the summary tables as Markdown to this path (used for the Actions run summary)",
    )
    parser.add_argument(
        "--status", default="",
        help="Comma-separated tender statuses to include (default: all statuses)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Only classify the first N unclassified tenders (0 = no limit). Useful for a test run.",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Claude model to classify with (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"],
        help="Reasoning effort (default: low — classification is a shallow task)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=25,
        help="Tenders sent per API request (default: 25)",
    )
    parser.add_argument(
        "--workers", type=int, default=4,
        help="Concurrent API requests in flight (default: 4)",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=8000,
        help="max_tokens per request (default: 8000)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-classify every tender, ignoring cached results",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Do not read or write the classification cache table at all (fully read-only DB access)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be classified without calling the API or writing anything",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started_at = datetime.now(timezone.utc)

    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY is not set — cannot classify. (Use --dry-run to inspect the database only.)")
        return 1

    statuses = [s.strip() for s in args.status.split(",") if s.strip()]
    use_cache = not args.no_cache

    with get_connection() as conn:
        if use_cache and not args.dry_run:
            ensure_cache_table(conn)

        tenders = fetch_tenders(conn, statuses or None)
        if not tenders:
            logger.error("No tenders found%s — nothing to classify.",
                         f" with status {statuses}" if statuses else "")
            return 1

        cached: dict[str, dict] = {}
        if use_cache:
            try:
                cached = fetch_cached(conn, TAXONOMY_VERSION)
            except Exception as exc:  # table may not exist yet on a dry run
                logger.info("No usable classification cache yet (%s)", exc)
                conn.rollback()
                cached = {}

        if args.force:
            pending = list(tenders)
            already_classified = 0
            logger.info("--force: re-classifying all %d tenders", len(pending))
        else:
            pending = [t for t in tenders if t["id"] not in cached]
            already_classified = len(tenders) - len(pending)

        deferred = 0
        if args.limit and len(pending) > args.limit:
            deferred = len(pending) - args.limit
            pending = pending[: args.limit]
            logger.info("--limit %d: %d tender(s) left for a later run", args.limit, deferred)

        logger.info(
            "%d tenders total | %d already classified | %d to classify now | %d deferred",
            len(tenders), already_classified, len(pending), deferred,
        )

        if args.dry_run:
            batches = (len(pending) + args.batch_size - 1) // max(1, args.batch_size)
            logger.info(
                "DRY RUN — would send %d tenders to %s in %d batch(es) of %d and write %s",
                len(pending), args.model, batches, args.batch_size, args.output,
            )
            return 0

        results = {}
        failed_ids: list[str] = []
        attempted = len(pending)

        if pending:
            def checkpoint(batch_results) -> None:
                if not use_cache:
                    return
                try:
                    save_classifications(conn, batch_results, args.model)
                except Exception as exc:  # noqa: BLE001 — a cache failure must not lose the run
                    logger.warning("Could not checkpoint batch to the cache table: %s", exc)
                    conn.rollback()

            classifications, failed_ids = classify(
                pending,
                model=args.model,
                effort=args.effort,
                batch_size=args.batch_size,
                workers=args.workers,
                max_tokens=args.max_tokens,
                on_batch_done=checkpoint if use_cache else None,
            )
            results = {c.tender_id: c for c in classifications}
            logger.info("Classified %d tenders (%d failed)", len(results), len(failed_ids))

            if not results:
                logger.error(
                    "Every classification request failed — check the API key, model "
                    "name, and rate limits. No report written."
                )
                return 1
        else:
            logger.info("Nothing new to classify — building the report from cached results.")

    # ── Merge cached + fresh results onto the tender rows ────────────────
    failed = set(failed_ids)
    rows = []
    unclassified_pending = 0

    for tender in tenders:
        row = dict(tender)
        fresh = results.get(tender["id"])
        # Fresh always wins; the cache is the fallback when a call failed —
        # including under --force, so a partial failure never loses a tender
        # that was already classified on an earlier run.
        cached_row = cached.get(tender["id"])

        if fresh is not None:
            row.update(
                industry=fresh.industry,
                industry_group=fresh.industry_group,
                secondary_industry=fresh.secondary_industry,
                confidence=fresh.confidence,
                note=fresh.note,
            )
        elif cached_row is not None:
            row.update(
                industry=cached_row["industry"],
                industry_group=cached_row["industry_group"],
                secondary_industry=cached_row["secondary_industry"],
                confidence=cached_row["confidence"],
                note=cached_row["note"],
            )
        else:
            # Either the API call failed, or --limit left this tender for a later run.
            unclassified_pending += 1
            row.update(
                industry=UNCLASSIFIED,
                industry_group=group_for(UNCLASSIFIED),
                secondary_industry=None,
                confidence="",
                note=FAILED_NOTE if tender["id"] in failed else "Not classified in this run.",
            )
        rows.append(row)

    finished_at = datetime.now(timezone.utc)
    metadata = {
        "Generated (UTC)": finished_at.strftime("%Y-%m-%d %H:%M:%S"),
        "Duration": str(finished_at - started_at).split(".")[0],
        "Tenders in report": len(rows),
        "Classified this run": len(results),
        "Reused from cache": len(rows) - len(results) - unclassified_pending,
        "Not classified": unclassified_pending,
        "Model": args.model,
        "Effort": args.effort,
        "Batch size": args.batch_size,
        "Taxonomy version": TAXONOMY_VERSION,
        "Status filter": ", ".join(statuses) if statuses else "(all)",
    }

    log_summary(rows)
    build_workbook(rows, args.output, run_metadata=metadata)
    if args.summary_md:
        write_markdown_summary(rows, args.summary_md, run_metadata=metadata)
    logger.info("Report written to %s", args.output)

    if unclassified_pending and not args.limit:
        logger.warning(
            "%d tender(s) were not classified — re-run to retry them.", unclassified_pending
        )

    # A run that mostly failed still writes a report (the successful rows are
    # cached and worth keeping) but must not be reported as a green run — the
    # workbook would be dominated by Other / Unclassified.
    if attempted and len(failed_ids) / attempted > 0.10:
        logger.error(
            "%d of %d attempted tenders (%.0f%%) could not be classified — the report is "
            "incomplete. The successful ones are cached; re-run to retry the rest.",
            len(failed_ids), attempted, 100 * len(failed_ids) / attempted,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
