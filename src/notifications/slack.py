"""
Slack notifier — posts a message when a new matched tender is found.

Webhook URL is read from the SLACK_WEBHOOK_URL environment variable.
Failures are logged but never crash the pipeline.
"""

import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)


def _webhook_url() -> str:
    return os.environ.get("SLACK_WEBHOOK_URL", "")


def post_match(
    title: str,
    source_name: str,
    detail_url: str,
    decision: str,
    closing_date=None,
    reference_no: str = "",
) -> None:
    """
    Post a single tender match to Slack.
    decision is 'yes' or 'maybe'.
    Only called for newly-seen tenders so the team never gets duplicate alerts.
    """
    url = _webhook_url()
    if not url:
        logger.debug("SLACK_WEBHOOK_URL not set — skipping notification")
        return

    confidence = "High" if decision == "yes" else "Medium"
    emoji      = "🟢" if decision == "yes" else "🟡"

    lines = [f"{emoji} *NEW TENDER — {confidence} Confidence*"]
    lines.append(f"*{title}*")
    lines.append(f"📍 {source_name}")
    if reference_no:
        lines.append(f"Ref: {reference_no}")
    if closing_date:
        lines.append(f"Closes: {closing_date}")
    lines.append(f"<{detail_url}|View Tender →>")

    payload = {"text": "\n".join(lines)}

    try:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning("Slack returned %s", resp.status)
    except Exception as exc:
        logger.warning("Slack notification failed: %s", exc)
