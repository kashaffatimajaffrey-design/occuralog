import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "output/events.json"

with open(path, encoding="utf-8") as f:
    data = json.load(f)

unknowns = [e["text"] for e in data["events"] if e["event_types"] == ["unknown"]]

print(f"Total unknown: {len(unknowns)}\n")
for i, text in enumerate(unknowns, 1):
    print(f"{i:>3}. {text}")