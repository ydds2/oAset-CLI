import glob
import json
import os
from collections import defaultdict

rows = []
for path in glob.glob(os.path.expanduser("~/.oaset/sessions/*/*.jsonl")):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"type": "usage"' not in line and '"type":"usage"' not in line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") != "usage":
                continue
            rows.append((e.get("model", "?"), e.get("at", ""),
                         int(e.get("prompt_tokens", 0) or 0),
                         int(e.get("cache_read_tokens", 0) or 0),
                         int(e.get("cache_write_tokens", 0) or 0)))

print("usage lines total:", len(rows))
have_cache = [r for r in rows if r[3] or r[4]]
print("lines with cache fields:", len(have_cache))
per = defaultdict(lambda: [0, 0, 0, 0])
for m, _at, p, rd, wr in have_cache:
    per[m][0] += 1
    per[m][1] += p
    per[m][2] += rd
    per[m][3] += wr
for m, (n, p, rd, wr) in sorted(per.items(), key=lambda kv: -kv[1][1]):
    rate = 100.0 * rd / p if p else 0.0
    print(f"{m:34} calls={n:4}  prompt={p:>10,}  read={rd:>10,}  hit={rate:5.1f}%")
if have_cache:
    ats = sorted(r[1] for r in have_cache if r[1])
    print("window:", ats[0], "->", ats[-1])
