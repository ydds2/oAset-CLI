"""Expand collapsed-paste placeholders back into the text the user pasted.

A paste larger than `PASTE_COLLAPSE_CHARS` is written to a workspace-external
file under `~/.oaset/pastes/` and the input box only shows a placeholder:

    [pasted: C:\\Users\\me\\.oaset\\pastes\\paste-20240101-120000.txt (6490 chars)]

The placeholder exists to keep the composer readable. It must never be what
reaches the model: the model cannot read `~/.oaset/pastes/` (it is outside the
workspace), so sending the marker verbatim silently discards the user's input —
they paste a stack trace and the answer is "I don't see any log".

Expansion happens at submit time, so the composer stays small while the turn
gets the real content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Mirrors the format written by InputArea.insert_collapsed_paste. The path is
# non-greedy so a path containing " (" cannot swallow the size group; the size
# group really is optional — a hand-edited marker without a count still
# expands (the count is decoration, the path is the payload).
PASTE_REF_RE = re.compile(r"\[pasted:\s*(?P<path>.+?)\s*(?:\((?P<size>\d+)\s*chars\))?\]")


@dataclass(frozen=True)
class PasteRef:
    """One collapsed paste found in a draft."""

    path: str
    chars: int
    start: int
    end: int


def find_paste_refs(text: str) -> list[PasteRef]:
    """Every placeholder in `text`, in order of appearance."""
    return [
        # size is optional now (hand-edited markers): a missing count is 0
        PasteRef(m.group("path"), int(m.group("size") or 0), m.start(), m.end())
        for m in PASTE_REF_RE.finditer(text or "")
    ]


def expand_paste_refs(text: str, read_text) -> tuple[str, list[PasteRef]]:
    """Replace each placeholder with the file's contents.

    `read_text(path) -> str | None` performs the read; returning None (missing
    file, permission error, not our file) leaves that placeholder untouched so
    the turn still carries the pointer and the caller can warn.

    Returns the expanded text and the refs actually expanded.
    """
    refs = find_paste_refs(text)
    if not refs:
        return text, []
    out: list[str] = []
    expanded: list[PasteRef] = []
    cursor = 0
    for ref in refs:
        out.append(text[cursor:ref.start])
        body = read_text(ref.path)
        if body is None:
            out.append(text[ref.start:ref.end])  # keep the pointer, unexpanded
        else:
            out.append(body)
            expanded.append(ref)
        cursor = ref.end
    out.append(text[cursor:])
    return "".join(out), expanded


def read_paste_file(path: str, pastes_dir: Path) -> str | None:
    """Read a collapsed-paste file, but only from the pastes directory.

    The placeholder is user-visible text, so a draft could reference any path.
    Restricting the read to the directory we ourselves write keeps a pasted
    marker from turning into an arbitrary-file-read primitive.
    """
    try:
        candidate = Path(path).resolve()
        root = pastes_dir.resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    if candidate.parent != root or not candidate.is_file():
        return None
    try:
        return candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def resolve_draft(text: str, pastes_dir: Path) -> tuple[str, list[PasteRef]]:
    """Expand placeholders in a draft about to be submitted."""
    return expand_paste_refs(text, lambda p: read_paste_file(p, pastes_dir))


# Bounds for the scratch directory. Every large paste and every pasted
# screenshot is written here for the lifetime of the process, and nothing ever
# removed them: `~/.oaset/pastes` was an unbounded pile of clipboard images and
# pasted logs that grew for as long as the tool was used.
PASTES_KEEP = 40
# Nothing younger than this is ever removed, however many files there are: a
# draft can still reference a paste the user made minutes ago, and expansion
# happens at submit time — deleting it would turn a paste into a warning.
PASTES_MIN_AGE_HOURS = 24


def prune_pastes(pastes_dir: Path, *, keep: int = PASTES_KEEP,
                 min_age_hours: int = PASTES_MIN_AGE_HOURS) -> int:
    """Sweep the scratch directory. Returns the number of files removed.

    One rule: among files older than `min_age_hours`, keep the newest `keep` and
    delete the rest. Called right after a new scratch file is written, so the
    sweep only happens while the directory is in use. Failures are ignored —
    housekeeping is never a failure path.
    """
    import time

    try:
        paths = [p for p in pastes_dir.iterdir() if p.is_file()]
    except OSError:
        return 0
    stamped: list[tuple[float, Path]] = []
    for path in paths:
        try:
            stamped.append((path.stat().st_mtime, path))
        except OSError:
            continue
    floor = time.time() - min_age_hours * 3600
    eligible = sorted((item for item in stamped if item[0] < floor),
                      key=lambda item: item[0], reverse=True)
    removed = 0
    for _, path in eligible[keep:]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed
