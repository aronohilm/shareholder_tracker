"""
scraper.py — Fetches shareholder tables from company IR pages.
Tries multiple extraction strategies per page.
"""

import re
import time
import logging
import requests
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urljoin

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Shareholder-Tracker/1.0)"
}


def fetch_page(url: str, fetch_type: str = "static", wait_ms: int = 5000) -> str | None:
    if fetch_type == "js":
        return fetch_js(url, wait_ms=wait_ms)
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


def fetch_js(url: str, wait_ms: int = 5000) -> str | None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-http2",
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )

            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                viewport={"width": 1440, "height": 900},
            )

            page = context.new_page()

            # Intercept JSON responses from LMD/Keldan shareholder API
            _lmd_shareholders: list[dict] = []

            def _on_response(response):
                if ("api.livemarketdata.com" in response.url
                        and "shareholders" in response.url
                        and response.status == 200):
                    try:
                        data = response.json()
                        _lmd_shareholders.append(data)
                        log.info("Captured LMD shareholders response from %s; type=%s len=%s",
                                 response.url, type(data).__name__,
                                 len(data) if isinstance(data, (list, dict)) else "?")
                        log.debug("LMD raw: %s", str(data)[:300])
                    except Exception as e:
                        log.warning("Failed to parse LMD response: %s", e)

            page.on("response", _on_response)

            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(wait_ms)

            # Dismiss cookie consent dialogs if present
            consent_selectors = [
                "button.cky-btn-accept",       # CookieYes
                "button.ch2-allow-all-btn",    # Cookiebot/CH2
                "button[data-cky-tag='accept-button']",
                "button[aria-label='Accept All']",
                "button[id*='accept']",
            ]
            for sel in consent_selectors:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        log.info(f"Clicked consent button: {sel}")
                        page.wait_for_timeout(3000)
                        break
                except Exception:
                    pass

            # If LMD API responses were captured, encode them into a synthetic HTML
            # that extract_from_table / extract_from_text can later find via
            # a dedicated path in get_shareholders.
            if _lmd_shareholders:
                page.close()
                browser.close()
                return _build_lmd_html(_lmd_shareholders)

            content = page.content()
            browser.close()
            return content

    except Exception as e:
        log.error(f"Playwright error for {url}: {e}")
        return None


def _build_lmd_html(responses: list) -> str:
    """
    Convert captured LMD JSON shareholder responses into a minimal HTML table
    that extract_from_table can parse.
    LMD API can return a list directly, or a dict with shareholders/data key.
    """
    rows = []
    for payload in responses:
        # Normalise to a list of items
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("shareholders") or payload.get("data") or []
        else:
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            # LMD API uses: Owner (name), Percentage (decimal e.g. 0.00531 = 0.531%)
            name = (item.get("Owner") or item.get("name")
                    or item.get("holderName") or item.get("holder") or "")
            pct_raw = (item.get("Percentage") or item.get("percentage")
                       or item.get("percent") or item.get("share") or "")
            if name and pct_raw not in ("", None):
                # Convert decimal fraction to percentage string with % sign
                try:
                    pct_val = float(pct_raw)
                    if 0 < pct_val < 1.0:
                        pct_val = pct_val * 100
                    pct_str = f"{pct_val:.4f}%"
                except (ValueError, TypeError):
                    pct_str = f"{pct_raw}%"
                rows.append(f"<tr><td>{name}</td><td>{pct_str}</td></tr>")

    if not rows:
        log.warning("LMD response captured but no rows extracted; raw: %s",
                    str(responses)[:300])
        return ""

    table = "<table><tr><th>name</th><th>hlutfall %</th></tr>" + "".join(rows) + "</table>"
    return f"<html><body>{table}</body></html>"


