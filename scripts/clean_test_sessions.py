"""Clean TEST-ARTIFACT sessions out of the real ~/.oaset.

Keeps every bucket that is NOT a pytest temp/test workspace; rewrites
session_index.jsonl without the removed entries. Dry-run by default.
"""
import glob
import json
import os
import re
import sys

DRY = "--go" not in sys.argv
root = os.path.expanduser("~/.oaset/sessions")
index = os.path.expanduser("~/.oaset/session_index.jsonl")
test_pat = re.compile(r"wd_(test_|tmp|pytest-)")

keep_files, drop_files = [], []
for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
    bucket = os.path.basename(os.path.dirname(path))
    (drop_files if test_pat.match(bucket) else keep_files).append(path)

print(f"keep {len(keep_files)} real, drop {len(drop_files)} test artifacts")
if DRY:
    print("dry-run; pass --go to delete")
    sys.exit(0)

entries = []
with open(index, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if any(os.path.normcase(e.get("path", "")) == os.path.normcase(p)
               for p in drop_files):
            continue
        entries.append(line.rstrip("\n"))

for p in drop_files:
    for suffix in ("", ".lock"):
        try:
            os.remove(p + suffix)
        except OSError:
            pass
    bucket = os.path.dirname(p)
    try:
        os.rmdir(bucket)
    except OSError:
        pass

tmp = index + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write("\n".join(entries) + ("\n" if entries else ""))
os.replace(tmp, index)
print(f"index rewritten with {len(entries)} entries")
