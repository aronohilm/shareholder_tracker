"""
scraper.py — Fetches shareholder tables from company IR pages.
Tries multiple extraction strategies per page.
"""

import re
import time
import logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Shareholder-Tracker/1.0)"
}


def fetch_page(url: str, fetch_type: str = "static") -> str | None:
    if fetch_type == "js":
        return fetch_js(url)
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            log.warning(f"Attempt {attempt+1}/3 failed for {url}: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None


def fetch_js(url: str) -> str | None:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(extra_http_headers=HEADERS)
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            content = page.content()
            browser.close()
            return content
    except Exception as e:
        log.error(f"Playwright error for {url}: {e}")
        return None


def parse_percentage(s: str) -> float | None:
    """Parse '15,76%' or '15.76%' or '0.1576' → float percentage"""
    if not s:
        return None
    s = s.strip().replace('%', '').replace(',', '.').strip()
    try:
        val = float(s)
        # If stored as decimal (0.1576) convert to percentage
        if val < 1.0 and val > 0:
            val = val * 100
        return round(val, 4)
    except ValueError:
        return None


def extract_from_table(soup: BeautifulSoup) -> list[dict]:
    """
    Try to extract shareholder data from HTML tables.
    Looks for tables with shareholder-name + ownership-percentage columns.
    """
    best_results = []

    for table in soup.find_all("table"):
        table_results = []
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        # Read header cells
        header_cells = rows[0].find_all(["th", "td"])
        headers = [c.get_text(" ", strip=True).lower() for c in header_cells]

        def is_name_header(h: str) -> bool:
            return any(x in h for x in [
                "nafn hluthafa",
                "hluthafi",
                "shareholder",
                "name",
            ])

        def is_pct_header(h: str) -> bool:
            return any(x in h for x in [
                "%",
                "eignarhlutur",
                "ownership",
                "percent",
                "hlutfall",
            ])

        name_idx = next((i for i, h in enumerate(headers) if is_name_header(h)), None)
        pct_idx = next((i for i, h in enumerate(headers) if is_pct_header(h)), None)

        # Fallback only if headers are really unclear
        if name_idx is None:
            name_idx = 0
        if pct_idx is None:
            pct_idx = -1

        for row in rows[1:]:
            # only direct cells in this row
            cells = row.find_all(["td", "th"], recursive=False)

            # fallback if recursive=False returns nothing
            if not cells:
                cells = row.find_all(["td", "th"])

            if len(cells) <= max(name_idx, pct_idx):
                log.debug("Skipping row, not enough cells: %s", row.get_text(" | ", strip=True))
                continue

            try:
                name = cells[name_idx].get_text(" ", strip=True)
                pct_raw = cells[pct_idx].get_text(" ", strip=True)
                pct = parse_percentage(pct_raw)

                if not name or pct is None:
                    log.debug("Skipping row, bad name/pct: name=%r pct_raw=%r", name, pct_raw)
                    continue

                lowered = name.lower()
                if any(k in lowered for k in ["samtals", "total", "aðrir", "others", "nafn"]):
                    continue

                if pct <= 0 or pct > 100:
                    log.debug("Skipping row, invalid pct: name=%r pct=%r", name, pct)
                    continue

                table_results.append({"name": name, "pct": pct})

            except Exception as e:
                log.debug("Row parse error: %s | row=%s", e, row.get_text(" | ", strip=True))
                continue

        # keep the biggest valid table, not just first non-empty one
        if len(table_results) > len(best_results):
            best_results = table_results

    # dedupe by (name, pct), not pct only
    seen = set()
    unique = []
    for r in best_results:
        key = (r["name"], r["pct"])
        if key not in seen:
            seen.add(key)
            unique.append(r)

    return unique

def extract_from_text(html: str) -> list[dict]:
    """
    Fallback: find shareholder name + percentage patterns in raw text.
    Handles cases where data isn't in a proper table (like Kaldalón).
    Pattern: name followed by percentage on same or adjacent line.
    """
    results = []

    # Strip HTML tags
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")

    # Pattern: "Name of Company\n15,76%" or "Name of Company 15,76%"
    pct_pattern = re.compile(r'(\d{1,3}[.,]\d{1,4})\s*%')
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    for i, line in enumerate(lines):
        pct_match = pct_pattern.search(line)
        if pct_match:
            pct = parse_percentage(pct_match.group(1))
            if pct is None or pct <= 0 or pct > 50:
                continue

            # Name is either on same line (before %) or previous line
            name_part = line[:pct_match.start()].strip()
            if not name_part and i > 0:
                name_part = lines[i - 1].strip()

            # Clean up name
            name_part = re.sub(r'\s+', ' ', name_part).strip(" ·-–")
            if not name_part or len(name_part) < 3:
                continue
            if any(k in name_part.lower() for k in ["samtals", "total", "aðrir", "others"]):
                continue

            results.append({"name": name_part, "pct": pct})

    # Deduplicate by name only (two holders can have identical pct)
    seen_names = set()
    unique = []
    for r in results:
        if r["name"] not in seen_names:
            seen_names.add(r["name"])
            unique.append(r)

    return unique


def get_shareholders(url: str, fetch_type: str = "static") -> list[dict]:
    """
    Main function: fetch page and extract shareholders.
    Returns list of {"name": str, "pct": float} sorted by pct desc.
    """
    html = fetch_page(url, fetch_type)
    if not html:
        log.error(f"Could not fetch {url}")
        return []

    soup = BeautifulSoup(html, "html.parser")

    # Try table extraction first
    shareholders = extract_from_table(soup)

    # Fall back to text extraction
    if not shareholders:
        log.info("Table extraction found nothing, trying text extraction")
        shareholders = extract_from_text(html)

    log.info("Raw shareholders: %s", shareholders)

    # Sort by percentage descending, take top 25
    shareholders = sorted(shareholders, key=lambda x: x["pct"], reverse=True)[:25]

    log.info(f"Found {len(shareholders)} shareholders")
    return shareholders