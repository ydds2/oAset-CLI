"""Filesystem tools: read/write/edit/glob/grep/list_dir with workspace fencing."""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import glob as globlib
import html
import os
import re
import shutil
import time
from pathlib import Path

from oaset.tools.base import READ, WRITE, Tool, ToolResult
from oaset.utils import (
    expand_path,
    one_line,
    read_text_smart,
    short_path,
    truncate_text,
)

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache"}
MAX_READ_BYTES = 256 * 1024
MAX_RESULTS = 500
GREP_MAX_MATCHES = 200
GREP_TIME_BUDGET = 20  # seconds for the python fallback walk (S1)
LINE_CUT = 300


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _path_param(parameters: dict, description: str, required: bool = True) -> None:
    parameters["path"] = {"type": "string", "description": description}
    if required:
        parameters.setdefault("required", [])
        # required list is handled via class attribute `required` instead


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a text file. Returns up to 256KB of content with line numbers. "
        "Use offset/limit to page through big files."
    )
    permission = READ
    required = ["path"]
    parameters = {
        "path": {"type": "string", "description": "File path (absolute or relative to cwd)"},
        "offset": {"type": "integer", "description": "1-based start line"},
        "limit": {"type": "integer", "description": "Max lines to return"},
    }

    def subject(self, args, ctx):
        return str(args.get("path", ""))

    def gate_summary(self, args, ctx):
        return f"read_file {short_path(expand_path(args['path'], ctx.cwd), ctx.cwd)}"

    def fence_paths(self, args, ctx):
        return [expand_path(args["path"], ctx.cwd)]

    async def run(self, args, ctx):
        path = expand_path(args["path"], ctx.cwd)
        try:
            if not path.exists():
                return ToolResult(f"File not found: {path}", is_error=True)
            # bounded probe: reading the WHOLE file just to sniff a NUL byte
            # made read_file on a multi-GB log an OOM
            with path.open("rb") as _fh:
                _probe = _fh.read(8192)
            if 0 in _probe:
                return ToolResult("Binary file — read_file only handles text.", is_error=True)
            text, used_enc = read_text_smart(path, max_bytes=MAX_READ_BYTES + 1)
        except OSError as exc:
            return ToolResult(f"Cannot read {path}: {exc}", is_error=True)
        lines = text.splitlines()
        offset = max(1, int(args.get("offset") or 1))
        limit = int(args.get("limit") or 2000)
        chosen = lines[offset - 1 : offset - 1 + limit]
        numbered = "\n".join(f"{offset + i:6d}\t{line}" for i, line in enumerate(chosen))
        header = f"{short_path(path, ctx.cwd)} ({len(lines)} lines)"
        return ToolResult(f"{header}\n{numbered}")


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create or overwrite a file with the given content. Parent directories are created."
    permission = WRITE
    required = ["path", "content"]
    # workspace-fenced file creation may be durably allowed (bound to this workspace)
    def permanent_allow(self, args, ctx) -> bool:
        return True
    parameters = {
        "path": {"type": "string", "description": "File path (absolute or relative to cwd)"},
        "content": {"type": "string", "description": "Full file content to write"},
    }

    def subject(self, args, ctx):
        return str(args.get("path", ""))

    def gate_summary(self, args, ctx):
        return f"write_file {short_path(expand_path(args['path'], ctx.cwd), ctx.cwd)}"

    def preview(self, args, ctx):
        lines = str(args.get("content", "")).splitlines()
        shown = lines[:10]
        body = "\n".join(f"+ {line}" for line in shown)
        if len(lines) > 10:
            body += f"\n  … +{len(lines) - 10} more lines"
        return f"{short_path(expand_path(args['path'], ctx.cwd), ctx.cwd)}\n{body}"

    def checkpoint_paths(self, args, ctx):
        return [expand_path(args["path"], ctx.cwd)]

    def in_fence(self, args, ctx):
        return _within(expand_path(args["path"], ctx.cwd), ctx.cwd)

    async def run(self, args, ctx):
        path = expand_path(args["path"], ctx.cwd)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"], encoding="utf-8")
        except OSError as exc:
            return ToolResult(f"Cannot write {path}: {exc}", is_error=True)
        n = len(args["content"].splitlines())
        preview_lines = args["content"].splitlines()[:8]
        preview = "\n".join(f"+ {line}" for line in preview_lines)
        more = f"\n  … +{n - 8} more lines" if n > 8 else ""
        return ToolResult(
            f"Wrote {short_path(path, ctx.cwd)} ({n} lines, {len(args['content'])} chars)\n{preview}{more}"
        )


