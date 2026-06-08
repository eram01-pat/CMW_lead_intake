"""
Parameterized collector for the bids&tenders.ca (eSolutionsGroup) platform.

All 17 municipalities share this one collector — differences are only in base_url.

Discovery notes (see ACCESS_NOTES.md for full findings):
  The platform is an ASP.NET SPA that loads tender listings via an XHR POST to:
    POST {base_url}/Module/Tenders/en/Search
  with a JSON body containing filters and pagination. The response is JSON.
  If this API call fails (structure change, new protection), the collector
  falls back to Playwright headless rendering and HTML parsing.

Etiquette:
  - Reads only public listing and detail pages, logged out.
  - Respects per-host rate limits from settings.yaml.
  - Identifies itself via User-Agent.
  - Backs off exponentially on 429 / 5xx.
  - Fails a single source gracefully; other sources continue.
"""

import hashlib
import json
import logging
import time
from datetime import date, datetime
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.storage.models import Tender

logger = logging.getLogger(__name__)

# ── Known API endpoint (discovered via browser devtools on the platform) ──────
_SEARCH_PATH = "/Module/Tenders/en/Search"
_PAGE_SIZE = 100  # fetch up to 100 per page; paginate if needed


def _make_session(user_agent: str, timeout: int) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=0,  # we handle retries ourselves for better logging
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "en-CA,en;q=0.9",
    })
    session.request_timeout = timeout  # stored for use in requests
    return session


def _tender_id(source_id: str, reference_no: str, detail_url: str) -> str:
    """Stable, dedup-safe primary key: hash of source + (ref_no if present else url)."""
    key = f"{source_id}:{reference_no if reference_no else detail_url}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(value)[:19], fmt).date()
        except ValueError:
            continue
    logger.debug("Could not parse date: %r", value)
    return None


def _normalize_status(raw: str) -> str:
    mapping = {
        "open": "Open",
        "active": "Open",
        "closed": "Closed",
        "awarded": "Awarded",
        "cancelled": "Cancelled",
    }
    return mapping.get(str(raw).strip().lower(), str(raw).strip())


# ── API strategy ──────────────────────────────────────────────────────────────

def _fetch_via_api(
    session: requests.Session,
    base_url: str,
    source_id: str,
    source_name: str,
    rate_limit: float,
    max_retries: int,
    backoff_base: float,
    timeout: int,
    max_per_source: int,
) -> list[Tender]:
    """Attempt the JSON XHR endpoint. Raises RuntimeError if the API is unavailable."""
    endpoint = base_url.rstrip("/") + _SEARCH_PATH
    tenders: list[Tender] = []
    page = 1

    while True:
        payload = {
            "pageNumber": page,
            "pageSize": _PAGE_SIZE,
            "status": "Open",
            "orderBy": "PostingDate",
            "orderDirection": "DESC",
        }
        resp = _post_with_retry(
            session, endpoint, json=payload,
            max_retries=max_retries, backoff_base=backoff_base, timeout=timeout,
        )

        content_type = resp.headers.get("Content-Type", "")
        if "application/json" not in content_type and "text/json" not in content_type:
            raise RuntimeError(
                f"API returned non-JSON ({content_type}); "
                "will fall back to Playwright"
            )

        data = resp.json()

        # The platform wraps results in various shapes; try common ones.
        items = (
            data.get("tenders")
            or data.get("Tenders")
            or data.get("results")
            or data.get("Results")
            or data.get("data")
            or (data if isinstance(data, list) else [])
        )
        if not items:
            break

        for item in items:
            t = _parse_api_item(item, source_id, source_name, base_url)
            if t:
                tenders.append(t)
            if max_per_source and len(tenders) >= max_per_source:
                return tenders

        total = (
            data.get("totalCount")
            or data.get("TotalCount")
            or data.get("total")
            or 0
        )
        if not total or len(tenders) >= int(total) or len(items) < _PAGE_SIZE:
            break

        page += 1
        time.sleep(rate_limit)

    return tenders


