"""Tool registry: schemas, permission gating, dispatch, output truncation."""

from __future__ import annotations

import contextlib
import inspect
import json
from pathlib import Path
from typing import Any

from oaset.tools.base import (
    EXEC,
    READ,
    WRITE,
    AutoGate,
    PermissionGate,
    ReadOnlyGate,
    Tool,
    ToolContext,
    ToolResult,
    combined_verdict,
)
from oaset.utils import truncate_text

# Heavy tool modules load LAZILY (PEP 562): importing this package used to
# drag web→httpx and subagent→agent→loop onto the FIRST-FRAME path — the
# paint needs only AutoGate; the registry build happens in the boot worker.
# Names below resolve on first attribute access, same objects thereafter.
_LAZY_EXPORTS = {
    "EditFileTool": "oaset.tools.fs",
    "GlobTool": "oaset.tools.fs",
    "GrepTool": "oaset.tools.fs",
    "ListDirTool": "oaset.tools.fs",
    "ReadFileTool": "oaset.tools.fs",
    "WriteFileTool": "oaset.tools.fs",
    "CodeOutlineTool": "oaset.tools.outline",
    "ApplyPatchTool": "oaset.tools.patch",
    "RunShellTool": "oaset.tools.shell",
    "SkillCreateTool": "oaset.tools.skill_create",
    "TaskTool": "oaset.tools.subagent",
    "TodoWriteTool": "oaset.tools.todo",
    "is_untrusted_source": "oaset.tools.trust",
    "wrap_untrusted": "oaset.tools.trust",
    "WebFetchTool": "oaset.tools.web",
    "WebSearchTool": "oaset.tools.web",
}


def __getattr__(name: str):
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_path), name)
    globals()[name] = value  # resolve once; later accesses are direct
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY_EXPORTS))

__all__ = [
    "EXEC",
    "READ",
    "WRITE",
    "AutoGate",
    "PermissionGate",
    "ReadOnlyGate",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "default_tools",
]


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _read_scope(path: Path) -> str:
    """The directory whose approval covers this read target."""
    p = path.resolve()
    return str(p if p.is_dir() else p.parent)


def workspace_allow_tools(entries: list[str], cwd: Path) -> set[str]:
    """Names from the durable allowlist that apply to THIS workspace.

    Entries are either legacy bare tool names (global — kept working for
    configs written before workspace binding) or ``name::bucket`` records
    written by 'always approve', which only grant inside the workspace whose
    bucket they pin."""
    from oaset.utils import workdir_bucket

    bucket = workdir_bucket(cwd)
    out: set[str] = set()
    for entry in entries or []:
        name, sep, wb = entry.partition("::")
        if not sep or wb == bucket:
            out.add(name)
    return out


def default_tools() -> list[Tool]:
    # local imports: same-module globals do NOT go through __getattr__, and
    # this build belongs to the boot worker anyway — never the first frame
    from oaset.tools.fs import (
        EditFileTool,
        GlobTool,
        GrepTool,
        ListDirTool,
        ReadFileTool,
        WriteFileTool,
    )
    from oaset.tools.outline import CodeOutlineTool
    from oaset.tools.patch import ApplyPatchTool
    from oaset.tools.shell import RunShellTool
    from oaset.tools.skill_create import SkillCreateTool
    from oaset.tools.subagent import TaskTool
    from oaset.tools.todo import TodoWriteTool
    from oaset.tools.web import WebFetchTool, WebSearchTool

    tools: list[Tool] = [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        GlobTool(),
        GrepTool(),
        ListDirTool(),
        CodeOutlineTool(),
        RunShellTool(),
        WebFetchTool(),
        WebSearchTool(),
        TodoWriteTool(),
        TaskTool(),
        SkillCreateTool(),
        ApplyPatchTool(),
    ]
    tools.extend(_late_bound_tools())
    return tools