def parse_percentage(s: str) -> float | None:
    """Parse '15,76%' or '15.76%' or '0.1576' → float percentage"""
    if not s:
        return None
    had_pct = '%' in s
    s = s.strip().replace('%', '').replace(',', '.').strip()
    try:
        val = float(s)
        # Only convert decimal→percentage when there was no % sign (e.g. raw 0.1576)
        if not had_pct and 0 < val < 1.0:
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
                "hluthafa",   # matches "nafn hluthafa", "hluthafar", etc.
                "hluthafi",
                "shareholder",
                "name",
                "eigandi",
                "nafn",
            ])

        def pct_priority(h: str) -> int:
            """Lower = higher confidence as a percentage column."""
            if any(x in h for x in ["%", "ownership", "percent", "hlutfall"]):
                return 0
            if "eignarhlutur" in h:
                return 1  # fallback: can be pct or share-count depending on table
            return 99

        name_idx = next((i for i, h in enumerate(headers) if is_name_header(h)), None)
        pct_candidates = [(i, pct_priority(h)) for i, h in enumerate(headers)
                          if pct_priority(h) < 99]
        pct_idx = min(pct_candidates, key=lambda x: x[1])[0] if pct_candidates else None

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
                # Strip injected metadata e.g. " Fjöldi hluta 130.609.960 Hlutfall 18.739%"
                name = re.split(r'\s+Fj\xf6ldi\s+hluta', name, flags=re.IGNORECASE)[0].strip()
                pct_raw = cells[pct_idx].get_text(" ", strip=True)
                pct = parse_percentage(pct_raw)

                if not name or pct is None:
                    log.debug("Skipping row, bad name/pct: name=%r pct_raw=%r", name, pct_raw)
                    continue

                lowered = name.lower().strip(" :.-")
                bad_prefixes = {"samtals", "total", "aðrir", "others", "nafn", "hlutir", "number of"}
                if any(lowered == p or lowered.startswith(p) for p in bad_prefixes):
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
            pct = parse_percentage(pct_match.group(0))
            if pct is None or pct <= 0 or pct > 50:
                continue

            # Name is either on same line (before %) or previous line(s)
            name_part = line[:pct_match.start()].strip()
            if not name_part and i > 0:
                name_part = lines[i - 1].strip()
            # If previous line looks like a share count (only digits, dots, commas)
            # the real name is one line further back
            if name_part and re.match(r'^[\d.,]+$', name_part) and i > 1:
                name_part = lines[i - 2].strip()

            # Clean up name
            name_part = re.sub(r'\s+', ' ', name_part).strip(" ·-–")
            if not name_part or len(name_part) < 3:
                continue
            cleaned = name_part.lower().strip(" :.-")
            bad_prefixes = {"samtals", "total", "aðrir", "others", "nafn", "hlutir"}
            if any(cleaned == p or cleaned.startswith(p) for p in bad_prefixes):
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


def extract_from_two_column_list(soup: BeautifulSoup) -> list[dict]:
    """
    Handle Elementor jet-listing repeater layouts where names and percentages
    are in parallel columns (e.g. Kaldalón hf.).
    Looks for paired jet-listing-dynamic-repeater__items columns.
    """
    pct_pattern = re.compile(r'^\d{1,3}[.,]\d{1,4}%$')
    repeater_cols = soup.find_all("div", class_="jet-listing-dynamic-repeater__items")
    if len(repeater_cols) < 2:
        return []

    for i in range(len(repeater_cols) - 1):
        names_col = repeater_cols[i]
        pcts_col = repeater_cols[i + 1]

        names = [d.get_text(strip=True) for d in names_col.find_all("div", class_="jet-listing-dynamic-repeater__item")]
        pcts_raw = [d.get_text(strip=True) for d in pcts_col.find_all("div", class_="jet-listing-dynamic-repeater__item")]

        # Only proceed if the second column looks like percentages
        if not pcts_raw or not all(pct_pattern.match(p) for p in pcts_raw[:3]):
            continue

        results = []
        bad_prefixes = {"samtals", "total", "aðrir", "others", "nafn", "hlutir"}
        for name, pct_raw in zip(names, pcts_raw):
            lowered = name.lower().strip(" :.-")
            if any(lowered == p or lowered.startswith(p) for p in bad_prefixes):
                continue
            pct = parse_percentage(pct_raw)
            if not name or pct is None or pct <= 0 or pct > 100:
                continue
            results.append({"name": name, "pct": pct})

        if results:
            return results

    return []


def get_shareholders(url: str, fetch_type: str = "static", wait_ms: int = 5000, debug_html: str | None = None) -> list[dict]:
    """
    Main function: fetch page and extract shareholders.
    Returns list of {"name": str, "pct": float} sorted by pct desc.
    """
    html = fetch_page(url, fetch_type, wait_ms=wait_ms)
    if not html:
        log.error(f"Could not fetch {url}")
        return []

    if debug_html:
        Path(debug_html).write_text(html, encoding="utf-8")
        log.info(f"Saved raw HTML to {debug_html}")

    soup = BeautifulSoup(html, "html.parser")

    # Try table extraction first
    shareholders = extract_from_table(soup)

    # Try two-column jet-listing layout (e.g. Kaldalón)
    if not shareholders:
        log.info("Table extraction found nothing, trying two-column list extraction")
        shareholders = extract_from_two_column_list(soup)

    # Fall back to text extraction
    if not shareholders:
        log.info("Two-column extraction found nothing, trying text extraction")
        shareholders = extract_from_text(html)

    log.info("Raw shareholders: %s", shareholders)

    # Sort by percentage descending, take top 25
    shareholders = sorted(shareholders, key=lambda x: x["pct"], reverse=True)[:25]

    log.info(f"Found {len(shareholders)} shareholders")
    return shareholders