def _diff_side(text: str, glyph: str) -> list[str]:
    """Preview lines for the edit diff output (head + overflow marker)."""
    lines = text.splitlines() or [""]
    shown = lines[:12]
    out = [f"{glyph} {line}" for line in shown]
    if len(lines) > 12:
        out.append(f"  … +{len(lines) - 12} more lines")
    return out


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Replace an exact unique snippet in a file. old_text must match exactly once "
        "unless replace_all is true."
    )
    permission = WRITE
    required = ["path", "old_text", "new_text"]
    # workspace-fenced in-place edit may be durably allowed (bound to this workspace)
    def permanent_allow(self, args, ctx) -> bool:
        return True
    parameters = {
        "path": {"type": "string", "description": "File to edit"},
        "old_text": {"type": "string", "description": "Exact text to find (include enough context to be unique)"},
        "new_text": {"type": "string", "description": "Replacement text"},
        "replace_all": {"type": "boolean", "description": "Replace every occurrence"},
    }

    def subject(self, args, ctx):
        return str(args.get("path", ""))

    def gate_summary(self, args, ctx):
        return f"edit_file {short_path(expand_path(args['path'], ctx.cwd), ctx.cwd)}"

    def preview(self, args, ctx):
        old_lines = str(args.get("old_text", "")).splitlines()[:8]
        new_lines = str(args.get("new_text", "")).splitlines()[:8]
        nl = chr(10)
        body = nl.join(f"- {line}" for line in old_lines)
        body += nl + nl.join(f"+ {line}" for line in new_lines)
        return f"{short_path(expand_path(args['path'], ctx.cwd), ctx.cwd)}{nl}{body}"

    def checkpoint_paths(self, args, ctx):
        return [expand_path(args["path"], ctx.cwd)]

    def in_fence(self, args, ctx):
        return _within(expand_path(args["path"], ctx.cwd), ctx.cwd)

    async def run(self, args, ctx):
        path = expand_path(args["path"], ctx.cwd)
        if not path.is_file():
            return ToolResult(f"File not found: {path}", is_error=True)
        try:
            text, original_enc = read_text_smart(path)
        except Exception:
            text, original_enc = path.read_text(encoding="utf-8", errors="replace"), "utf-8"
        old, new = args["old_text"], args["new_text"]
        if not old:
            return ToolResult("old_text must not be empty.", is_error=True)
        count = text.count(old)
        if count == 0 and not args.get("replace_all"):
            # P3-4 (fuzzy apply): exact match failed, so try a
            # whitespace-tolerant match before giving up — models routinely
            # drift indentation or drop trailing spaces on long edits.
            return self._fuzzy_apply(path, text, old, new, original_enc, ctx)
        if count == 0:
            return ToolResult("old_text not found in file — copy it exactly from read_file output.", is_error=True)
        if count > 1 and not args.get("replace_all"):
            return ToolResult(
                f"old_text matches {count} locations; add surrounding context or pass replace_all=true.",
                is_error=True,
            )
        if args.get("replace_all"):
            updated = text.replace(old, new)
        else:
            updated = text.replace(old, new, 1)
        path.write_bytes(updated.encode(original_enc, errors="replace"))
        diff = ["diff:", *_diff_side(old, "-"), *_diff_side(new, "+")]
        replaced = count if args.get("replace_all") else 1
        return ToolResult(
            f"Edited {short_path(path, ctx.cwd)} ({replaced} replacement(s))\n" + "\n".join(diff)
        )

    def _fuzzy_apply(self, path, text, old, new, original_enc, ctx) -> ToolResult:
        """Whitespace-tolerant fallback apply (P3-4).

        Two tolerance levels, tried in order: trailing-whitespace drift, then
        full whitespace drift (indentation). One candidate applies (with a
        note); several candidates are ambiguous and are LISTED rather than
        guessed, because editing the wrong copy of a repeated block is worse
        than a second attempt. new_text is inserted verbatim — re-indenting it
        to the matched block is a future refinement, not a guess to make here.
        """
        import difflib

        file_lines = text.splitlines()
        old_lines = old.splitlines()
        n = len(old_lines)
        if n == 0:
            return ToolResult("old_text not found in file.", is_error=True)
        candidates: list[tuple[int, int, str]] = []
        for level, norm in (("trailing-whitespace", str.rstrip),
                            ("whitespace", str.strip)):
            candidates = []
            for i in range(len(file_lines) - n + 1):
                window = file_lines[i:i + n]
                # A fully identical window cannot occur here (the exact count
                # was 0), so every candidate differs only in whitespace.
                if all(norm(w) == norm(o) for w, o in zip(window, old_lines)):
                    candidates.append((i, i + n, level))
            if candidates:
                break
        if not candidates:
            closest = difflib.get_close_matches(
                old_lines[0].strip(), [ln.strip() for ln in file_lines], n=3, cutoff=0.6)
            hint = f" Closest line(s): {closest}." if closest else ""
            return ToolResult(
                f"old_text not found even with whitespace tolerance.{hint} "
                "Re-read the file and copy the block exactly.", is_error=True)
        if len(candidates) > 1:
            spots = ", ".join(f"line {start + 1}" for start, _end, _lvl in candidates)
            return ToolResult(
                f"old_text (whitespace-normalised) matches {len(candidates)} locations: "
                f"{spots}. Include more surrounding lines to make it unique.",
                is_error=True)
        start, end, level = candidates[0]
        new_lines = new.splitlines()
        updated_lines = file_lines[:start] + new_lines + file_lines[end:]
        newline = "\r\n" if "\r\n" in text else "\n"
        path.write_bytes(newline.join(updated_lines)
                         .encode(original_enc, errors="replace"))
        note = (f"Applied to {short_path(path, ctx.cwd)} via fuzzy match "
                f"({level} differences; lines {start + 1}-{end} replaced).\n")
        diff = ["diff:", *_diff_side("\n".join(file_lines[start:end]), "-"),
                *_diff_side(new, "+")]
        return ToolResult(note + "\n".join(diff))


