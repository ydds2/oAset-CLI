"""code_outline — the on-demand structural index (the third school of repo
understanding).

Aider's eager map spends context on the whole repo up front; agentic grep
discovers well but burns tokens wandering. This tool is the hybrid: the model
asks for structure WHERE it is looking, and gets a precise, transparent
symbol index — classes/functions with line numbers, no bodies — at ~1-2% of
the file's read cost. Python parses via the stdlib ``ast`` (exact); other
languages fall back to a heuristic line scanner, clearly labelled as such.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from oaset.i18n import t
from oaset.tools.base import READ, Tool, ToolContext, ToolResult
from oaset.utils import expand_path, truncate_text

MAX_FILES = 40
MAX_LINES = 160

# generic symbol shapes for non-Python sources: def/func/fn/class/struct/
# interface/impl/typedef + TS/Java-style members. Heuristic by design and
# labelled as such in the output.
_GENERIC = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(def |function |func |fn |class |struct |interface |enum |impl |"
    r"public [\w<>\[\]]+ \w+\(|private [\w<>\[\]]+ \w+\()")


def _python_outline(path: Path) -> list[str] | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        return None
    lines: list[str] = []

    def signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        try:
            args = [a.arg for a in node.args.args]
            return f"({', '.join(args)})"
        except AttributeError:
            return "(...)"

    def walk(body: list[ast.stmt], prefix: str, indent: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lines.append(f"{indent}def {prefix}{node.name}{signature(node)}  L{node.lineno}")
            elif isinstance(node, ast.ClassDef):
                bases = ""
                if node.bases:
                    names = ", ".join(ast.unparse(b) for b in node.bases
                                      if hasattr(ast, "unparse"))
                    bases = f"({names})" if names else ""
                lines.append(f"{indent}class {node.name}{bases}  L{node.lineno}")
                walk(node.body, "", indent + "  ")

    walk(tree.body, "", "")
    return lines


def _generic_outline(path: Path) -> list[str]:
    out: list[str] = []
    try:
        for n, raw in enumerate(path.read_text(encoding="utf-8",
                                               errors="replace").splitlines(), 1):
            if _GENERIC.match(raw):
                out.append(f"{raw.strip()[:100]}  L{n}")
            if len(out) >= MAX_LINES:
                break
    except OSError:
        return []
    return out


def _outline_for(path: Path) -> tuple[str, list[str]]:
    if path.suffix == ".py":
        exact = _python_outline(path)
        if exact is not None:
            return ("ast", exact)
    return ("heuristic", _generic_outline(path))


class CodeOutlineTool(Tool):
    name = "code_outline"
    description = (
        "Structural index of a file or directory: classes/functions with line "
        "numbers, no bodies — see the skeleton before reading, or map an "
        "unfamiliar tree cheaply. Python is parsed exactly (ast); other "
        "languages use a labelled heuristic."
    )
    permission = READ
    required = []
    parameters = {
        "path": {"type": "string",
                 "description": "File or directory (default: cwd)"},
    }

    def subject(self, args, ctx):
        return str(args.get("path", ""))

    def fence_paths(self, args, ctx):
        target = expand_path(args.get("path") or ".", ctx.cwd)
        return [target]

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        target = expand_path(args.get("path") or ".", ctx.cwd)
        if not target.exists():
            return ToolResult(f"not found: {target}", is_error=True)

        sections: list[str] = []
        files = [target] if target.is_file() else self._source_files(target)
        if not files:
            return ToolResult(f"no source files under {target}", is_error=True)
        mode_counts: dict[str, int] = {}
        for f in files[:MAX_FILES]:
            mode, lines = _outline_for(f)
            mode_counts[mode] = mode_counts.get(mode, 0) + 1
            if lines:
                shown = truncate_text("\n".join(lines), 1600)
                # outside-workspace reads are gate-approved but still cannot be
                # made relative to cwd — fall back to the absolute path
                try:
                    display = str(f.relative_to(ctx.cwd))
                except ValueError:
                    display = str(f)
                sections.append(f"[{display}]\n{shown}")
        mode_note = " ".join(f"{k}:{v}" for k, v in sorted(mode_counts.items()))
        head = t("outline_header", files=len(files), modes=mode_note)
        if len(files) > MAX_FILES:
            head += f" (first {MAX_FILES})"
        body = "\n\n".join(sections) or "(no symbols found)"
        return ToolResult(truncate_text(f"{head}\n\n{body}", ctx.output_limit))

    @staticmethod
    def _source_files(root: Path) -> list[Path]:
        skip = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}
        files: list[Path] = []
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if any(part in skip for part in p.parts):
                continue
            files.append(p)
        return files

