"""I18N-01 guards: the catalog is complete and internally consistent.

1. every key carries BOTH en and zh (a missing language must fail CI, not
   silently render the other language);
2. every key referenced through t("...") in src/oaset exists in the catalog
   (a typo'd key renders as the raw key string in the product).
"""

from __future__ import annotations

import re
from pathlib import Path

from oaset.i18n import CATALOG

SRC = Path(__file__).resolve().parents[1] / "src" / "oaset"


def test_every_catalog_key_has_both_languages():
    incomplete = {
        key: sorted(set(entry) - {"en", "zh"})
        for key, entry in CATALOG.items()
        if not {"en", "zh"} <= set(entry)
    }
    assert not incomplete, incomplete


def test_no_duplicate_catalog_keys():
    src = (SRC / "i18n.py").read_text(encoding="utf-8")
    keys = re.findall(r'^    "([a-z_0-9]+)": \{', src, re.MULTILINE)
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, dupes


def test_every_referenced_key_exists():
    missing = []
    pat = re.compile(r'\bt\(\s*"([a-z_0-9]+)"[,)]')
    for path in SRC.rglob("*.py"):
        if path.name == "i18n.py":
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in pat.finditer(line):
                key = m.group(1)
                if key not in CATALOG:
                    missing.append(f"{path.relative_to(SRC)}:{i}: {key}")
    assert not missing, missing