def _late_bound_tools() -> list[Tool]:
    """Tools whose modules depend on oaset.tools (registered without import cycles)."""
    from oaset.cron import CronTool
    from oaset.history import SearchHistoryTool
    from oaset.lsp import LspDiagnosticsTool
    from oaset.memory import MemoryTool
    from oaset.tools.ask_user import AskUserQuestionTool, EnterPlanModeTool, ExitPlanModeTool
    from oaset.tools.computer import computer_tools
    from oaset.tools.computer_use import computer_use_tools
    from oaset.tools.git_tool import GitStatusTool
    from oaset.tools.parallel_subagent import ParallelTaskTool
    from oaset.tools.repo_map import RepoMapTool
    from oaset.tools.tasks import TaskListTool, TaskOutputTool, TaskStopTool

    return [MemoryTool(), SearchHistoryTool(), RepoMapTool(), CronTool(), LspDiagnosticsTool(),
            ParallelTaskTool(), GitStatusTool(), TaskListTool(), TaskOutputTool(),
            TaskStopTool(), AskUserQuestionTool(), EnterPlanModeTool(),
            ExitPlanModeTool(), *computer_tools(), *computer_use_tools()]


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None, gate: PermissionGate | None = None):
        # tools=[] starts EMPTY (sub-agent registries); None means the default set
        self._all: dict[str, Tool] = {t.name: t for t in (default_tools() if tools is None else tools)}
        self.gate = gate or AutoGate()
        self.ctx: ToolContext = ToolContext(cwd=Path.cwd())  # replaced by bind()
        self.enabled: list[str] | None = None  # None = everything allowed
        self.disabled: set[str] = set()
        self.tools: dict[str, Tool] = dict(self._all)
        self._schema_cache: list[dict[str, Any]] | None = None

    def bind(self, ctx: ToolContext) -> None:
        ctx.gate = self.gate  # computer-use tools route approvals through it
        self.ctx = ctx

    def add_tool(self, tool: Tool) -> None:
        """Register an extra tool (MCP adapters, subagent extensions)."""
        self._all[tool.name] = tool
        if self._is_active(tool.name):
            self.tools[tool.name] = tool
        self._schema_cache = None

    def remove_tool(self, name: str) -> None:
        self._all.pop(name, None)
        self.tools.pop(name, None)
        self._schema_cache = None

    def set_toggles(self, enabled: list[str] | None = None, disabled: set[str] | list[str] | None = None) -> None:
        """Enable a whitelist and/or blacklist tool names.

        A disabled-only call (the native-skill step-back) must NOT clear an
        existing whitelist: it used to reset self.enabled to None, silently
        re-enabling every tool the user had fenced off.
        """
        if enabled is not None or disabled is None:
            self.enabled = list(enabled) if enabled else None
        self.disabled = set(disabled or ())
        self.tools = {
            name: tool for name, tool in self._all.items() if self._is_active(name)
        }
        self._schema_cache = None

    def _is_active(self, name: str) -> bool:
        if name in self.disabled:
            return False
        return self.enabled is None or name in self.enabled

    def schemas(self) -> list[dict[str, Any]]:
        if self._schema_cache is None:
            self._schema_cache = [tool.schema() for tool in self.tools.values()]
        return self._schema_cache

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def _denial_message(self) -> str:
        """Why a call was denied — honest about WHO denied it.

        A headless gate (ReadOnlyGate in `oaset -p` without --yolo) denied
        without anyone being asked; reporting "the user declined" told the
        model and the user a falsehood about an interaction that never
        happened.
        """
        from oaset.i18n import t

        if getattr(self.gate, "headless", False):
            # "[no approver]" is a stable machine-readable marker (the loop
            # matches its denials by prefix); the body follows ui_language
            return "[no approver] " + t("denied_headless_msg")
        return "[denied by user] " + t("denied_msg")

    async def dispatch(self, name: str, arguments: str) -> ToolResult:
        """Parse → gate → execute → truncate. Always returns a ToolResult."""
        ctx = self.ctx
        assert ctx is not None, "ToolRegistry.bind() must be called before dispatch"
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(f"Unknown tool '{name}'. Available: {', '.join(sorted(self.tools))}", is_error=True)
        try:
            args = json.loads(arguments) if arguments and arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return ToolResult(f"Invalid JSON arguments for {name}: {exc}", is_error=True)
        if not isinstance(args, dict):
            return ToolResult(f"Arguments for {name} must be a JSON object.", is_error=True)
        missing = [k for k in getattr(tool, "required", []) or [] if k not in args]
        if missing:
            return ToolResult(
                f"Missing required argument(s) {', '.join(missing)} for {name}. "
                f"Expected parameters: {', '.join(tool.parameters)}.",
                is_error=True,
            )

        checker = getattr(tool, "blocked_reason", None)
        if checker is not None:
            reason = checker(args, ctx)
            if reason:
                return ToolResult(f"[blocked] {reason}", is_error=True)

        if ctx.mode == "plan" and tool.permission != READ:
            return ToolResult(
                "[plan mode] This operation is blocked because plan mode is read-only. "
                "The user can switch back with Shift-Tab or /mode default.",
                is_error=True,
            )

        verdict = combined_verdict(ctx.rules, tool, args, ctx)
        if verdict == "deny":
            from oaset.i18n import t

            return ToolResult(f"[denied by rule] {t('denied_msg')}", is_error=True)

        if tool.permission != READ:
            # danger tier (shell command classification): danger.py promises
            # "always allow is suppressed for the session", but the session
            # grant below ignored it — one 'a' on any command let sudo/rm -rf
            # run silently for the rest of the session.
            level = "normal"
            danger_level = getattr(tool, "danger_level", None)
            if danger_level is not None:
                with contextlib.suppress(Exception):
                    level = str(danger_level(args, ctx) or "normal")
            dangerous_now = level == "dangerous"
            outside = not tool.in_fence(args, ctx)
            # Ask BY DEFAULT. A shortcut needs an explicit reason, and each
            # reason has its own limits: a rule cannot blanket the danger
            # tier; a session grant cannot blanket an out-of-fence write.
            shortcut = False
            if verdict == "allow":
                shortcut = not dangerous_now
            elif ctx.mode == "auto":
                shortcut = True  # --yolo: the user opted out of gating
            elif (verdict != "ask" and tool.name in ctx.session_allowed
                  and not dangerous_now and not outside):
                # an explicit rule asking for confirmation OUTRANKS a session
                # grant: "ask" is the one verdict no shortcut may swallow
                shortcut = True
            if not shortcut:
                summary = tool.gate_summary(args, ctx)
                if outside:
                    summary += "  ⚠ OUTSIDE the workspace directory"
                decision = await self.gate.request(
                    tool.name, tool.permission, summary,
                    preview=tool.preview(args, ctx),
                )
                if decision == "deny":
                    return ToolResult(self._denial_message(), is_error=True)
                if decision == "always":
                    # "always" means at least this session — EXCEPT dangerous
                    # calls, which per danger.py stay ask-every-time. Durable
                    # grants additionally require a workspace-fenced, normal
                    # call (an out-of-fence "always" used to be persisted and
                    # blanketed every later write).
                    if not dangerous_now:
                        ctx.session_allowed.add(tool.name)
                        if (not outside and tool.permanent_allow(args, ctx)
                                and ctx.persist_allow is not None):
                            ctx.persist_allow(tool.name)
        elif ctx.mode != "auto" and verdict != "allow":
            read_paths = tool.fence_paths(args, ctx)
            outside_reads = bool(read_paths) and not all(_within(p, ctx.cwd) for p in read_paths)
            # An explicit `ask` rule must reach the gate even for READ tools —
            # the branch below only prompted for out-of-workspace reads, so
            # `ask web_fetch(...)` etc. used to be silently ineffective.
            if verdict == "ask" or outside_reads:
                scopes = {_read_scope(p) for p in read_paths}
                if verdict == "ask" or not scopes.issubset(ctx.session_paths):
                    summary = tool.gate_summary(args, ctx)
                    if outside_reads:
                        summary += "  ⚠ OUTSIDE the workspace directory"
                    decision = await self.gate.request(
                        tool.name, READ, summary, preview=tool.preview(args, ctx),
                    )
                    if decision == "deny":
                        return ToolResult(self._denial_message(), is_error=True)
                    if decision == "always":
                        ctx.session_paths.update(scopes)
        await self._checkpoint(tool, args, ctx)
        import time as _time

        _t0 = _time.monotonic()
        try:
            result = await tool.run(args, ctx)
        except Exception as exc:  # tool bugs must not kill the agent loop
            result = ToolResult(f"Tool {name} crashed: {type(exc).__name__}: {exc}", is_error=True)
        audit = getattr(ctx, "audit", None)
        if audit is not None:
            with contextlib.suppress(Exception):  # audit must never break a turn
                audit(name, arguments, bool(result.is_error),
                      (_time.monotonic() - _t0) * 1000.0)
        if not result.is_error and tool.permission == WRITE:
            result.output = await self._lsp_inject(tool, args, ctx, result.output)
        if not result.is_error:
            full = result.output
            result.output = truncate_text(full, ctx.output_limit)
            if len(result.output) < len(full):
                # Truncation must be recoverable: the full text is
                # spilled to disk and the marker names the file, so the model
                # can read the rest back with read_file(offset, limit).
                spilled = await self._spill(ctx, name, full)
                if spilled:
                    from oaset.i18n import t

                    result.output += t("spill_saved_note", path=spilled)
        from oaset.tools.trust import is_untrusted_source, wrap_untrusted

        if is_untrusted_source(name):
            # Frame external content once, here, so every consumer (TUI, CLI,
            # sub-agents, gateway) sees the same trust boundary.
            result.output = wrap_untrusted(name, result.output)
        return result

    async def _spill(self, ctx: ToolContext, name: str, text: str) -> str:
        """Persist pre-truncation output on the ordered IO thread; "" on failure."""
        out_dir = ctx.session_state.get("tool_output_dir")
        if not out_dir:
            return ""
        from oaset.utils import run_io, write_side_output

        try:
            return await run_io(write_side_output, out_dir, name, text)
        except Exception:
            return ""

    async def _lsp_inject(self, tool: Tool, args: dict, ctx: ToolContext, output: str) -> str:
        """Attach LSP publishDiagnostics for the touched files."""
        from oaset.lsp import lsp_summary_for

        manager = ctx.session_state.get("lsp_manager")
        if manager is None or not getattr(manager, "connections", {}):
            return output
        summaries = []
        for path in tool.checkpoint_paths(args, ctx):
            summary = await lsp_summary_for(path, ctx)
            if summary:
                summaries.append(summary)
        return output + ("\n" + "\n".join(summaries) if summaries else "")

    async def _checkpoint(self, tool: Tool, args: dict[str, Any], ctx: ToolContext) -> None:
        """Snapshot files about to be modified (WRITE tools) into the session store.

        File reads and the store write both run on the ordered IO thread (S2):
        snapshotting a multi-megabyte file must not stall the event loop."""
        from oaset.utils import run_io

        sink = ctx.session_state.get("checkpoint_sink")
        if sink is None or tool.permission != WRITE:
            return
        snapshot: dict[str, str | None] = {}
        for path in tool.checkpoint_paths(args, ctx):
            try:
                snapshot[str(path)] = await run_io(_read_snapshot, path)
            except OSError:
                continue
        if snapshot:
            try:
                outcome = sink(snapshot)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                pass  # checkpointing must never block the tool


def _read_snapshot(path: Path) -> str | None:
    """Read one file for a checkpoint snapshot (runs on the ordered IO thread)."""
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
