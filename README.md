# CMW Tender Monitor

Automated monitor for Canadian Mobile Wash (CMW) that watches 35 Ontario
public-sector procurement portals (municipalities, regions, and school
boards), uses Claude to judge whether each open tender is relevant to CMW's
services, and publishes a daily dashboard of relevant opportunities.

## What it does

1. **Collects** open tenders from 35 bids&tenders.ca portals — municipalities,
   regions, and school boards, all on the same eSolutionsGroup platform, so a
   single parameterized collector handles every site.
2. **Adjudicates** every new tender with Claude (`yes` / `no` / `maybe`) and
   caches the decision in the database, so each tender is judged exactly once
   and API cost stays flat as the database grows.
3. **Publishes** a static HTML dashboard to GitHub Pages — no server required —
   and optionally posts new matches to Slack.

## What it does NOT do

- No bidding, no form submission, no document downloads, no login
- Does not cover MERX, or the City of Ottawa / City of Toronto **municipal**
  portals (deliberately excluded). Note: Toronto DSB is a separate school-board
  portal and *is* covered.
- Only reads publicly available pages while logged out

---

## Architecture

```
collect (Playwright) → store (Neon Postgres) → LLM adjudicates each new tender → dashboard (GitHub Pages)
                                                          └→ Slack notification on new match
```

The collector loads each listing page in headless Chromium so the platform's
JavaScript runs, intercepts the AJAX search response, and parses the tender
JSON. Each municipality's search-endpoint GUID is discovered on first run and
cached in `data/module_endpoints.yaml`.

---

## Sources (35)

Defined in `config/sources.yaml`:

- **Regions (5):** Peel, York, Durham, Halton, Waterloo
- **Cities / Towns / Counties (20):** Vaughan, Brampton, Mississauga, Markham,
  Richmond Hill, Aurora, Newmarket, Whitby, Pickering, Ajax, Oshawa, Burlington,
  Oakville, Halton Hills, Hamilton, Niagara Falls, Kitchener, Waterloo, Guelph,
  Oxford County (Woodstock)
- **School Boards (10):** YRDSB, TDSB, DDSB, HDSB, HWDSB, WRDSB, YCDSB, DPCDSB,
  DCDSB, HWCDSB

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/eram01-pat/cmw_lead_intake.git
cd cmw_lead_intake
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure secrets / environment

The pipeline reads these from the environment (set as GitHub Actions secrets in CI):

| Secret              | Required | Purpose                                                  |
|---------------------|----------|----------------------------------------------------------|
| `DATABASE_URL`      | Yes      | Neon Postgres connection string (tender + decision store) |
| `ANTHROPIC_API_KEY` | Yes      | Claude relevance adjudication (run skips judging without it) |
| `SLACK_WEBHOOK_URL` | Optional | Posts new matches to Slack                               |

### 3. Enable GitHub Pages

In repository Settings → Pages → Source: **GitHub Actions**.

### 4. Run manually

```bash
# Full run (collect, adjudicate, build dashboard)
python pipeline.py

# Single source (for testing/debugging)
python pipeline.py --source vaughan

# Dry run (no DB writes / no dashboard deploy; still calls the LLM and logs matches)
python pipeline.py --dry-run
```

### Scheduled runs

`.github/workflows/monitor.yml` runs the pipeline automatically at
**6:00 AM Eastern on weekdays** (and on manual `workflow_dispatch`). After each
run it commits any newly discovered GUIDs in `data/module_endpoints.yaml`
(tagged `[skip ci]`).

---

## Configuration

| File                        | What to edit                                                 |
|-----------------------------|--------------------------------------------------------------|
| `config/sources.yaml`       | Add/remove a portal — one line per source                    |
| `config/settings.yaml`      | Crawl rate limits, LLM model/toggle, dashboard options       |

### Adding a source

Edit `config/sources.yaml` — add one line under the appropriate section:
```yaml
- {id: newcity, name: "City of New City", base_url: "https://newcity.bidsandtenders.ca"}
```
That's all. The collector discovers the endpoint GUID and handles the rest
automatically.

### Tuning relevance

Relevance is decided by Claude using the system prompt in
`src/matching/relevance.py`, which enumerates CMW's in-scope services and
explicit out-of-scope exclusions.

- **Too noisy:** tighten the OUT OF SCOPE list or decision rules in the prompt.
- **Missing real opportunities:** broaden the in-scope service list or relax the
  MAYBE rule.

---

## State persistence

Tenders and their cached LLM decisions live in **Neon Postgres**, reached via
the `DATABASE_URL` environment variable (`src/storage/db.py`). Nothing about the
database is committed to the repo — the only file written back after a run is
`data/module_endpoints.yaml` (the discovered GUID cache), committed with
`[skip ci]`.

---

## LLM relevance pass

Every new tender is sent to Claude exactly once (model set in
`config/settings.yaml`, default `claude-haiku-4-5`). The model answers
`yes` / `no` / `maybe` plus a one-line reason, and the decision is cached so
subsequent runs skip already-judged tenders.

- `yes` / `maybe` → surfaced on the dashboard (and Slack, if configured)
- `no` → stored but kept off the main feed

If `ANTHROPIC_API_KEY` is unset, the adjudication step is skipped and tenders
are stored without a decision.

---

## Discovery / troubleshooting

See `ACCESS_NOTES.md` for platform discovery notes and the verification checklist
that must be completed before first production run.

If a source breaks (structure change), it logs an error and continues with the
remaining sources. The pipeline exits with code 2 if any source failed (unless
`--ignore-errors` is passed, as it is in CI).

---

## Phase-2 upgrade paths (not built in v1)

- **Interactive dashboard**: mark tenders reviewed/dismissed, shared state across
  the team — requires a small backend (FastAPI) over the existing Postgres DB
- **Email digest**: daily summary of new matches — thin add-on over the same data
- **LLM detail-page enrichment**: fetch tender detail pages for tenders that only
  had a listing-level description
