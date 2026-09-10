"""
Claude-powered industry classification of tenders.

Tenders are sent to the Claude API in batches (one request per batch of N
tenders) and returned as structured JSON constrained to the taxonomy, so the
model cannot invent a category. Batches are dispatched concurrently across a
small thread pool; the shared system prompt is marked for prompt caching so
the taxonomy is only paid for in full once.

This module is standalone — it is not imported by the daily pipeline.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from src.classification.taxonomy import (
    INDUSTRIES,
    NO_SECONDARY,
    UNCLASSIFIED,
    group_for,
    normalize_industry,
    taxonomy_prompt_block,
)

logger = logging.getLogger(__name__)

# Listing/detail descriptions on bids&tenders.ca are almost always this
# boilerplate rather than a real scope of work — drop it rather than pay
# tokens for it (see ACCESS_NOTES.md).
_BOILERPLATE_MARKERS = (
    "only online submissions",
    "only electronic submissions",
    "will be accepted for this",
)

_MAX_DESCRIPTION_CHARS = 600

# output_config.effort is only accepted by these model families. Sending it to
# a model that does not support it (Haiku 4.5, Sonnet 4.5, and older) is a 400,
# so it is omitted for those rather than failing every batch.
_EFFORT_SUPPORTED_PREFIXES = (
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-fable-5",
    "claude-mythos-5",
)


def supports_effort(model: str) -> bool:
    return model.startswith(_EFFORT_SUPPORTED_PREFIXES)


@dataclass
class Classification:
    tender_id: str
    industry: str
    industry_group: str
    secondary_industry: Optional[str]
    confidence: str
    note: str


def _system_prompt() -> str:
    return f"""\
You are a procurement analyst categorising Ontario public-sector tenders \
(municipalities, regional governments, and school boards) by the industry or \
trade that would perform the work.

For each tender you are given, choose the ONE industry from the taxonomy below \
that best describes the primary work being procured.

TAXONOMY — you must copy the industry name exactly as written here:

{taxonomy_prompt_block()}

HOW TO DECIDE:
- Classify by the work being bought, not by who is buying it. A school board \
tender for roof replacement is Roofing, not Education.
- Classify by the PRIMARY trade. If a tender bundles several trades, pick the \
dominant one and put the next most significant one in secondary_industry.
- Supply vs. service matters: buying trucks is "Vehicles & Heavy Equipment"; \
washing trucks is "Fleet & Vehicle Washing"; repairing trucks is \
"Facility Operations & Maintenance" only if no better fit exists.
- Consulting and design work goes to the professional-services group even when \
the subject matter is construction — "engineering services for a bridge" is \
"Engineering & Design Consulting", not "Bridges & Structures".
- Roster, standing offer, vendor-of-record, and pre-qualification postings \
should be classified by the work the roster covers.
- Titles carry a reference prefix (RFP26-144, T-2026-11, Q-25-3) — ignore it.

INPUT QUALITY:
The description field is usually missing or boilerplate on this platform, so \
in most cases the title and bid categories are all you have. That is expected. \
Make the best supportable judgement from the title, and record how sure you \
are in the confidence field:
- high   — the title names the work unambiguously
- medium — the work is strongly implied but not stated outright
- low    — you are inferring from thin wording
Use "{UNCLASSIFIED}" only when the title genuinely carries no usable signal \
(e.g. a bare reference number). Prefer a low-confidence real category over \
"{UNCLASSIFIED}".

