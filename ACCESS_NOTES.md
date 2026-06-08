# Phase-0 Discovery Notes — bids&tenders.ca Platform

## Platform Overview

All 17 CMW source municipalities run the **eSolutionsGroup bids&tenders.ca** SaaS platform.
One collector handles all 17 sources; only `base_url` differs.

---

## Confirmed API (verified on vaughan.bidsandtenders.ca, 2026-06-08)

### Step 1 — GET listing page (obtain session cookies + CSRF token)

```
GET https://{municipality}.bidsandtenders.ca/Module/Tenders/en
```

The HTML response contains two things needed for Step 2:

1. **CSRF token** — hidden input field in the page:
   ```html
   <input name="__RequestVerificationToken" value="a8-tzIg7_..." type="hidden" />
   ```

2. **MODULE_GUID** — the per-municipality module instance ID, embedded in the page
   HTML (form action or JS variable), e.g.:
   ```
   /Module/Tenders/en/Tender/Search/83b40e99-2f9a-4b20-9444-9cc522b4c6f5
   ```
   Vaughan's GUID: `83b40e99-2f9a-4b20-9444-9cc522b4c6f5`
   Each municipality has a different GUID. Discovered GUIDs are cached in
   `data/module_endpoints.yaml` so the listing page is only fetched once per
   municipality (once cached, only the search POST is needed).

### Step 2 — POST search

```
POST https://{municipality}.bidsandtenders.ca/Module/Tenders/en/Tender/Search/{MODULE_GUID}
     ?status=Open&limit=100&start=0&dir=ASC&from=&to=&sort=DateClosing+ASC%2CId

Content-Type: application/x-www-form-urlencoded

Body (form-encoded, NOT JSON):
  status=Open
  limit=100
  start=0
  dir=ASC
  from=
  to=
  sort=DateClosing ASC,Id
  __RequestVerificationToken={TOKEN}
```

- **Auth**: none — public endpoint confirmed accessible logged out
- **Pagination**: offset-based via `start` (not page number). `start=0`, `start=100`, etc.

### Response shape

```json
{
  "success": true,
  "data": [ ... ],
  "total": 16
}
```

### Confirmed field names (from `data` array items)

| Field                 | Type          | Notes                                                    |
|-----------------------|---------------|----------------------------------------------------------|
| `Id`                  | GUID string   | Platform tender ID; used as dedup key                    |
| `Title`               | string        | Includes ref prefix: `"T26-180 - Some Title"`            |
| `Status`              | string        | `"Open"`, `"Closed"`, `"Awarded"`                        |
| `Description`         | HTML string   | **Boilerplate** — "Only Online Submissions..." (see note)|
| `DateAvailable`       | `/Date(ms)/`  | ASP.NET JSON date, Unix milliseconds                     |
| `DateClosing`         | `/Date(ms)/`  | Closing date/time                                        |
| `DateClosingDisplay`  | string        | Human-readable, e.g. `"Mon Jun 8, 2026 3:00:00 PM"`     |
| `DaysLeft`            | int           | Days until closing (calculated server-side)              |
| `Scope`               | string        | Always `"Public"` — not a useful category field          |

**Fields NOT present at listing level:**
- No `referenceNumber` — reference prefix is embedded in `Title`, parsed via regex
- No `category` / `TenderType`
- No `detailUrl` — constructed from `Id`

### ⚠️ Description is boilerplate

Every tender's listing-level `Description` is just:
> "Only Online Submissions will be Accepted for this Tender"

The actual scope of work lives **only on the detail page**. The collector fetches
each detail page to get real matchable text.

---

## Detail page

**Confirmed URL pattern:**
```
GET https://{municipality}.bidsandtenders.ca/Module/Tenders/en/Tender/Detail/{tender_Id}
```
Example: `https://vaughan.bidsandtenders.ca/Module/Tenders/en/Tender/Detail/7184877f-3ef6-4fe5-a4fc-0a7c854dcfe6`

Note: singular `Detail`, not `Details`.

---

## robots.txt

`GET /robots.txt` returned HTTP 403 from the automated environment. Load manually
in a browser and record here before going live.

Provisional stance: **polite crawl of public listing + detail pages only**, per
`config/settings.yaml` rate limits.

---

## Generalization

All 17 municipalities use the same platform. The endpoint pattern, field names,
CSRF token mechanism, and `/Date(ms)/` format apply to all 17. The only
per-municipality variable is the MODULE_GUID.

---

## Per-site Quirk Log

| Source ID | Quirk | Resolution |
|-----------|-------|------------|
| *(none yet — add here as each site is verified)* | | |

---

## Remaining Verification Checklist

- [x] **Endpoint URL and method** — `POST /Module/Tenders/en/Tender/Search/{GUID}`
- [x] **POST body format** — form-encoded (not JSON), includes CSRF token
- [x] **Response shape** — `{"success": true, "data": [...], "total": N}`
- [x] **Field names** — `Id`, `Title`, `Status`, `Description`, `DateAvailable`, `DateClosing`
- [x] **Date format** — `/Date(ms)/` Unix milliseconds
- [x] **Detail URL** — `GET /Module/Tenders/en/Tender/Detail/{Id}` (singular)
- [ ] **robots.txt** — load in browser, record contents
- [ ] **Second municipality** — spot-check Brampton to confirm MODULE_GUID differs
      but structure is identical
- [ ] **Pagination** — find a municipality with >100 open tenders, confirm `start=100`
      works correctly
- [ ] **Detail page HTML structure** — confirm which CSS selector contains the
      scope/description text (update `_fetch_detail_description` if needed)
