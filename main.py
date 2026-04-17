"""
main.py — Entry point. Scans all companies, detects changes, sends notifications.
"""

import json
import logging
import time
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

import yaml

from scraper import get_shareholders
from detector import detect_changes, filter_notifiable
from notify import send_notifications

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent / "state.json"
COMPANIES_FILE = Path(__file__).parent / "companies.yml"


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def scan_company(company: dict, state: dict, debug_html: str | None = None) -> list[dict]:
    ticker = company["ticker"]
    name = company["name"]
    url = company["shareholder_url"]
    fetch_type = company.get("fetch_type", "static")
    wait_ms = company.get("wait_ms", 5000)

    log.info(f"Scanning: {name} ({ticker})")

    current = get_shareholders(url, fetch_type, wait_ms=wait_ms, debug_html=debug_html)


    if not current:
        log.warning(f"No shareholders found for {name} — skipping")
        return []

    log.info(f"  Found {len(current)} shareholders")

    previous = state.get(ticker, {}).get("shareholders", [])
    changes = detect_changes(ticker, name, current, previous)

    if changes:
        log.info(f"  Changes detected: {len(changes)}")
        for c in changes:
            log.info(f"    {c['emoji']} {c['summary']}")
    else:
        log.info(f"  No changes")

    # Update state
    state[ticker] = {
        "name": name,
        "shareholders": current,
        "last_scan": datetime.now(timezone.utc).isoformat(),
        "total": len(current),
    }

    return changes


def main():
    parser = argparse.ArgumentParser(description="Shareholder Tracker")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan but don't save state or send notifications")
    parser.add_argument("--company", help="Only scan this ticker")
    parser.add_argument("--output-json", help="Write changes to this JSON file")
    parser.add_argument("--debug-html", help="Save raw fetched HTML to this file (use with --company)")
    args = parser.parse_args()

    config = yaml.safe_load(COMPANIES_FILE.read_text(encoding="utf-8"))
    companies = config.get("companies", [])

    if args.company:
        companies = [c for c in companies if c.get("ticker") == args.company]
        if not companies:
            log.error(f"Ticker {args.company} not found in companies.yml")
            sys.exit(1)

    state = load_state()
    all_changes = []

    for company in companies:
        try:
            changes = scan_company(company, state, debug_html=args.debug_html)
            all_changes.extend(changes)
        except Exception as e:
            log.error(f"Error scanning {company.get('name')}: {e}")
        time.sleep(1)

    if args.output_json:
        Path(args.output_json).write_text(
            json.dumps(all_changes, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    if not args.dry_run:
        save_state(state)
        notifiable = filter_notifiable(all_changes)
        if notifiable:
            send_notifications(notifiable)
        else:
            log.info("No notifiable changes — no email sent")

    log.info(f"Done. {len(all_changes)} total change(s) across {len(companies)} companies.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