Return one entry for EVERY tender you were given, echoing back its "ref" number.
Keep "note" under 15 words: what in the input drove the decision.\
"""


def _response_schema(batch_size: int) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "classifications": {
                    # No minItems/maxItems: the structured-output validator only
                    # accepts 0 or 1 for those, so pinning them to the batch size
                    # is rejected with a 400. Completeness is asked for in the
                    # prompt instead, and _parse_batch treats any tender the model
                    # skips as unclassified — left uncached so a re-run retries it.
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "ref": {"type": "integer"},
                            "industry": {"type": "string", "enum": INDUSTRIES},
                            "secondary_industry": {
                                "type": "string",
                                "enum": INDUSTRIES + [NO_SECONDARY],
                            },
                            "confidence": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                            "note": {"type": "string"},
                        },
                        "required": [
                            "ref",
                            "industry",
                            "secondary_industry",
                            "confidence",
                            "note",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["classifications"],
            "additionalProperties": False,
        },
    }


def _useful_description(description: Optional[str]) -> str:
    if not description:
        return ""
    text = " ".join(description.split())
    lowered = text.lower()
    if any(marker in lowered for marker in _BOILERPLATE_MARKERS):
        return ""
    if len(text) < 40:
        return ""
    return text[:_MAX_DESCRIPTION_CHARS]


def _render_batch(items: Sequence[dict]) -> str:
    blocks: list[str] = []
    for ref, item in enumerate(items, start=1):
        lines = [f"[{ref}] Title: {item.get('title') or '(no title)'}"]

        categories = item.get("bid_categories") or []
        if isinstance(categories, str):
            categories = [categories]
        if categories:
            lines.append(f"    Bid categories: {', '.join(str(c) for c in categories)}")

        if item.get("source_name"):
            lines.append(f"    Issuer: {item['source_name']}")

        description = _useful_description(item.get("description"))
        if description:
            lines.append(f"    Description: {description}")

        blocks.append("\n".join(lines))

    count = len(items)
    return (
        f"Classify each of the following {count} tenders.\n\n"
        + "\n\n".join(blocks)
        + f"\n\nReturn exactly {count} objects in \"classifications\" — one for each "
          f"tender above, with \"ref\" running from 1 to {count}. Do not stop after "
          f"the first one; every tender listed must appear exactly once."
    )


def _call_with_retry(
    client,
    *,
    model: str,
    effort: str,
    max_tokens: int,
    items: Sequence[dict],
    max_attempts: int,
) -> dict[str, Any]:
    """One batch request, retrying transient failures with exponential backoff."""
    import anthropic

    last_exc: Optional[Exception] = None
    send_effort = supports_effort(model)
    if not send_effort:
        logger.debug("%s does not accept output_config.effort — omitting it", model)

    for attempt in range(max_attempts):
        output_config: dict[str, Any] = {"format": _response_schema(len(items))}
        if send_effort:
            output_config["effort"] = effort

        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": _system_prompt(),
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                output_config=output_config,
                messages=[{"role": "user", "content": _render_batch(items)}],
            )
            if response.stop_reason == "max_tokens":
                raise RuntimeError(
                    "response truncated at max_tokens — lower --batch-size "
                    "or raise --max-tokens"
                )
            text = next(b.text for b in response.content if b.type == "text")
            return json.loads(text)
        except anthropic.RateLimitError as exc:
            last_exc = exc
            wait = min(60.0, 5.0 * (2**attempt)) + random.uniform(0, 2)
            logger.warning(
                "Rate limited (attempt %d/%d) — sleeping %.1fs",
                attempt + 1, max_attempts, wait,
            )
            time.sleep(wait)
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            last_exc = exc
            wait = min(30.0, 2.0 * (2**attempt)) + random.uniform(0, 1)
            logger.warning(
                "Connection error (attempt %d/%d): %s — sleeping %.1fs",
                attempt + 1, max_attempts, exc, wait,
            )
            time.sleep(wait)
        except anthropic.APIStatusError as exc:
            last_exc = exc
            if exc.status_code == 400 and send_effort and "effort" in str(exc).lower():
                # Model does not accept output_config.effort after all —
                # drop it and retry rather than failing the whole run.
                logger.warning("%s rejected output_config.effort — retrying without it", model)
                send_effort = False
                continue
            if exc.status_code < 500:
                raise
            wait = min(30.0, 2.0 * (2**attempt)) + random.uniform(0, 1)
            logger.warning(
                "Server error %s (attempt %d/%d) — sleeping %.1fs",
                exc.status_code, attempt + 1, max_attempts, wait,
            )
            time.sleep(wait)

    raise RuntimeError(f"gave up after {max_attempts} attempts") from last_exc


def _parse_batch(payload: dict[str, Any], items: Sequence[dict]) -> list[Classification]:
    """
    Map the model's response back onto the batch by echoed ref number.

    Items the model failed to return are simply omitted — the caller reports
    them as unclassified and they stay uncached so a re-run retries them.
    """
    by_ref: dict[int, dict] = {}
    for entry in payload.get("classifications") or []:
        try:
            ref = int(entry.get("ref"))
        except (TypeError, ValueError):
            continue
        if 1 <= ref <= len(items):
            by_ref.setdefault(ref, entry)

    results: list[Classification] = []
    for ref, item in enumerate(items, start=1):
        entry = by_ref.get(ref)
        if entry is None:
            logger.warning("Model returned no entry for %r", item.get("title", "")[:60])
            continue

        industry = normalize_industry(entry.get("industry"))
        secondary_raw = (entry.get("secondary_industry") or "").strip()
        secondary = (
            None
            if secondary_raw in ("", NO_SECONDARY)
            else normalize_industry(secondary_raw)
        )
        if secondary == industry or secondary == UNCLASSIFIED:
            secondary = None

        confidence = str(entry.get("confidence") or "").strip().lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "low"

        results.append(
            Classification(
                tender_id=item["id"],
                industry=industry,
                industry_group=group_for(industry),
                secondary_industry=secondary,
                confidence=confidence,
                note=(entry.get("note") or "").strip()[:300],
            )
        )
    return results


def _chunk(items: Sequence[dict], size: int) -> list[list[dict]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def classify(
    items: Sequence[dict],
    *,
    model: str,
    effort: str = "low",
    batch_size: int = 25,
    workers: int = 4,
    max_tokens: int = 8000,
    max_attempts: int = 5,
    api_key: Optional[str] = None,
    on_batch_done: Optional[Callable[[list[Classification]], None]] = None,
) -> tuple[list[Classification], list[str]]:
    """
    Classify every tender in `items`.

    Returns (classifications, failed_tender_ids). `on_batch_done` is invoked
    with each batch's results as soon as they land, so callers can checkpoint
    to the database and survive an interrupted run.
    """
    import anthropic

    if not items:
        return [], []

    client = anthropic.Anthropic(
        **({"api_key": api_key} if api_key else {}),
        max_retries=0,  # retries/backoff are handled explicitly above
    )

    batches = _chunk(items, batch_size)
    logger.info(
        "Classifying %d tenders in %d batches of up to %d (model=%s, effort=%s, workers=%d)",
        len(items), len(batches), batch_size, model, effort, workers,
    )

    lock = threading.Lock()
    all_results: list[Classification] = []
    failed_ids: list[str] = []
    done = 0

    def attempt(chunk: list[dict]) -> tuple[list[Classification], list[dict]]:
        """One request. Returns (classifications, items the model did not answer)."""
        try:
            payload = _call_with_retry(
                client,
                model=model,
                effort=effort,
                max_tokens=max_tokens,
                items=chunk,
                max_attempts=max_attempts,
            )
        except Exception as exc:  # noqa: BLE001 — one bad request must not kill the run
            logger.error("Request for %d tender(s) failed permanently: %s", len(chunk), exc)
            return [], list(chunk)

        results = _parse_batch(payload, chunk)
        answered = {r.tender_id for r in results}
        return results, [item for item in chunk if item["id"] not in answered]

    def run_batch(index_and_batch: tuple[int, list[dict]]) -> None:
        nonlocal done
        index, batch = index_and_batch

        results, unanswered = attempt(batch)

        # A structured-output array has no enforceable minimum length, so the
        # model can return fewer entries than it was given. Rather than write
        # those tenders off, re-ask for each one on its own — a single-item
        # request has nothing to truncate.
        if unanswered and len(batch) > 1:
            logger.info(
                "Batch %d: model answered %d/%d — re-asking for %d tender(s) individually",
                index + 1, len(results), len(batch), len(unanswered),
            )
            for item in list(unanswered):
                single_results, single_missing = attempt([item])
                results.extend(single_results)
                if not single_missing:
                    unanswered.remove(item)

        missing = [item["id"] for item in unanswered]

        with lock:
            all_results.extend(results)
            failed_ids.extend(missing)
            done += 1
            logger.info(
                "Batch %d/%d complete — %d classified, %d failed (%d/%d tenders done)",
                done, len(batches), len(results), len(missing),
                len(all_results) + len(failed_ids), len(items),
            )
            if on_batch_done and results:
                on_batch_done(results)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(run_batch, enumerate(batches)))

    return all_results, failed_ids
