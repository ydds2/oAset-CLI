"""`apply_patch` — apply a unified diff without going through the shell.

Codex/Aider-style: the model produces a patch, this tool writes the files.
Paths must stay inside the workspace. One hunk failing aborts the whole apply.
"""

from __future__ import annotations

import re
from typing import Any

from oaset.tools.base import WRITE, Tool, ToolContext, ToolResult
from oaset.utils import expand_path, short_path

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _old_marker(line: str) -> bool:
    """`--- a/x` header, or the space-less `---a/x` models also emit.

    A deletion of content `--a/x` produces the same bytes — callers
    disambiguate with the pair-plus-@@ lookahead, never the marker alone.
    """
    return line.startswith("--- ") or (
        line.startswith("---") and len(line) > 3 and not line[3].isspace())


def _pathlike(raw: str) -> bool:
    """True when a `---`/`+++` payload names a PATH (file header), not
    content like `--flag`/`++i;`. Models sometimes OVER-count hunk lines,
    and the counting alone then swallowed the next file's real header —
    the pair shape plus a path-shaped value outranks the counts.

    Called ONLY on payloads whose pair shape (--- x / +++ y / @@) already
    holds, so spaces are allowed here (spaced filenames, git's quoted
    forms): the leading -/+ test alone separates `-- old setting` content
    from `my file.py` headers."""
    raw = raw.strip()
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    if raw.startswith("a/") or raw.startswith("b/"):
        raw = raw[2:]
    if raw == "/dev/null":
        return True
    if not raw or raw.startswith(("-", "+")):
        return False
    return True


def _header_path(payload: str) -> str:
    """`b/x`-style new-file header payload → the path (quotes stripped, as
    git emits them for spaced filenames)."""
    raw = payload.strip()
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    if raw.startswith("b/"):
        raw = raw[2:]
    return raw


def _new_marker(line: str) -> bool:
    return line.startswith("+++ ") or (
        line.startswith("+++") and len(line) > 3 and not line[3].isspace())


def parse_unified_diff(text: str) -> list[tuple[str, list[str]]]:
    """Return [(path, hunk_lines including the @@ header), ...].

    The `--- a/path` header lines are AMBIGUOUS in unified diffs: at a file
    boundary they open the old file, but inside a hunk body `-` means a
    deletion. Distinguishing them by peeking one line ahead (a `---` that is
    immediately followed by `+++` is a header, anything else is a deletion
    line) is what makes MULTI-FILE diffs parse — without the lookahead the
    second file's header was swallowed into the first file's hunks as a
    deletion, and every multi-file patch failed to apply.
    """
    files: list[tuple[str, list[str]]] = []
    path = ""
    hunks: list[str] = []
    prev_was_content_dd = False  # a `--- x` classified as a DELETION line
    prev_was_dd_header = False   # the previous line was a `---` OLD-FILE HEADER
    # remaining old/new lines of the OPEN hunk. While either is >0 the hunk
    # body is live and a `---`/`+++`-shaped line is CONTENT by definition —
    # the lookahead rules alone misread a body-tail `+++i;` sitting right
    # before the next `@@` as a header (real repro: edits swallowed into a
    # bogus file named "i;").
    old_left = 0
    new_left = 0
    # prefix-match: git may append a section label after the closing @@
    count_re = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        counts = count_re.match(line)
        if counts:
            old_left = int(counts.group(1)) if counts.group(1) else 1
            new_left = int(counts.group(2)) if counts.group(2) else 1
            prev_was_content_dd = False
            prev_was_dd_header = False
            hunks.append(line)
            continue
        if (old_left > 0 or new_left > 0) and (_new_marker(line) or _old_marker(line)):
            # escape hatch: an over-counted hunk must not swallow the NEXT
            # file's real header — a `--- <path>` whose `+++ <path>` partner
            # is followed by `@@`, with path-shaped values, is a header no
            # matter what the counts claim
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            after = lines[i + 2] if i + 2 < len(lines) else ""
            if (_old_marker(line) and _pathlike(line[3:])
                    and _new_marker(nxt) and _pathlike(nxt[3:])
                    and after.startswith("@@ ")):
                if path and hunks:
                    files.append((path, hunks))
                path = _header_path(nxt[3:])
                hunks = []
                prev_was_content_dd = False
                prev_was_dd_header = False
                continue
            # live hunk body: marker-shaped or not, this line is content
            hunks.append(line)
            if line[:1] == "-":
                old_left -= 1
            elif line[:1] == "+":
                new_left -= 1
            prev_was_content_dd = False
            prev_was_dd_header = False
            continue
        if _new_marker(line):
            # a `+++` line is a NEW-FILE HEADER only when its `---` partner
            # just preceded it, or when its NEXT line is the hunk header
            # (`@@ …`) — that covers header-less single- and multi-file
            # diffs, an accepted model output shape. Anywhere else it is
            # CONTENT: an added line whose text begins with "++" wires as
            # `+++ …`/`+++…`, and treating that as a header swallowed the
            # rest of the hunk into a bogus file name
            if prev_was_content_dd:
                # `--- x`/`+++ y` pair inside a hunk: content, not a header
                hunks.append(line)
                prev_was_content_dd = False
                prev_was_dd_header = False
                continue
            nxt_is_hunk = (lines[i + 1] if i + 1 < len(lines) else "").startswith("@@ ")
            if not prev_was_dd_header and not nxt_is_hunk:
                hunks.append(line)
                continue
            if path and hunks:
                files.append((path, hunks))
            # `+++ b/x`, the space-less `+++b/x` and git's quoted forms
            # all name the new file
            path = _header_path(line[3:])
            hunks = []
            prev_was_dd_header = False
            continue
        prev_was_content_dd = False
        prev_was_dd_header = False
        if _old_marker(line):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            after = lines[i + 2] if i + 2 < len(lines) else ""
            # a `--- x` line is the OLD-FILE HEADER only when its `+++ y`
            # partner is itself followed by a hunk header — deleting content
            # `-- x` and adding `++ y` produces exactly this pair inside a
            # hunk, and treating it as a header silently dropped the rest of
            # the hunk while reporting success
            if _new_marker(nxt) and after.startswith("@@ "):
                prev_was_dd_header = True
                continue
            prev_was_content_dd = True
        if line.startswith("@@ ") or (hunks and (line[:1] in " +-" or line.startswith("\\"))):
            hunks.append(line)
            if line[:1] == " ":
                old_left -= 1
                new_left -= 1
            elif line[:1] == "-":
                old_left -= 1
            elif line[:1] == "+":
                new_left -= 1
    if path and hunks:
        files.append((path, hunks))
    return files


