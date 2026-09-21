"""Ship-complete gate: mutating source without evidence is not done.

The model will take the shortest path that makes the transcript look
finished — skip tests, skip the screen, stub a layer. The system prompt
forbids that; this module is the mechanical half. One nudge per turn,
then the model may still answer (so a capped loop cannot deadlock).
"""

from __future__ import annotations

import json
from pathlib import Path

MUTATION_TOOLS = frozenset({"write_file", "edit_file", "apply_patch",
                            "str_replace_based_edit_tool"})
VERIFY_TOOLS = frozenset({
    "run_shell",
    "lsp_diagnostics",
    "browser_observe",
    "browser_screenshot",
    "browser_open",
    "desktop_screenshot",
    "desktop_find",
    "computer",
    "bash",
})
# Notes, skills, and data dumps are not "the product". Source and UI files are.
SOURCE_SUFFIXES = frozenset({
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html",
    ".java", ".js", ".jsx", ".kt", ".mjs", ".cjs", ".php", ".py", ".pyi",
    ".rb", ".rs", ".scss", ".sql", ".svelte", ".swift", ".toml", ".ts",
    ".tsx", ".vue", ".yaml", ".yml",
})

VERIFY_NUDGE = (
    "[verify-gate] You mutated source this turn ({paths}) but did not run "
    "verification. Do not claim the work is done. Call `run_shell` for the "
    "project tests/build and/or `lsp_diagnostics` on the files you changed. "
    "For UI/TUI/CSS/layout work, look at the real screen (`browser_observe`, "
    "a screenshot, or a compositor dump) — reading the source is not visual "
    "proof. If a layer (database, backend, frontend, auth, migrations) is "
    "still missing, add it or say it is out of scope; do not stub it silently. "
    "Then answer from evidence gathered this turn."
)

# A request that implies a multi-layer PRODUCT: the mechanical half of
# "never silently skip the database / backend / frontend". When the turn's
# user prompt matches, the nudge additionally demands a stack checklist.
SYSTEM_KEYWORDS = (
    "网站", "网页", "应用", "平台", "系统", "后台", "仪表盘", "看板",
    "前端", "后端", "数据库", "登录", "鉴权", "部署", "全栈",
    "web", "app", "website", "webapp", "platform", "system", "dashboard",
    "frontend", "backend", "full-stack", "fullstack", "database", "api",
    "login", "auth", "deploy",
)

STACK_CHECKLIST = (
    " Stack checklist before you answer: for each layer the request implies "
    "(data store / backend API / frontend UI / auth / tests), state whether "
    "the code EXISTS ON DISK NOW or was EXPLICITLY declared out of scope. "
    "A layer that is neither must be added now — a demo file that only "
    "resembles the layer does not count."
)


def implies_system(prompt: str) -> bool:
    lowered = (prompt or "").lower()
    return any(keyword in lowered for keyword in SYSTEM_KEYWORDS)


def path_from_arguments(arguments: str | None) -> str:
    try:
        data = json.loads(arguments or "{}")
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("path") or "")


def is_source_path(path: str) -> bool:
    if not path:
        return False
    return Path(path).suffix.lower() in SOURCE_SUFFIXES


def mutation_paths(name: str, arguments: str | None) -> list[str]:
    """Source paths a mutation call touched (non-source entries dropped).

    apply_patch carries its targets inside the diff, not a "path" argument, so
    parsing only the top-level key meant the system prompt's recommended edit
    tool never armed the gate at all.
    """
    if name == "apply_patch":
        try:
            data = json.loads(arguments or "{}")
        except (TypeError, json.JSONDecodeError):
            return []
        diff = str(data.get("diff") or "") if isinstance(data, dict) else ""
        from oaset.tools.patch import parse_unified_diff

        return [path for path, _hunks in parse_unified_diff(diff)
                if is_source_path(path)]
    path = path_from_arguments(arguments)
    return [path] if is_source_path(path) else []


def is_mutation(name: str, arguments: str | None, is_error: bool) -> bool:
    return (name in MUTATION_TOOLS) and (not is_error) and bool(mutation_paths(name, arguments))


def is_verify(name: str) -> bool:
    return name in VERIFY_TOOLS


def needs_verify(mutated_paths: list[str] | tuple[str, ...], verified: bool) -> bool:
    return bool(mutated_paths) and not verified


def nudge_text(mutated_paths: list[str] | tuple[str, ...], system: bool = False,
               unmet: list[str] | None = None) -> str:
    seen: list[str] = []
    for path in mutated_paths:
        short = Path(path).name or path
        if short not in seen:
            seen.append(short)
        if len(seen) >= 6:
            break
    text = VERIFY_NUDGE.format(paths=", ".join(seen) or "source files")
    if system:
        text += STACK_CHECKLIST
    if unmet:
        items = "; ".join(item for item in unmet[:8] if item)
        text += (
            "\n\n[requirements] The plan still lists scenarios that are not "
            f"done: {items}. Complete them, or state explicitly why each one "
            "is out of scope — an unfinished scenario is not a finished task."
        )
    return text
