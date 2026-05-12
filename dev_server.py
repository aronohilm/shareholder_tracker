#!/usr/bin/env python3
"""
Local dev server — serves the viewer and auto-rebuilds aliases.json
whenever aliases.yml is saved. Just refresh the browser after editing.
"""

import http.server
import json
import os
import threading
import time
from pathlib import Path

import yaml

PORT = 8765
BASE_DIR = Path(__file__).parent
ALIASES_YML = BASE_DIR / "aliases.yml"
ALIASES_JSON = BASE_DIR / "aliases.json"


def build_aliases():
    config = yaml.safe_load(ALIASES_YML.read_text(encoding="utf-8"))
    result = {
        e["canonical"]: e.get("aliases", [])
        for e in config.get("entities", [])
        if e.get("canonical")
    }
    ALIASES_JSON.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  [aliases] rebuilt — {len(result)} entities")


def watch():
    last_mtime = ALIASES_YML.stat().st_mtime
    while True:
        time.sleep(1)
        try:
            mtime = ALIASES_YML.stat().st_mtime
            if mtime != last_mtime:
                last_mtime = mtime
                build_aliases()
        except Exception as e:
            print(f"  [watcher] error: {e}")


if __name__ == "__main__":
    os.chdir(BASE_DIR)
    build_aliases()

    threading.Thread(target=watch, daemon=True).start()

    print(f"Serving at http://localhost:{PORT}/shareholder.html")
    print("Edit aliases.yml and refresh — aliases.json updates automatically.")
    print("Ctrl+C to stop.\n")

    http.server.test(
        HandlerClass=http.server.SimpleHTTPRequestHandler,
        port=PORT,
        bind="",
    )
