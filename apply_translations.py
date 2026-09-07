"""
Apply translations from bot_messages.csv back into the source files.

Plain strings are replaced literally. Messages that came from f-strings were
exported with '{}' placeholders, so those are matched with a regex that keeps
the ORIGINAL python expressions ({U.tier_of(uid)} etc.) and slots them into the
translated sentence in the same order.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

CSV = Path(sys.argv[1] if len(sys.argv) > 1 else "bot_messages.csv")

rows = list(csv.DictReader(CSV.open(encoding="utf-8-sig")))
applied, skipped = 0, []

# group by file so each file is read/written once
by_file: dict[str, list[dict]] = {}
for r in rows:
    tr = (r.get("translation_az") or "").strip()
    src = (r.get("text") or "").strip()
    if not tr or tr == src:
        continue
    by_file.setdefault(r["file"], []).append(r)

for fname, items in by_file.items():
    p = Path(fname)
    if not p.exists():
        skipped.append((fname, "file missing"))
        continue
    s = p.read_text(encoding="utf-8")

    # longest first so a short string never clobbers part of a longer one
    items.sort(key=lambda r: -len(r["text"]))

    for r in items:
        old, new = r["text"], r["translation_az"].strip()
        n_old, n_new = old.count("{}"), new.count("{}")

        if n_old == 0:
            if old in s:
                s = s.replace(old, new)
                applied += 1
            else:
                skipped.append((f"{fname}:{r['id']}", "literal not found"))
            continue

        # f-string: keep the original {expr} pieces
        if n_new != n_old:
            skipped.append((f"{fname}:{r['id']}",
                            f"placeholder count {n_new}!={n_old}"))
            continue

        parts = old.split("{}")
        pattern = re.escape(parts[0])
        for part in parts[1:]:
            pattern += r"(\{[^{}]*\})" + re.escape(part)

        m = re.search(pattern, s)
        if not m:
            skipped.append((f"{fname}:{r['id']}", "f-string not matched"))
            continue

        new_parts = new.split("{}")
        rebuilt = new_parts[0]
        for i, part in enumerate(new_parts[1:], start=1):
            rebuilt += m.group(i) + part
        s = s[:m.start()] + rebuilt + s[m.end():]
        applied += 1

    p.write_text(s, encoding="utf-8")

print(f"applied : {applied}")
print(f"skipped : {len(skipped)}")
for where, why in skipped:
    print(f"   - {where}: {why}")
