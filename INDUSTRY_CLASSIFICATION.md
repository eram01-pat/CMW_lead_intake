# Tender Industry Classification

On-demand analysis that reads every tender already in the database, asks Claude
which **industry** each one belongs to (painting, HVAC, hardscaping, roofing, …),
and publishes an Excel summary of how many tenders fall into each category.

This is a separate tool from the daily monitor. It is **not** part of the
scheduled pipeline and does not affect it.

---

## What it does not touch

This was built to be provably isolated from the production pipeline:

| Guarantee | How |
|---|---|
| The daily monitor is unchanged | No existing file was modified — every file here is new |
| No schedule | `classify_industries.yml` has `workflow_dispatch` only, no `cron` |
| The `tenders` table is read-only | This tool only ever runs `SELECT` against it |
| Nothing is committed back | The workflow runs with `permissions: contents: read` |
| The dashboard is untouched | Nothing here writes to `docs/index.html` |
| No shared code path | `src/classification/` imports nothing from `src/storage`, `src/matching`, or `src/collectors` |

The only thing it writes to the database is its own table, `tender_industry`,
which the pipeline never reads.

---

## Running it

**In GitHub Actions (the normal way):**

Actions → **Classify Tenders by Industry** → *Run workflow*.

It uses the existing `ANTHROPIC_API_KEY` and `DATABASE_URL` secrets. When the
run finishes:

- the summary tables appear on the run's own page (**Summary** section), and
- `tender_industries.xlsx` is attached as a downloadable artifact, kept 90 days.

Workflow inputs (all optional):

| Input | Default | Notes |
|---|---|---|
| `model` | `claude-opus-5` | `claude-sonnet-5` / `claude-haiku-4-5` are cheaper and faster |
| `effort` | `low` | Reasoning depth. Classification is shallow work; raise only if quality disappoints |
| `status` | *(blank)* | e.g. `Open` to skip closed/awarded tenders. Blank means all |
| `limit` | `0` | Cap tenders classified this run. Use a small number for a trial run first |
| `batch_size` | `25` | Tenders per API request |
| `workers` | `4` | Concurrent requests. Lower it if you hit rate limits |
| `force` | `false` | Re-classify everything, ignoring cached results |
| `dry_run` | `false` | Report what *would* be classified; makes no API calls |

**Locally:**

```bash
pip install -r requirements-classify.txt
export DATABASE_URL="postgres://..."
export ANTHROPIC_API_KEY="sk-ant-..."

python classify_industries.py --dry-run          # see what would run
python classify_industries.py --limit 50         # small trial
python classify_industries.py                    # full run
```

### Suggested first run

Do a `dry_run` to confirm the tender count, then a run with `limit` set to
`100`. Open the workbook's **All Tenders** sheet and read the `Industry` and
`Why` columns for a few rows. If the categories look right, run it again with
no limit — the 100 already done are cached and are not paid for twice.

---

## The output workbook

| Sheet | Contents |
|---|---|
| **Summary by Industry** | The headline: every industry with its tender count and share of the total |
| **Summary by Group** | The same rolled up into 9 broad groups |
| **Demand by Sector** | The demand view: consulting and design tenders credited to the subject they are about (an EA for a watermain counts as water infrastructure, not consulting), with the direct and re-attributed counts shown separately |
| **Group by Source** | Cross-tab of issuing municipality/board against group |
| **Industry vs CMW Decision** | Each industry against the pipeline's existing yes/maybe/no relevance calls — shows which industries the daily monitor is actually surfacing |
| **Confidence** | How sure the model was, overall |
| **Secondary Industries** | Second trades on tenders that span two |
| **All Tenders** | Every tender with its industry, confidence, and one-line reason |
| **Run Info** | Model, effort, timings, counts, taxonomy version |

---

## How the classification works

Each tender is sent to Claude with its title, bid categories, issuer, and
description, in batches of 25 per request, several requests at a time. The
response is constrained by a JSON schema whose `industry` field is an enum of
the taxonomy, so the model cannot return a category that does not exist. The
shared system prompt is marked for prompt caching, so the taxonomy is only
paid for in full once.

### Accuracy expectations

As `ACCESS_NOTES.md` records, the `Description` field on this platform is
boilerplate at both listing and detail level — the real scope of work is inside
PDF bid documents behind a document fee. **So classification is title-driven.**

That works well for descriptive titles ("Roof Replacement at Public Works
Yard") and poorly for opaque ones. Every row therefore carries:

- **`Confidence`** — `high` (title names the work), `medium` (strongly implied),
  `low` (inferred from thin wording)
- **`Why`** — the one-line reason the model gave

Filter the **All Tenders** sheet to `low` confidence to see where the source
data, not the model, is the limiting factor. A handful of tenders will land in
`Other / Unclassified`; those are titles with no usable signal at all.

---

## Caching and cost

Every classification is cached in `tender_industry`, keyed by tender ID and
taxonomy version. A re-run only pays for tenders that are new or previously
failed, so running it again after the next daily crawl costs almost nothing.
Results are checkpointed to the database after each batch, so an interrupted
run loses at most one batch.

Failed batches are deliberately **not** cached — re-running retries them.

---

### Primary vs. secondary: two different questions

The primary industry answers **who performs the work** — an engineering firm wins
a watermain environmental assessment, not a pipe contractor. That is the right
axis for bid screening, but it hides subject matter: roughly a third of the
"Engineering & Design Consulting" bucket is water and sewer work.

`secondary_industry` carries the subject where the tender names one, and the
**Demand by Sector** sheet uses it to re-attribute consulting and design tenders
to what they are actually about. Use *Summary by Industry* to see who competes
for the work, and *Demand by Sector* to see where the demand sits.

The categories treated as re-attributable are listed in `_REATTRIBUTABLE` in
`src/classification/report.py`. Changing that list only affects report
generation — no re-classification and no API calls.

## Changing the categories

The taxonomy lives in `src/classification/taxonomy.py` as a `group → industries`
mapping. To add, remove, or rename a category:

1. Edit `TAXONOMY`.
2. Bump `TAXONOMY_VERSION`.

Bumping the version invalidates the cache, so the next run re-classifies
everything against the revised list. Cached rows from the old version are left
in place rather than deleted.
