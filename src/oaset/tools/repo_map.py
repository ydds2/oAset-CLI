"""Repo map tool (Aider-inspired): a ranked, symbol-aware outline of the
workspace so the model can understand a codebase before reading files.

- Python files: top-level classes/functions via ast (no tree-sitter dep)
- Markdown: first heading
- Config files: flagged as config
- Other text files: listed bare
Files are ordered most-recently-modified first (recency = relevance).
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

from oaset.tools.base import READ, Tool, ToolResult
from oaset.utils import truncate_text

SKIP = {".git", ".oaset", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".idea", ".vscode"}
CODE_SUFFIXES = {"py"}
DOC_SUFFIXES = {"md", "rst", "txt"}
CONFIG_SUFFIXES = {"json", "toml", "yaml", "yml", "ini", "cfg"}
OTHER_SUFFIXES = {"js", "ts", "tsx", "jsx", "css", "html", "sh", "bat", "ps1"}
SCAN_SUFFIXES = CODE_SUFFIXES | DOC_SUFFIXES | CONFIG_SUFFIXES | OTHER_SUFFIXES
MAX_FILES_SCAN = 4000


def _py_symbols(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace")[:100_000])
    except (OSError, SyntaxError, ValueError):
        return []
    symbols: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = ",".join(getattr(b, "id", getattr(b, "attr", "")) for b in node.bases if isinstance(b, ast.Name) or isinstance(b, ast.Attribute))
            symbols.append(f"class {node.name}({bases})" if bases else f"class {node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(f"def {node.name}()")
    return symbols


def _md_header(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("#"):
                    return [line.lstrip("#").strip()]
    except OSError:
        pass
    return []


def _scan(root: Path) -> list[tuple[float, Path, str]]:
    """(mtime, path, kind) for candidate files, newest last."""
    out: list[tuple[float, Path, str]] = []
    stack: list[Path] = [root]
    seen = 0
    while stack and seen < MAX_FILES_SCAN:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name in SKIP or entry.name.startswith("."):
                continue
            seen += 1
            try:
                if entry.is_dir():
                    stack.append(entry)
                    continue
                if not entry.is_file():
                    continue
                suffix = entry.suffix.lower().lstrip(".")
                if suffix not in SCAN_SUFFIXES:
                    continue
                out.append((entry.stat().st_mtime, entry, suffix))
            except OSError:
                continue
    return out


class RepoMapTool(Tool):
    name = "repo_map"
    description = (
        "Workspace map: files with their top-level classes/functions (Python) or "
        "headings (Markdown), most recently modified first. Call this FIRST to "
        "understand a codebase before reading individual files."
    )
    permission = READ
    required = []
    parameters = {
        "max_files": {"type": "integer", "description": "Max files in the map (default 40)"},
    }

    async def run(self, args, ctx):
        root = ctx.cwd
        max_files = int(args.get("max_files") or 40)
        entries = _scan(root)
        entries.sort(key=lambda e: e[0], reverse=True)

        lines: list[str] = []
        shown = 0
        for mtime, path, suffix in entries:
            if shown >= max_files:
                break
            rel = path.relative_to(root).as_posix()
            age = time.strftime("%m-%d", time.localtime(mtime))
            if suffix == "py":
                symbols = _py_symbols(path)
                body = "  ".join(symbols[:8]) if symbols else ""
                lines.append(f"{rel} ({age})" + (f":  {body}" if body else ""))
            elif suffix in DOC_SUFFIXES:
                header = _md_header(path)
                lines.append(f"{rel} ({age})" + (f":  {header[0]}" if header else ""))
            elif suffix in CONFIG_SUFFIXES:
                lines.append(f"{rel} ({age})  [config]")
            else:
                lines.append(f"{rel} ({age})")
            shown += 1
        if not lines:
            return ToolResult("Empty workspace — no files to map.")
        return ToolResult(truncate_text(
            f"repo map ({shown} files, newest first):\n" + "\n".join(lines), ctx.output_limit))