def _seal_tail(out: list[str]) -> None:
    """Close a final line that has no newline before the patch writes past
    it — appending `+c` after a no-newline `b` used to glue them into one
    line (`bc`) and invent a trailing newline."""
    if out and out[-1] and not out[-1].endswith("\n"):
        out[-1] += "\n"


def _apply_hunks(original: str, hunks: list[str]) -> str:
    source = original.splitlines(keepends=True)
    # splitlines(keepends) keeps "\n"; a file without a trailing newline still works
    cursor = 0
    out: list[str] = []
    i = 0
    while i < len(hunks):
        match = _HUNK_RE.match(hunks[i])
        if not match:
            i += 1
            continue
        old_start = int(match.group(1))
        i += 1
        # Copy unchanged prefix (1-based old_start)
        target = max(0, old_start - 1)
        if target < cursor:
            raise ValueError(f"hunk overlaps previous apply at line {old_start}")
        out.extend(source[cursor:target])
        cursor = target
        while i < len(hunks) and not hunks[i].startswith("@@ "):
            line = hunks[i]
            i += 1
            if line.startswith("\\"):
                continue
            if line.startswith(" "):
                have = source[cursor] if cursor < len(source) else ""
                if have.rstrip("\n") != line[1:]:
                    raise ValueError(
                        f"context mismatch at line {cursor + 1}: expected {line[1:]!r}"
                    )
                _seal_tail(out)
                out.append(have)
                cursor += 1
            elif line.startswith("-"):
                have = source[cursor] if cursor < len(source) else ""
                if have.rstrip("\n") != line[1:]:
                    raise ValueError(
                        f"deletion mismatch at line {cursor + 1}: expected {line[1:]!r}"
                    )
                cursor += 1
            elif line.startswith("+"):
                addition = line[1:]
                _seal_tail(out)
                if not addition.endswith("\n") and (cursor < len(source) or out):
                    addition += "\n"
                out.append(addition)
            else:
                raise ValueError(f"malformed hunk line: {line!r}")
    out.extend(source[cursor:])
    return "".join(out)


class ApplyPatchTool(Tool):
    name = "apply_patch"
    description = (
        "Apply a unified diff to files inside the workspace. Prefer this over "
        "shelling out to `git apply` or `patch`. One file failing aborts the rest."
    )
    permission = WRITE
    required = ["diff"]
    parameters = {
        "diff": {"type": "string", "description": "Unified diff (---/+++ / @@ hunks)"},
    }

    def permanent_allow(self, args, ctx) -> bool:
        return True

    def subject(self, args, ctx):
        files = parse_unified_diff(str(args.get("diff") or ""))
        return files[0][0] if files else ""

    def subjects(self, args, ctx):
        """Every file the patch touches — rules must judge each of them."""
        return [path for path, _hunks in parse_unified_diff(str(args.get("diff") or ""))]

    def gate_summary(self, args, ctx):
        files = parse_unified_diff(str(args.get("diff") or ""))
        names = ", ".join(short_path(expand_path(p, ctx.cwd), ctx.cwd) for p, _ in files[:4])
        extra = f" +{len(files) - 4}" if len(files) > 4 else ""
        return f"apply_patch {names}{extra}" if names else "apply_patch"

    def preview(self, args, ctx):
        diff = str(args.get("diff") or "")
        lines = diff.splitlines()[:24]
        more = f"\n  … +{len(diff.splitlines()) - 24} more lines" if len(diff.splitlines()) > 24 else ""
        return "\n".join(lines) + more

    def checkpoint_paths(self, args, ctx):
        return [expand_path(p, ctx.cwd) for p, _ in parse_unified_diff(str(args.get("diff") or ""))]

    def in_fence(self, args, ctx):
        root = ctx.cwd.resolve()
        for path, _hunks in parse_unified_diff(str(args.get("diff") or "")):
            target = expand_path(path, ctx.cwd).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                return False
        return True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        diff = str(args.get("diff") or "")
        files = parse_unified_diff(diff)
        if not files:
            return ToolResult("No +++ files found in the diff.", is_error=True)
        root = ctx.cwd.resolve()
        applied: list[str] = []
        for rel, hunks in files:
            path = expand_path(rel, ctx.cwd)
            try:
                path.resolve().relative_to(root)
            except ValueError:
                return ToolResult(f"Refusing to patch outside the workspace: {path}", is_error=True)
            if path.exists():
                original = path.read_text(encoding="utf-8", errors="replace")
            else:
                original = ""
            try:
                updated = _apply_hunks(original, hunks)
            except ValueError as exc:
                return ToolResult(f"{short_path(path, ctx.cwd)}: {exc}", is_error=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(updated, encoding="utf-8")
            applied.append(short_path(path, ctx.cwd))
        return ToolResult(f"Applied patch to {len(applied)} file(s): {', '.join(applied)}")