class GlobTool(Tool):
    name = "glob"
    description = "Find files by glob pattern, e.g. **/*.py. Skips .git/node_modules/venvs. Max 500 results."
    permission = READ
    required = ["pattern"]
    parameters = {
        "pattern": {"type": "string", "description": "Glob pattern relative to the search path"},
        "path": {"type": "string", "description": "Search root (default: cwd)"},
    }

    def subject(self, args, ctx):
        return str(args.get("path", ""))

    def gate_summary(self, args, ctx):
        root = expand_path(args.get("path") or ".", ctx.cwd)
        return f"glob '{args['pattern']}' in {short_path(root, ctx.cwd)}"

    def fence_paths(self, args, ctx):
        return [expand_path(args.get("path") or ".", ctx.cwd)]

    async def run(self, args, ctx):
        root = expand_path(args.get("path") or ".", ctx.cwd)
        pattern = args["pattern"]
        # Path("/a/b") / "/etc/**" silently REPLACES the root: an absolute or
        # ".."-carrying pattern read anywhere on disk without a prompt.
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            return ToolResult(
                "[blocked] pattern must stay inside the workspace directory",
                is_error=True)
        hits: list[str] = []
        for hit in globlib.glob(str(root / pattern), recursive=True):
            p = Path(hit)
            if p.is_dir():
                continue
            if not self.in_fence({"path": str(p)}, ctx):
                continue  # symlinks etc. resolving outside the fence
            parts = set(p.parts)
            if parts & SKIP_DIRS:
                continue
            hits.append(str(p))
            if len(hits) >= MAX_RESULTS:
                break
        hits.sort()
        if not hits:
            return ToolResult(f"No matches for '{pattern}' under {short_path(root, ctx.cwd)}")
        body = "\n".join(hits)
        return ToolResult(truncate_text(f"{len(hits)} match(es):\n{body}", ctx.output_limit))