def _parse_api_item(
    item: dict, source_id: str, source_name: str, base_url: str
) -> Optional[Tender]:
    """Map a raw API dict to a Tender. Returns None if the item is unparseable."""
    try:
        title = (
            item.get("title") or item.get("Title")
            or item.get("tenderTitle") or item.get("TenderTitle") or ""
        ).strip()
        if not title:
            return None

        ref_no = str(
            item.get("referenceNumber") or item.get("ReferenceNumber")
            or item.get("tenderNumber") or item.get("TenderNumber")
            or item.get("id") or item.get("Id") or ""
        ).strip()

        # Detail URL: may be a relative path or absolute
        raw_url = (
            item.get("url") or item.get("Url")
            or item.get("detailUrl") or item.get("DetailUrl")
            or item.get("link") or item.get("Link") or ""
        ).strip()
        detail_url = (
            raw_url if raw_url.startswith("http")
            else urljoin(base_url, raw_url) if raw_url
            else f"{base_url}/Module/Tenders/en/{ref_no}" if ref_no
            else base_url
        )

        description = (
            item.get("description") or item.get("Description")
            or item.get("scope") or item.get("Scope")
            or item.get("summary") or item.get("Summary") or ""
        ).strip()

        category = (
            item.get("category") or item.get("Category")
            or item.get("tenderType") or item.get("TenderType") or ""
        )
        if isinstance(category, dict):
            category = category.get("name") or category.get("Name") or ""

        posted = _parse_date(
            item.get("postedDate") or item.get("PostedDate")
            or item.get("openDate") or item.get("OpenDate")
            or item.get("issueDate") or item.get("IssueDate")
        )
        closing = _parse_date(
            item.get("closingDate") or item.get("ClosingDate")
            or item.get("dueDate") or item.get("DueDate")
            or item.get("closingDateTime") or item.get("ClosingDateTime")
        )

        raw_status = (
            item.get("status") or item.get("Status")
            or item.get("tenderStatus") or item.get("TenderStatus") or "Open"
        )
        status = _normalize_status(str(raw_status))

        tid = _tender_id(source_id, ref_no, detail_url)

        return Tender(
            id=tid,
            source_id=source_id,
            source_name=source_name,
            title=title,
            description=description,
            category=str(category),
            reference_no=ref_no,
            detail_url=detail_url,
            status=status,
            posted_date=posted,
            closing_date=closing,
            raw=item,
        )
    except Exception as exc:
        logger.warning("Failed to parse API item: %s — %r", exc, item)
        return None


# ── Playwright fallback ───────────────────────────────────────────────────────

