import json, pathlib

f = pathlib.Path("changes.json")
if not f.exists():
    print("No changes file found")
    raise SystemExit(0)

changes = json.loads(f.read_text())
print(f"Total changes detected: {len(changes)}")
for c in changes:
    print(f"  {c.get('emoji','')} [{c['ticker']}] {c['summary']}")
