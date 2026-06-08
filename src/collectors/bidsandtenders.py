"""
Parameterized collector for the bids&tenders.ca (eSolutionsGroup) platform.

All 17 municipalities share this one collector — only base_url differs.

Confirmed API (verified on vaughan.bidsandtenders.ca, 2026-06-08):

  Step 1 — GET {base_url}/Module/Tenders/en
           Obtain session cookies + CSRF token (__RequestVerificationToken)
           from the hidden input in the HTML. Also extract the MODULE_GUID
           from the form action or a JS variable in the page source.

  Step 2 — POST {base_url}/Module/Tenders/en/Tender/Search/{MODULE_GUID}
                ?status=Open&limit=100&start=0&dir=ASC&sort=DateClosing%20ASC,Id
           Content-Type: application/x-www-form-urlencoded
           Body: status=Open&limit=100&start=0&dir=ASC&from=&to=
                 &sort=DateClosing+ASC%2CId
                 &__RequestVerificationToken={TOKEN}

  Response: {"success": true, "data": [...], "total": N}
  Dates: /Date(ms)/ — Unix milliseconds (ASP.NET JSON date format)
  Descriptions: boilerplate at listing level; real scope is on the detail page.

See ACCESS_NOTES.md for full discovery findings.
"""

import hashlib
import logging
import re
import time
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.storage.models import Tender

logger = logging.getLogger(__name__)

_LISTING_PATH = "/Module/Tenders/en"
_DETAIL_PATH  = "/Module/Tenders/en/Tender/Detail"    # confirmed: singular "Detail"
_CACHE_FILE   = "data/module_endpoints.yaml"
_PAGE_LIMIT   = 100

# Listing-level Description is just this boilerplate
_BOILERPLATE_RE = re.compile(r"only\s+online\s+submissions", re.IGNORECASE)

# Reference prefix in Title: "T26-180 - Some Title" → "T26-180"
_REF_PREFIX_RE = re.compile(r"^([A-Z]{1,8}\d{2}-\d{2,5}[A-Z]?)\s*[-–]\s*", re.IGNORECASE)

# GUID pattern
_GUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


# ── Date parsing ──────────────────────────────────────────────────────────────

def _parse_aspnet_date(value: Any) -> Optional[date]:
    if not value:
        return None
    s = str(value)
    m = re.search(r"/Date\((-?\d+)(?:[+-]\d+)?\)/", s)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except ValueError:
            continue
    return None


# ── HTML helpers ──────────────────────────────────────────────────────────────

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str):
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(p.strip() for p in self._parts if p.strip())


def _strip_html(html: str) -> str:
    if not html:
        return ""
    p = _HTMLStripper()
    p.feed(html)
    return p.get_text()


def _is_boilerplate(text: str) -> bool:
    return not text or bool(_BOILERPLATE_RE.search(text))


def _extract_csrf_token(html: str) -> Optional[str]:
    """Extract __RequestVerificationToken from a hidden input."""
    m = re.search(
        r'<input[^>]+name="__RequestVerificationToken"[^>]+value="([^"]+)"',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1)
    # Some ASP.NET versions put it in a meta tag
    m = re.search(
        r'<meta[^>]+name="__RequestVerificationToken"[^>]+content="([^"]+)"',
        html, re.IGNORECASE,
    )
    return m.group(1) if m else None


def _extract_module_guid(html: str, base_url: str) -> Optional[str]:
    """
    Find the MODULE_GUID embedded in the listing page.
    Looks for /Tender/Search/{GUID} in form action attributes or JS variables.
    """
    # Form action containing the search path
    m = re.search(
        r'/Module/Tenders/en/Tender/Search/(' + _GUID_RE.pattern + r')',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1)
    # JavaScript variable assignment
    m = re.search(
        r'["\'](?:/[^"\']*)?/Tender/Search/(' + _GUID_RE.pattern + r')["\']',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1)
    return None


# ── Session / requests helpers ────────────────────────────────────────────────

