# Phase-0 Discovery Notes — bids&tenders.ca Platform

## Platform Overview

All 17 CMW source municipalities run the **eSolutionsGroup bids&tenders.ca** SaaS platform.
This is the key simplification: one collector handles all 17 sources.

Platform URL pattern:
```
https://{municipality}.bidsandtenders.ca/Module/Tenders/en
```

---

## Network Discovery Findings

> **Note:** Live network probing from the CI/build environment was blocked by the
> Anthropic egress gateway during development. The findings below are based on:
> 1. Public documentation and community analysis of the eSolutionsGroup platform
> 2. Standard ASP.NET SPA patterns used by this platform generation
> 3. **Must be verified by a developer with unrestricted browser access before the
>    collector is run in production.** See the verification checklist below.

### robots.txt

`/robots.txt` returned HTTP 403 during automated probing. This does not mean the
site prohibits crawling — 403 on robots.txt is common for SaaS platforms that serve
the file only to specific user agents or from non-proxied connections. A developer
should manually load `https://vaughan.bidsandtenders.ca/robots.txt` in a browser and
record its contents here.

Provisional stance until verified: **polite crawl of public listing pages only**,
per the etiquette rules in `config/settings.yaml`.

---

## Tender Listing: Expected Structure

### Primary strategy — JSON XHR endpoint

Based on the eSolutionsGroup platform architecture, the tender listing page renders
via a React SPA that fetches tender data from a background XHR/fetch call:

```
POST https://{municipality}.bidsandtenders.ca/Module/Tenders/en/Search
Content-Type: application/json

{
  "pageNumber": 1,
  "pageSize": 100,
  "status": "Open",
  "orderBy": "PostingDate",
  "orderDirection": "DESC"
}
```

Expected response shape (may vary; update if different):
```json
{
  "tenders": [...],
  "totalCount": 42
}
```

Each tender object is expected to contain:
| Field                    | Notes                                      |
|--------------------------|--------------------------------------------|
| `title` / `TenderTitle`  | Tender name                                |
| `referenceNumber`        | Unique reference (used as dedup key)       |
| `description` / `scope`  | Tender description / scope of work         |
| `category` / `TenderType`| Category string                            |
| `postedDate` / `issueDate`| ISO-8601 date                             |
| `closingDate` / `dueDate`| ISO-8601 date (with time for precision)    |
| `status`                 | Open / Closed / Awarded                    |
| `url` / `detailUrl`      | Relative or absolute URL to detail page    |

The collector (`src/collectors/bidsandtenders.py`) tries multiple field-name variants
to handle differences across municipalities.

### Fallback strategy — Playwright headless rendering

If the XHR endpoint returns non-JSON (structure change, WAF, etc.), the collector
falls back to Playwright, which:
1. Loads the page in a headless Chromium browser
2. Listens for XHR responses matching `/Tenders/` to intercept the API call
3. If no XHR is captured, falls back to parsing the rendered HTML DOM

Known Playwright DOM selectors to try (update if the platform changes):
```
table.tenders-list tbody tr
.tender-list-item
.tender-row
[data-tender-id]
.bids-table tbody tr
```

---

## Verification Checklist (run before first production deployment)

A developer must open `https://vaughan.bidsandtenders.ca/Module/Tenders/en` in a
browser with DevTools open (Network tab) and confirm or correct the following:

- [ ] **API endpoint URL** — confirm `POST /Module/Tenders/en/Search` or record the
  actual endpoint path
- [ ] **Request body** — record the exact JSON fields and values used
- [ ] **Response shape** — record field names for title, ref#, dates, status, URL
- [ ] **Pagination** — confirm `pageNumber`/`pageSize` or record actual param names;
  confirm `totalCount` or the actual total field name
- [ ] **`robots.txt`** — load in browser, record contents here, confirm no restrictions
  on the `/Module/Tenders/en` path
- [ ] **Rate limiting** — note if any 429s appear at what request rate
- [ ] **Auth requirement** — confirm listing page is accessible logged out (expected: yes)
- [ ] **Per-site quirks** — note any municipalities where the endpoint differs from Vaughan

Update this file and `src/collectors/bidsandtenders.py` with findings.

---

## Per-Site Quirk Log

*Add notes here as each municipality is verified.*

| Source ID    | Quirk                                  | Resolution              |
|--------------|----------------------------------------|-------------------------|
| *(none yet)* |                                        |                         |

---

## Generalization Note

All 17 municipalities use the same eSolutionsGroup SaaS instance (same platform version,
same subdomain pattern). The API endpoint, field names, and pagination should be identical
across all 17. If one site has a quirk, it is likely a configuration difference in their
portal instance, not a platform difference. Special-casing should be rare.
