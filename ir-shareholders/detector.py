"""
detector.py — Compares current shareholders against previous snapshot.
Returns structured list of changes to notify about.
"""


CHANGE_THRESHOLD_PCT = 1.0   # Minimum % change to trigger "stake increased/decreased" alert


def detect_changes(ticker: str, company: str,
                   current: list[dict],
                   previous: list[dict]) -> list[dict]:
    """
    Compare current vs previous shareholder list.
    Returns list of change dicts.

    Change types:
      - new_entry:    shareholder appears for first time in top 20
      - dropped_out:  shareholder was in top 20, now gone
      - increased:    stake increased by >= CHANGE_THRESHOLD_PCT
      - decreased:    stake decreased by >= CHANGE_THRESHOLD_PCT (informational)
    """
    if not previous:
        # First run — no baseline to compare against
        return []

    changes = []

    prev_map = {s["name"]: s["pct"] for s in previous}
    curr_map = {s["name"]: s["pct"] for s in current}

    prev_names = set(prev_map.keys())
    curr_names = set(curr_map.keys())

    # New entries
    for name in curr_names - prev_names:
        pct = curr_map[name]
        changes.append({
            "ticker": ticker,
            "company": company,
            "type": "new_entry",
            "name": name,
            "pct_now": pct,
            "pct_before": None,
            "delta": None,
            "emoji": "🆕",
            "summary": f"{name} enters top shareholders at {pct:.2f}%",
        })

    # Dropped out
    for name in prev_names - curr_names:
        pct_before = prev_map[name]
        changes.append({
            "ticker": ticker,
            "company": company,
            "type": "dropped_out",
            "name": name,
            "pct_now": None,
            "pct_before": pct_before,
            "delta": None,
            "emoji": "👋",
            "summary": f"{name} drops out (was {pct_before:.2f}%)",
        })

    # Stake changes for existing holders
    for name in curr_names & prev_names:
        pct_now = curr_map[name]
        pct_before = prev_map[name]
        delta = pct_now - pct_before

        if delta >= CHANGE_THRESHOLD_PCT:
            changes.append({
                "ticker": ticker,
                "company": company,
                "type": "increased",
                "name": name,
                "pct_now": pct_now,
                "pct_before": pct_before,
                "delta": delta,
                "emoji": "📈",
                "summary": f"{name} increased stake {pct_before:.2f}% → {pct_now:.2f}% (+{delta:.2f}%)",
            })
        elif delta <= -CHANGE_THRESHOLD_PCT:
            # Decreased — informational only, not in notify list but recorded
            changes.append({
                "ticker": ticker,
                "company": company,
                "type": "decreased",
                "name": name,
                "pct_now": pct_now,
                "pct_before": pct_before,
                "delta": delta,
                "emoji": "📉",
                "summary": f"{name} decreased stake {pct_before:.2f}% → {pct_now:.2f}% ({delta:.2f}%)",
            })

    return changes


def filter_notifiable(changes: list[dict]) -> list[dict]:
    """Return only changes worth sending a notification for"""
    notify_types = {"new_entry", "dropped_out", "increased"}
    return [c for c in changes if c["type"] in notify_types]