class GrepTool(Tool):
    name = "grep"
    description = "Search file contents with a regex. Uses ripgrep when available, pure-Python fallback otherwise."
    permission = READ
    required = ["pattern"]
    parameters = {
        "pattern": {"type": "string", "description": "Regular expression"},
        "path": {"type": "string", "description": "Search root (default: cwd)"},
        "include": {"type": "string", "description": "Filename glob filter, e.g. *.py"},
        "context": {"type": "integer", "description": "Context lines around matches (rg -C)"},
    }

    def subject(self, args, ctx):
        return str(args.get("path", ""))

    def gate_summary(self, args, ctx):
        root = expand_path(args.get("path") or ".", ctx.cwd)
        return f"grep '{args['pattern']}' in {short_path(root, ctx.cwd)}"

    def fence_paths(self, args, ctx):
        return [expand_path(args.get("path") or ".", ctx.cwd)]

    async def run(self, args, ctx):
        root = expand_path(args.get("path") or ".", ctx.cwd)
        pattern = args["pattern"]
        include = args.get("include")
        rg = shutil.which("rg")
        if rg:
            cmd = [rg, "--no-heading", "-n", "-S", "--max-count", "20", "--", pattern, str(root)]
            if include:
                cmd[6:6] = ["-g", include]
            ctx_lines = int(args.get("context") or 0)
            if ctx_lines:
                cmd[2:2] = ["-C", str(ctx_lines)]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError:
                proc = None
            if proc is not None:
                try:
                    out, _err = await asyncio.wait_for(proc.communicate(), timeout=30)
                except TimeoutError:
                    # rg must not survive its budget (S5): the old code dropped
                    # the handle here and leaked the process. Kill the tree,
                    # then fall through to the bounded python fallback.
                    from oaset.utils import kill_tree_async

                    await kill_tree_async(proc)
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    proc = None
                except ProcessLookupError:
                    proc = None
            if proc is not None and (proc.returncode in (0, 1)):
                text = out.decode("utf-8", errors="replace").strip()
                if text:
                    return ToolResult(truncate_text(text, ctx.output_limit))
                if proc.returncode == 1:
                    return ToolResult(f"No matches for '{pattern}'.")
        return await self._python_fallback(pattern, root, include, ctx)

    async def _python_fallback(self, pattern, root, include, ctx) -> ToolResult:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return ToolResult(f"Invalid regex: {exc}", is_error=True)
        try:
            matches = await asyncio.wait_for(
                asyncio.to_thread(self._walk_matches, rx, root, include, ctx),
                timeout=GREP_TIME_BUDGET + 5)
        except TimeoutError:
            return ToolResult(
                f"Search exceeded its {GREP_TIME_BUDGET}s budget on this tree. "
                "Narrow the path or install ripgrep.")
        if not matches:
            return ToolResult(f"No matches for '{pattern}'.")
        return ToolResult(truncate_text("\n".join(matches), ctx.output_limit))

    def _walk_matches(self, rx, root, include, ctx) -> list[str]:
        """Bounded synchronous walk (runs on a worker thread): stops at the
        match cap or the time budget, whichever comes first."""
        matches: list[str] = []
        deadline = time.monotonic() + GREP_TIME_BUDGET
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fname in filenames:
                if include and not fnmatch.fnmatch(fname, include):
                    continue
                fpath = Path(dirpath) / fname
                try:
                    if fpath.stat().st_size > 1024 * 1024:
                        continue
                    with fpath.open("r", encoding="utf-8", errors="replace") as fh:
                        for lineno, line in enumerate(fh, 1):
                            if rx.search(line):
                                matches.append(f"{short_path(fpath, ctx.cwd)}:{lineno}: {one_line(line, LINE_CUT)}")
                                if len(matches) >= GREP_MAX_MATCHES:
                                    return matches
                except OSError:
                    continue
            if len(matches) >= GREP_MAX_MATCHES or time.monotonic() > deadline:
                break
        return matches


class ListDirTool(Tool):
    name = "list_dir"
    description = "List a directory overview (depth 2): subdirectories and files."
    permission = READ
    parameters = {"path": {"type": "string", "description": "Directory (default: cwd)"}}
    required = []

    def subject(self, args, ctx):
        return str(args.get("path", ""))

    def gate_summary(self, args, ctx):
        root = expand_path(args.get("path") or ".", ctx.cwd)
        return f"list_dir {short_path(root, ctx.cwd)}"

    def fence_paths(self, args, ctx):
        return [expand_path(args.get("path") or ".", ctx.cwd)]

    async def run(self, args, ctx):
        root = expand_path(args.get("path") or ".", ctx.cwd)
        if not root.is_dir():
            return ToolResult(f"Not a directory: {root}", is_error=True)

        def render(directory: Path, prefix: str, depth: int) -> list[str]:
            try:
                entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError:
                return []
            lines: list[str] = []
            for entry in entries:
                if entry.name in SKIP_DIRS:
                    continue
                if entry.is_dir():
                    lines.append(f"{prefix}{entry.name}/")
                    if depth > 1:
                        lines.extend(render(entry, prefix + "  ", depth - 1))
                else:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    lines.append(f"{prefix}{entry.name} ({size}B)")
                if len(lines) >= MAX_RESULTS:
                    lines.append("...[truncated]")
                    break
            return lines

        body = "\n".join(render(root, "", 2)) or "(empty)"
        return ToolResult(truncate_text(f"{short_path(root, ctx.cwd)}\n{body}", ctx.output_limit))


def _strip_html(data: str) -> str:
    data = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", data)
    data = re.sub(r"(?s)<[^>]+>", " ", data)
    data = html.unescape(data)
    return re.sub(r"\s+", " ", data).strip()