def _fetch_via_playwright(
    base_url: str,
    source_id: str,
    source_name: str,
    user_agent: str,
    max_per_source: int,
) -> list[Tender]:
    """
    Headless Playwright fallback for when the JSON API is unavailable.
    Navigates to the tenders listing page, waits for the tender rows to render,
    then reads each row and its detail page.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install playwright && "
            "playwright install chromium"
        )

    listing_url = f"{base_url.rstrip('/')}/Module/Tenders/en"
    tenders: list[Tender] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=user_agent)
        page = context.new_page()

        logger.info("Playwright: loading %s", listing_url)
        page.goto(listing_url, wait_until="networkidle", timeout=30_000)

        # Capture XHR responses that look like tender listings
        api_data: list[dict] = []

        def handle_response(response):
            if "Tenders" in response.url and response.status == 200:
                content_type = response.headers.get("content-type", "")
                if "json" in content_type:
                    try:
                        body = response.json()
                        api_data.append(body)
                    except Exception:
                        pass

        page.on("response", handle_response)

        # Trigger a fresh load so we capture the XHR
        page.reload(wait_until="networkidle", timeout=30_000)

        if api_data:
            # We intercepted the JSON — process it the same as API strategy
            for payload in api_data:
                items = (
                    payload.get("tenders") or payload.get("Tenders")
                    or payload.get("results") or payload.get("data")
                    or (payload if isinstance(payload, list) else [])
                )
                for item in items:
                    t = _parse_api_item(item, source_id, source_name, base_url)
                    if t:
                        tenders.append(t)
                    if max_per_source and len(tenders) >= max_per_source:
                        break
        else:
            # Parse rendered HTML as fallback
            tenders = _parse_rendered_html(
                page, source_id, source_name, base_url, max_per_source
            )

        browser.close()

    return tenders


def _parse_rendered_html(
    page, source_id: str, source_name: str, base_url: str, max_per_source: int
) -> list[Tender]:
    """
    Parse tender rows from the rendered DOM when XHR interception yielded nothing.
    Selectors are based on the eSolutionsGroup bids&tenders.ca DOM structure.
    Update ACCESS_NOTES.md if selectors need adjustment per-site.
    """
    tenders: list[Tender] = []

    # Common row selectors on the bids&tenders.ca platform
    row_selectors = [
        "table.tenders-list tbody tr",
        ".tender-list-item",
        ".tender-row",
        "[data-tender-id]",
        ".bids-table tbody tr",
    ]

    rows = []
    for sel in row_selectors:
        rows = page.query_selector_all(sel)
        if rows:
            logger.debug("Playwright HTML: matched rows with selector %r", sel)
            break

    if not rows:
        logger.warning(
            "Playwright HTML: no rows found for %s — "
            "selectors may need updating (see ACCESS_NOTES.md)",
            base_url,
        )
        return tenders

    for row in rows:
        try:
            title_el = row.query_selector("a.tender-title, .tender-name a, td:nth-child(2) a")
            if not title_el:
                continue
            title = title_el.inner_text().strip()
            href = title_el.get_attribute("href") or ""
            detail_url = href if href.startswith("http") else urljoin(base_url, href)

            ref_el = row.query_selector(".reference-no, .tender-ref, td:nth-child(1)")
            ref_no = ref_el.inner_text().strip() if ref_el else ""

            close_el = row.query_selector(".closing-date, .tender-closing, td:nth-child(5)")
            closing = _parse_date(close_el.inner_text().strip() if close_el else None)

            status_el = row.query_selector(".status, .tender-status, td:nth-child(6)")
            status = _normalize_status(status_el.inner_text().strip() if status_el else "Open")

            tid = _tender_id(source_id, ref_no, detail_url)
            tenders.append(Tender(
                id=tid,
                source_id=source_id,
                source_name=source_name,
                title=title,
                description="",  # fetched on detail page pass if needed
                category="",
                reference_no=ref_no,
                detail_url=detail_url,
                status=status,
                posted_date=None,
                closing_date=closing,
                raw={"title": title, "href": href},
            ))
            if max_per_source and len(tenders) >= max_per_source:
                break
        except Exception as exc:
            logger.debug("Playwright HTML: row parse error: %s", exc)

    return tenders


# ── Retry wrapper ─────────────────────────────────────────────────────────────

def _post_with_retry(
    session: requests.Session,
    url: str,
    json: dict,
    max_retries: int,
    backoff_base: float,
    timeout: int,
) -> requests.Response:
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            resp = session.post(url, json=json, timeout=timeout)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                wait = backoff_base * (2 ** attempt)
                logger.warning(
                    "HTTP %s from %s; backing off %.1fs (attempt %d/%d)",
                    resp.status_code, url, wait, attempt + 1, max_retries + 1,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < max_retries:
                wait = backoff_base * (2 ** attempt)
                logger.warning("Request error %s; retrying in %.1fs", exc, wait)
                time.sleep(wait)
    raise RuntimeError(f"Failed after {max_retries + 1} attempts: {last_exc}")


# ── Public entry point ────────────────────────────────────────────────────────

def collect(
    source: dict,
    user_agent: str,
    rate_limit_seconds: float = 3.0,
    max_retries: int = 3,
    backoff_base_seconds: float = 5.0,
    timeout_seconds: int = 30,
    max_per_source: int = 0,
) -> list[Tender]:
    """
    Collect open tenders from a single bids&tenders.ca source.

    Returns a list of Tender objects. On error, logs and returns an empty list
    so the pipeline continues with remaining sources.
    """
    source_id = source["id"]
    source_name = source["name"]
    base_url = source["base_url"].rstrip("/")

    session = _make_session(user_agent, timeout_seconds)

    logger.info("Collecting %s (%s)", source_name, base_url)

    try:
        tenders = _fetch_via_api(
            session, base_url, source_id, source_name,
            rate_limit=rate_limit_seconds,
            max_retries=max_retries,
            backoff_base=backoff_base_seconds,
            timeout=timeout_seconds,
            max_per_source=max_per_source,
        )
        logger.info("%s: API strategy yielded %d tenders", source_name, len(tenders))
        return tenders

    except Exception as api_exc:
        logger.warning(
            "%s: API strategy failed (%s); falling back to Playwright",
            source_name, api_exc,
        )

    try:
        tenders = _fetch_via_playwright(
            base_url, source_id, source_name,
            user_agent=user_agent,
            max_per_source=max_per_source,
        )
        logger.info(
            "%s: Playwright strategy yielded %d tenders", source_name, len(tenders)
        )
        return tenders

    except Exception as pw_exc:
        logger.error(
            "%s: Both strategies failed. Playwright error: %s. "
            "Source skipped for this run.",
            source_name, pw_exc,
        )
        return []
