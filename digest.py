"""
CMW Daily Digest — posts a morning summary of all open matched tenders to Slack.

Run:
  python digest.py
"""

import logging
import sys

import yaml

from src.notifications.slack import post_digest
from src.storage.db import get_connection, get_open_matched_tenders

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("digest")


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def main() -> None:
    storage_cfg = load_config("config/settings.yaml")["storage"]
    db_path = storage_cfg.get("db_path", "")

    with get_connection(db_path) as conn:
        tenders = get_open_matched_tenders(conn)

    logger.info("Digest: %d open matched tenders", len(tenders))
    post_digest(tenders)


if __name__ == "__main__":
    main()
