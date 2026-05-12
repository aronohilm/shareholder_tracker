# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Scrapes Icelandic stock exchange (NASDAQ Iceland) company IR pages daily, detects changes in their top-shareholder lists, and sends email notifications. Runs as a scheduled GitHub Actions workflow (weekdays at 08:00 UTC). State is committed back to `state.json` after each run.

## Commands

```bash
# Activate virtualenv
source .venv/bin/activate

# Install dependencies (static scraping only)
pip install -r requirements.txt

# Install Playwright (needed for JS-rendered pages)
pip install playwright && playwright install chromium

# Run full scan (saves state, sends notifications)
python main.py

# Dry run — no state save, no notifications
python main.py --dry-run

# Scan a single company by ticker
python main.py --company ARION

# Debug a JS-heavy company — saves raw fetched HTML to disk
python main.py --company KALD --debug-html kald_debug.html

# Write all detected changes to JSON
python main.py --output-json changes.json && python summarize.py

# Local viewer — serves shareholder.html at http://localhost:8765/shareholder.html
# Watches aliases.yml and rebuilds aliases.json automatically on save
python dev_server.py

# Stop the dev server
lsof -ti :8765 | xargs kill
```

## Architecture

The pipeline is four modules wired together in `main.py`:

```
companies.yml → scraper.py → detector.py → notify.py
                    ↓
                state.json (persisted between runs)
```

**`companies.yml`** — The only config file. Each entry specifies a `ticker`, `name`, `shareholder_url`, and `fetch_type` (`static` or `js`). JS companies also accept `wait_ms` (networkidle timeout) and `click_selector` (a Playwright selector to click before extracting, used for tab-based UIs like Icelandair).

**`scraper.py`** — `get_shareholders(url, fetch_type, ...)` is the main entry point. It:
1. Fetches the page via `requests` (static) or Playwright headless Chromium (js).
2. For JS pages using the Keldan/LMD API, intercepts XHR responses and short-circuits to `_build_lmd_html()` which synthesises a parseable HTML table from the JSON payload.
3. Tries three extraction strategies in priority order:
   - `extract_from_table()` — standard HTML `<table>` with header-column detection
   - `extract_from_two_column_list()` — Elementor jet-listing repeater layout (used by Kaldalón)
   - `extract_from_text()` — regex fallback on raw page text
4. Returns top 25 shareholders sorted by `pct` descending.

**`detector.py`** — Pure comparison logic. `detect_changes()` diffs `current` vs `previous` shareholder lists and returns structured change dicts (`new_entry`, `dropped_out`, `increased`, `decreased`). `filter_notifiable()` drops `decreased` changes (informational only). The stake-change threshold is `CHANGE_THRESHOLD_PCT = 1.0`.

**`notify.py`** — Sends HTML+text email. Tries Resend API first (`RESEND_API_KEY` + `NOTIFY_EMAIL_TO`), falls back to Gmail SMTP (`NOTIFY_EMAIL_FROM` + `NOTIFY_EMAIL_PASS` + `NOTIFY_EMAIL_TO`).

**`state.json`** — Committed to the repo after each CI run. Keyed by ticker; stores `shareholders` list, `last_scan` timestamp, and `total` count. First scan for a new company produces no changes (no baseline).

**`aliases.yml`** — Manually maintained. Maps canonical entity names to name variants found in scraped data (e.g. different fund account names for the same institution). `main.py` converts this to `aliases.json` on every run. `aliases.json` is committed by CI alongside `state.json`. Never edit `aliases.json` directly.

**`shareholder.html`** — Static single-page viewer. Fetches `state.json` and `aliases.json` via `fetch()` — must be served over HTTP, not opened as a file. Has two tabs:
- **By Company** — pick a company, see its shareholder list
- **By Shareholder** — pick an investor, see all companies they appear in; aliases are merged under the canonical name with a "Also listed as" note and a total row

**`dev_server.py`** — Local development only. Combines a Python HTTP server with a file watcher that rebuilds `aliases.json` within 1 second of saving `aliases.yml`.

## Adding a new company

Add an entry to `companies.yml`. Minimal required fields: `name`, `ticker`, `shareholder_url`, `fetch_type`. Use `fetch_type: static` unless the page requires JavaScript. For JS pages, try without `wait_ms` first; add it (e.g. `wait_ms: 15000`) if the widget hasn't loaded by networkidle. Use `--debug-html` locally to inspect what Playwright actually fetched.

## Adding shareholder aliases

Edit `aliases.yml` — add a new `canonical` + `aliases` block. To find all name variants for an institution:

```bash
python3 -c "
import json
state = json.loads(open('state.json').read())
for info in state.values():
    for sh in info.get('shareholders', []):
        if 'KEYWORD' in sh['name'].lower(): print(repr(sh['name']))
" | sort -u
```

Run `python dev_server.py` while editing — `aliases.json` rebuilds automatically on save.

## CI / GitHub Actions

- Runs on `.github/workflows/shareholder_tracker.yml`, schedule `0 8 * * 1-5`.
- On CI, `*_debug.html` is saved automatically for every `fetch_type: js` company and uploaded as the `debug-html` artifact (retained 3 days).
- Required secrets: `RESEND_API_KEY`, `NOTIFY_EMAIL_TO` (and optionally `NOTIFY_EMAIL_FROM`, `NOTIFY_EMAIL_PASS` for Gmail fallback).
- State commits use `[skip ci]` to avoid re-triggering the workflow.