def _make_session(user_agent: str) -> requests.Session:
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(total=0, raise_on_status=False)))
    s.mount("http://",  HTTPAdapter(max_retries=Retry(total=0, raise_on_status=False)))
    s.headers.update({
        "User-Agent":      user_agent,
        "Accept-Language": "en-CA,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    })
    return s


def _get_with_retry(
    session: requests.Session,
    url: str,
    timeout: int,
    max_retries: int,
    backoff_base: float,
) -> requests.Response:
    last: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            r = session.get(url, timeout=timeout,
                            headers={"Accept": "text/html,application/xhtml+xml,*/*"})
            if r.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                _backoff(attempt, backoff_base, r.status_code, url)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last = exc
            if attempt < max_retries:
                _backoff(attempt, backoff_base, None, url)
    raise RuntimeError(f"GET {url} failed after {max_retries + 1} attempts: {last}")


def _post_with_retry(
    session: requests.Session,
    url: str,
    data: dict,
    timeout: int,
    max_retries: int,
    backoff_base: float,
) -> requests.Response:
    last: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            r = session.post(
                url, data=data, timeout=timeout,
                headers={"Accept": "application/json, */*",
                         "Content-Type": "application/x-www-form-urlencoded"},
            )
            if r.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                _backoff(attempt, backoff_base, r.status_code, url)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last = exc
            if attempt < max_retries:
                _backoff(attempt, backoff_base, None, url)
    raise RuntimeError(f"POST {url} failed after {max_retries + 1} attempts: {last}")


def _backoff(attempt: int, base: float, status: Optional[int], url: str) -> None:
    wait = base * (2 ** attempt)
    logger.warning("HTTP %s from %s — backing off %.1fs", status or "err", url, wait)
    time.sleep(wait)


# ── Module GUID + CSRF discovery ──────────────────────────────────────────────

def _load_cache() -> dict:
    p = Path(_CACHE_FILE)
    return yaml.safe_load(p.read_text()) if p.exists() else {}


def _save_cache(cache: dict) -> None:
    Path(_CACHE_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(_CACHE_FILE).write_text(yaml.dump(cache, default_flow_style=False))


def _load_listing_page(
    session: requests.Session,
    base_url: str,
    source_id: str,
    timeout: int,
    max_retries: int,
    backoff_base: float,
) -> tuple[str, str]:
    """
    GET the tenders listing page. Returns (csrf_token, module_guid).
    Caches the module_guid in data/module_endpoints.yaml.
    Raises RuntimeError if either cannot be extracted.
    """
    listing_url = f"{base_url}{_LISTING_PATH}"
    session.headers["Referer"] = base_url

    r = _get_with_retry(session, listing_url, timeout, max_retries, backoff_base)
    html = r.text

    csrf = _extract_csrf_token(html)
    if not csrf:
        raise RuntimeError(
            f"Could not find __RequestVerificationToken in {listing_url}. "
            "The page structure may have changed."
        )

    # Check cache first; extract from HTML otherwise
    cache = _load_cache()
    guid = cache.get(source_id) or _extract_module_guid(html, base_url)
    if not guid:
        raise RuntimeError(
            f"Could not find MODULE_GUID in {listing_url}. "
            "Check ACCESS_NOTES.md and update _extract_module_guid() if needed."
        )

    if source_id not in cache:
        cache[source_id] = guid
        _save_cache(cache)
        logger.info("%s: cached module GUID %s", source_id, guid)

    session.headers["Referer"] = listing_url
    return csrf, guid


# ── Core search + pagination ──────────────────────────────────────────────────

def _search_page(
    session: requests.Session,
    base_url: str,
    guid: str,
    csrf: str,
    start: int,
    timeout: int,
    max_retries: int,
    backoff_base: float,
) -> tuple[list[dict], int]:
    url = (
        f"{base_url}{_LISTING_PATH}/Tender/Search/{guid}"
        f"?status=Open&limit={_PAGE_LIMIT}&start={start}"
        f"&dir=ASC&from=&to=&sort=DateClosing+ASC%2CId"
    )
    data = {
        "status": "Open",
        "limit":  str(_PAGE_LIMIT),
        "start":  str(start),
        "dir":    "ASC",
        "from":   "",
        "to":     "",
        "sort":   "DateClosing ASC,Id",
        "__RequestVerificationToken": csrf,
    }
    r = _post_with_retry(session, url, data, timeout, max_retries, backoff_base)

    ct = r.headers.get("Content-Type", "")
    if "json" not in ct:
        raise RuntimeError(f"Expected JSON, got {ct!r} from {url}")

    body = r.json()
    return body.get("data") or [], int(body.get("total") or 0)


def _fetch_all_pages(
    session: requests.Session,
    base_url: str,
    guid: str,
    csrf: str,
    timeout: int,
    rate_limit: float,
    max_retries: int,
    backoff_base: float,
    max_per_source: int,
) -> list[dict]:
    all_items: list[dict] = []
    start = 0

    while True:
        items, total = _search_page(
            session, base_url, guid, csrf,
            start=start, timeout=timeout,
            max_retries=max_retries, backoff_base=backoff_base,
        )
        all_items.extend(items)

        if max_per_source and len(all_items) >= max_per_source:
            return all_items[:max_per_source]
        if not items or len(all_items) >= total or len(items) < _PAGE_LIMIT:
            break

        start += len(items)
        time.sleep(rate_limit)

    return all_items


# ── Detail page ───────────────────────────────────────────────────────────────

def _fetch_detail_description(
    session: requests.Session,
    detail_url: str,
    timeout: int,
    rate_limit: float,
) -> str:
    """Fetch the tender detail page; return plain-text description (best-effort)."""
    try:
        r = session.get(detail_url, timeout=timeout,
                        headers={"Accept": "text/html,*/*"})
        if r.status_code != 200:
            return ""
        time.sleep(rate_limit)
        html = r.text
        # Try common eSolutionsGroup description container patterns
        for pat in [
            r'id="[^"]*[Dd]escription[^"]*"[^>]*>(.*?)</(?:div|section|article)',
            r'class="[^"]*[Dd]escription[^"]*"[^>]*>(.*?)</(?:div|section)',
            r'class="[^"]*[Ss]cope[^"]*"[^>]*>(.*?)</(?:div|section)',
            r'class="[^"]*tender-detail[^"]*"[^>]*>(.*?)</(?:div|section)',
        ]:
            m = re.search(pat, html, re.DOTALL | re.IGNORECASE)
            if m:
                text = _strip_html(m.group(1))
                if text and not _is_boilerplate(text) and len(text) > 50:
                    return text
        return ""
    except Exception as exc:
        logger.debug("Detail fetch error %s: %s", detail_url, exc)
        return ""


# ── Item parsing ──────────────────────────────────────────────────────────────

def _tender_id(source_id: str, platform_id: str) -> str:
    return hashlib.sha256(f"{source_id}:{platform_id}".encode()).hexdigest()[:32]


def _extract_ref_no(title: str) -> str:
    m = _REF_PREFIX_RE.match(title)
    return m.group(1).upper() if m else ""


def _parse_item(
    item: dict,
    source_id: str,
    source_name: str,
    base_url: str,
    session: requests.Session,
    timeout: int,
    rate_limit: float,
    fetch_details: bool,
) -> Optional[Tender]:
    try:
        platform_id = str(item.get("Id") or "").strip()
        if not platform_id:
            return None

        title = str(item.get("Title") or "").strip()
        if not title:
            return None

        ref_no     = _extract_ref_no(title)
        detail_url = f"{base_url}{_DETAIL_PATH}/{platform_id}"

        description = _strip_html(str(item.get("Description") or ""))
        if _is_boilerplate(description) and fetch_details:
            description = _fetch_detail_description(session, detail_url, timeout, rate_limit)

        return Tender(
            id=_tender_id(source_id, platform_id),
            source_id=source_id,
            source_name=source_name,
            title=title,
            description=description,
            category="",   # not present at listing level on this platform
            reference_no=ref_no,
            detail_url=detail_url,
            status=str(item.get("Status") or "Open").strip(),
            posted_date=_parse_aspnet_date(item.get("DateAvailable")),
            closing_date=_parse_aspnet_date(item.get("DateClosing")),
            raw=item,
        )
    except Exception as exc:
        logger.warning("Failed to parse item %r: %s", item.get("Id"), exc)
        return None


# ── Public entry point ────────────────────────────────────────────────────────

def collect(
    source: dict,
    user_agent: str,
    rate_limit_seconds: float = 3.0,
    max_retries: int = 3,
    backoff_base_seconds: float = 5.0,
    timeout_seconds: int = 30,
    max_per_source: int = 0,
    fetch_detail_pages: bool = True,
) -> list[Tender]:
    """
    Collect open tenders from one bids&tenders.ca municipality.
    Returns [] on unrecoverable error so the pipeline continues with other sources.
    """
    source_id   = source["id"]
    source_name = source["name"]
    base_url    = source["base_url"].rstrip("/")

    logger.info("Collecting %s", source_name)

    session = _make_session(user_agent)

    try:
        csrf, guid = _load_listing_page(
            session, base_url, source_id,
            timeout=timeout_seconds,
            max_retries=max_retries,
            backoff_base=backoff_base_seconds,
        )
    except Exception as exc:
        logger.error("%s: listing page load failed: %s — skipping", source_name, exc)
        return []

    try:
        raw_items = _fetch_all_pages(
            session=session,
            base_url=base_url,
            guid=guid,
            csrf=csrf,
            timeout=timeout_seconds,
            rate_limit=rate_limit_seconds,
            max_retries=max_retries,
            backoff_base=backoff_base_seconds,
            max_per_source=max_per_source,
        )
    except Exception as exc:
        logger.error("%s: search failed: %s — skipping", source_name, exc)
        return []

    tenders: list[Tender] = []
    for item in raw_items:
        t = _parse_item(
            item=item,
            source_id=source_id,
            source_name=source_name,
            base_url=base_url,
            session=session,
            timeout=timeout_seconds,
            rate_limit=rate_limit_seconds,
            fetch_details=fetch_detail_pages,
        )
        if t:
            tenders.append(t)

    logger.info("%s: %d tenders collected", source_name, len(tenders))
    return tenders
