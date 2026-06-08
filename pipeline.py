"""
CMW Tender Monitor — main pipeline.

Orchestrates: collect → store → match → (optional LLM pass) → build dashboard

Run:
  python pipeline.py                 # normal run
  python pipeline.py --dry-run       # collect + match but don't write DB or dashboard
  python pipeline.py --source vaughan  # single source (dev/debug)
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import yaml

from src.collectors.bidsandtenders import collect
from src.dashboard.build import build
from src.matching.matcher import load_disqualifiers, load_keywords, match_tender
from src.matching.relevance import adjudicate
from src.storage.db import get_connection, init_db, insert_match, upsert_tender

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("pipeline")


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def run(args: argparse.Namespace) -> None:
    run_started_at = datetime.utcnow()

    sources_cfg  = load_config("config/sources.yaml")
    settings_cfg = load_config("config/settings.yaml")

    sources       = sources_cfg["sources"]
    crawl         = settings_cfg["crawl"]
    match_cfg     = settings_cfg["matching"]
    llm_cfg       = settings_cfg["llm"]
    storage_cfg   = settings_cfg["storage"]
    dashboard_cfg = settings_cfg["dashboard"]

    db_path = storage_cfg["db_path"]

    if not args.dry_run:
        init_db(db_path)

    keywords     = load_keywords("config/keywords.yaml")
    disqualifiers = load_disqualifiers("config/disqualifiers.yaml")

    tier_weights = {int(k): v for k, v in match_cfg["tier_weights"].items()}

    # Filter to requested source(s) if --source specified
    if args.source:
        sources = [s for s in sources if s["id"] == args.source]
        if not sources:
            logger.error("Unknown source id: %s", args.source)
            sys.exit(1)

    total_new = 0
    total_matched = 0
    failed_sources: list[str] = []

    for source in sources:
        try:
            tenders = collect(
                source=source,
                user_agent=crawl["user_agent"],
                rate_limit_seconds=crawl["rate_limit_seconds"],
                max_retries=crawl["max_retries"],
                backoff_base_seconds=crawl["backoff_base_seconds"],
                timeout_seconds=crawl["timeout_seconds"],
                max_per_source=crawl["max_per_source"],
            )
        except Exception as exc:
            logger.error("Unhandled error collecting %s: %s", source["id"], exc)
            failed_sources.append(source["id"])
            continue

        if not tenders:
            logger.info("%s: 0 tenders returned", source["name"])
            continue

        new_count = 0
        matched_count = 0

        # Optional LLM pass: collect candidates first, then adjudicate
        # (collect all tenders, identify tier-2/3 candidates, batch if desired)
        for tender in tenders:
            relevance_label = None

            if llm_cfg.get("enabled") and tender:
                # Determine if this tender has tier-2 or tier-3 hits
                # (do a quick pre-check before paying for LLM)
                from src.matching.matcher import keyword_pattern
                from src.matching.normalize import normalize
                norm_text = normalize(f"{tender.title} {tender.description}")
                candidate_tiers = {
                    kw["tier"] for kw in keywords
                    if kw["_pattern"].search(norm_text)
                }
                if candidate_tiers and min(candidate_tiers) >= 2:
                    relevance_label = adjudicate(
                        title=tender.title,
                        description=tender.description,
                        model=llm_cfg["model"],
                        max_tokens=llm_cfg["max_tokens"],
                    )

            match = match_tender(
                tender=tender,
                keywords=keywords,
                disqualifiers=disqualifiers,
                tier_weights=tier_weights,
                keyword_bonus=match_cfg["keyword_match_bonus"],
                category_diversity_bonus=match_cfg["category_diversity_bonus"],
                confidence_thresholds=match_cfg["confidence_thresholds"],
                relevance_label=relevance_label,
                relevance_bonus=llm_cfg.get("relevance_bonus", 0),
            )

            if not args.dry_run:
                with get_connection(db_path) as conn:
                    is_new = upsert_tender(conn, tender)
                    if is_new:
                        new_count += 1
                    if match:
                        insert_match(conn, match)
                        matched_count += 1
            else:
                if match:
                    matched_count += 1
                    logger.info(
                        "DRY-RUN match: [%s] %s — %s (score=%.1f keywords=%s)",
                        match.confidence, source["name"],
                        tender.title, match.score, match.matched_keywords,
                    )

        logger.info(
            "%s: %d tenders | %d new | %d matched",
            source["name"], len(tenders), new_count, matched_count,
        )
        total_new += new_count
        total_matched += matched_count

    logger.info(
        "Run complete: %d new tenders, %d matched, %d sources failed",
        total_new, total_matched, len(failed_sources),
    )
    if failed_sources:
        logger.warning("Failed sources: %s", ", ".join(failed_sources))

    if not args.dry_run and not args.skip_dashboard:
        build(
            db_path=db_path,
            output_path=dashboard_cfg["output_path"],
            snippet_length=dashboard_cfg["snippet_length"],
            closing_soon_days=dashboard_cfg["closing_soon_days"],
            run_started_at=run_started_at,
        )

    if failed_sources and not args.ignore_errors:
        sys.exit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="CMW Tender Monitor pipeline")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Collect and match but do not write to DB or build dashboard",
    )
    parser.add_argument(
        "--source", metavar="SOURCE_ID",
        help="Run only for a single source ID (useful for debugging)",
    )
    parser.add_argument(
        "--skip-dashboard", action="store_true",
        help="Skip dashboard build (run collection + matching only)",
    )
    parser.add_argument(
        "--ignore-errors", action="store_true",
        help="Exit 0 even if some sources failed (useful in CI)",
    )
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
