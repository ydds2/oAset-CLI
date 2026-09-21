"""Slash command registry — metadata here, handlers live on the app (cmd_<name>).

`Command` carries everything the command palette needs to present a command
without executing it: category, aliases, usage, where it came from and how
risky it is. Parameter shapes live in `ARG_PROMPTS` as `ArgSpec` objects, which
are the serialisable schema both the palette form and `requires_args` dispatch
read from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from oaset.i18n import t

# Risk levels, ordered. "safe" runs immediately from the palette; anything else
# gets a confirmation page showing the final command line before it executes.
RISK_SAFE = "safe"
RISK_WRITE = "write"
RISK_DANGER = "danger"
RISK_ORDER = (RISK_SAFE, RISK_WRITE, RISK_DANGER)


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    usage: str = ""
    category: str = "general"
    aliases: tuple[str, ...] = ()
    requires_args: bool = False
    source: str = "builtin"
    risk: str = RISK_SAFE
    subcommands: tuple[str, ...] = ()
    effect: str = ""

    @property
    def handler_name(self) -> str:
        return "cmd_" + self.name.replace("-", "_")

    @property
    def needs_confirmation(self) -> bool:
        return self.risk in (RISK_WRITE, RISK_DANGER)

    def schema(self) -> dict[str, Any]:
        """Serialisable view used by the palette form and by diagnostics."""
        spec = arg_spec_for(self.name)
        return {
            "name": self.name,
            "description": self.description,
            "usage": self.usage,
            "category": self.category,
            "aliases": list(self.aliases),
            "requires_args": self.requires_args,
            "source": self.source,
            "risk": self.risk,
            "subcommands": list(self.subcommands),
            "effect": self.effect,
            "arg": spec.to_dict() if spec else None,
        }


COMMANDS: list[Command] = [
    Command("help", "Show shortcuts and commands", aliases=("?", "h")),
    Command("palette", "Search commands", category="interface"),
    Command("update", "Update center (read-only here; apply/rollback use the CLI)",
            "[check|list|plan|refresh] [kind] [detail]", category="system", risk=RISK_WRITE,
            subcommands=("check", "list", "plan", "refresh"),
            effect="联网刷新目录缓存 / 生成安装计划（apply 与 rollback 只经 CLI）"),
    Command("new", "Start a fresh session and clear context", risk=RISK_WRITE,
            effect="新建会话：当前会话保留在历史中，界面清空"),
    Command("sessions", "Browse and restore past sessions", risk=RISK_WRITE,
            effect="切换当前会话（未保存的草稿会丢失）", aliases=("resume",)),
    Command("model", "Switch, add or inspect models and providers",
            "<model-id>", risk=RISK_WRITE, subcommands=("add", "list", "verify"),
            effect="切换模型并写回 config.toml；无参数时打开选择/向导", aliases=("md", "models")),
    Command("think", "Reasoning depth: off / light / medium / heavy",
            "[off|light|medium|heavy]", category="model",
            effect="适配各家思考档位（Anthropic 预算 / OpenAI reasoning_effort / Gemini thinkingBudget / GLM-Qwen 开关）"),
    Command("theme", "Switch theme (built-ins + ~/.oaset/themes/*.toml)",
            risk=RISK_WRITE, effect="切换主题并写回 config.toml"),
    Command("language", "Switch UI language (zh / en)", "[zh|en]",
            category="interface", risk=RISK_WRITE, aliases=("lang", "locale"),
            effect="写回 config.toml 的 [ui] language 并刷新界面文案"),
    Command("mode", "Permission mode: default / plan (read-only) / auto",
            risk=RISK_WRITE, subcommands=("default", "plan", "auto"),
            effect="改变后续工具调用的确认策略"),
    Command("yolo", "Toggle auto-approve mode (same as /mode auto)", risk=RISK_WRITE,
            effect="开启后所有工具调用免确认执行"),
    Command("plugins", "List loaded plugins"),
    Command("reload-plugins", "Hot reload plugins (after oaset update apply)", risk=RISK_WRITE,
            effect="撤销旧插件工具/命令并重新加载插件目录"),
    Command("market", "Community plugin market: discover / installable / installed / sources",
            "[sources|installed]", category="plugin", risk=RISK_WRITE,
            subcommands=("sources", "installed"),
            effect="从活动来源浏览并安装插件（复用更新中心：网络策略+sha256+事务回滚）；来源仅 http(s) 且一次只用一个"),
    Command("desktop", "List desktop windows (UIA, gated)", category="computer", risk=RISK_WRITE,
            effect="通过受权限门控的 UIA provider 枚举桌面窗口"),
    Command("selfmaint", "体验计划：token 驱动自维护（搜索/兼容/补丁/自更新）",
            category="computer", risk=RISK_WRITE, subcommands=("enable", "run"),
            effect="按固定 token 预算运行一次自维护；安装补丁需 allow_apply 与审批"),
    Command("compact", "Compress older history into a summary", risk=RISK_WRITE,
            effect="调用模型压缩历史（不可撤销，会改写会话记录）"),
    Command("fork", "Fork the current session with full history", risk=RISK_WRITE,
            effect="复制当前会话为新会话并切换过去"),
    Command("undo", "Undo the last file change via checkpoint", risk=RISK_DANGER,
            effect="按 checkpoint 还原文件（会覆盖当前文件内容）"),
    Command("rewind", "Drop the last N turns: /rewind [N] (default 1)", risk=RISK_DANGER,
            effect="从会话记录中删除最近 N 轮对话（不可撤销）"),
    Command("agents", "Sub-agents: list, show, create, delete, reload",
            "[name|show <name>|new <name>|delete <name>|reload]", category="config",
            risk=RISK_WRITE, subcommands=("show", "new", "delete", "reload"),
            effect="新建/删除子代理会写盘并需审批；子代理可用独立模型与工具白名单"),
    Command("mcp", "MCP servers: status, add, enable/disable, remove, reload",
            "<list|add|remove|enable|disable|reload|roots>", category="system",
            risk=RISK_WRITE, subcommands=("list", "add", "remove", "enable",
                                          "disable", "reload", "roots"),
            effect="编辑 mcp.json 的服务器条目（项目级/用户级）并可选重启"),
    Command("search", "Full-text search over all past sessions", "<query>", category="history",
            requires_args=True),
    Command("memory", "Show persisted agent memory (MEMORY.md / USER.md)"),
    Command("plans", "List confirmed plans from .oaset/plans (idea-forge)",
            category="history",
            effect="只读列出方案文档；/view <path> 查看内容"),
    Command("tools", "List tools; /tools-toggle enables/disables"),
    Command("tools-toggle", "Enable/disable a tool for this session", risk=RISK_WRITE,
            effect="启停工具并写回 config.toml 的 disabled_tools"),
    Command("usage", "Token usage for this session"),
    Command("context", "What is using the context window (prompt/tools/messages)",
            category="model"),
    Command("evidence", "Evidence gate: done claims must carry receipts",
            "<on|off>", category="model"),
    Command("cron", "List scheduled automations"),
    Command("skills", "Skills: list, load, show, create, delete",
            "<name|show <name>|new <name>|delete <name>>", category="config",
            risk=RISK_WRITE, subcommands=("show", "new", "delete"),
            effect="载入技能到系统提示；new/delete 会写盘并需审批"),
    Command("todo", "Toggle the plan strip above the input"),
    Command("clear", "Clear the chat view (history is kept)", risk=RISK_WRITE,
            effect="只清空界面，历史仍保存在磁盘", aliases=("cls",)),
    Command("export", "Export current session to Markdown", "[path]", risk=RISK_WRITE,
            effect="在当前目录写出 Markdown 文件"),
    Command("session-delete", "Delete a session by ID suffix", "<id>", category="session",
            requires_args=True, risk=RISK_DANGER,
            effect="从磁盘永久删除该会话文件"),
    Command("title", "Set or show the session title", "<title>", category="session",
            requires_args=True, risk=RISK_WRITE, effect="写入会话元数据"),
    Command("config", "Show config path, providers and models"),
    Command("settings", "Settings: network policy, tools, hooks, permission rules",
            "<network|tools|hooks|rules|show>", category="config", risk=RISK_WRITE,
            subcommands=("network", "tools", "hooks", "rules", "show"),
            effect="写回 config.toml 的网络模式/工具开关/hooks（均可撤销）", aliases=("config-ui",)),
    Command("tasks", "Browse background tasks"),
    Command("goal", "Set a persistent goal; /goal budget <k>, /goal stop", risk=RISK_WRITE,
            subcommands=("status", "stop", "budget"),
            effect="改写系统提示中的目标块（本会话内生效）"),
    Command("editor", "Set the external editor for Ctrl+G", "<command>", category="config",
            requires_args=True, risk=RISK_WRITE, effect="写回 config.toml 的编辑器命令"),
    Command("insights", "Token usage by day", "[days]"),
    Command("image", "Attach an image to the next message", "<path>", category="input",
            requires_args=True, risk=RISK_WRITE,
            effect="把该图片随下一条消息发送给模型（需模型支持 image_in）"),
    Command("worktree", "Git worktree: new / list / remove",
            "[new <name>|list|remove <name>]", category="session", risk=RISK_DANGER,
            subcommands=("new", "list", "remove"),
            effect="在 .oaset/worktrees 下加/删 git worktree，不碰当前脏工作区"),
    Command("copy", "Copy a chat selection, tool output or the draft", category="input"),
    Command("paste", "Paste clipboard text into the current input", category="input"),
    Command("steer", "Inject an instruction into the running turn", "<text>", category="input",
            requires_args=True, risk=RISK_WRITE,
            effect="把文本注入正在运行的回合，会立即影响模型行为"),
    Command("retry", "Resend the previous prompt", risk=RISK_WRITE,
            effect="再次调用模型（会消耗 token）"),
    Command("history", "Show a compact history of this conversation"),
    Command("status", "Session and runtime status", aliases=('st',)),
    Command("init", "Analyze the codebase and generate AGENTS.md", risk=RISK_WRITE,
            effect="以模型回合分析代码库并写入 AGENTS.md"),
    Command("reload", "Reload config.toml from disk", risk=RISK_WRITE,
            effect="重建 provider/fallback/hooks/shell 配置"),
    Command("reload-mcp", "Restart MCP servers and re-register tools", risk=RISK_WRITE,
            effect="关闭并重启 MCP 服务器，替换已注册工具"),
    Command("roots", "MCP workspace roots: add / remove / clear",
            "[add <path>|remove <n>|clear]", category="system", risk=RISK_WRITE,
            subcommands=("add", "remove", "clear"),
            effect="roots/list 会把额外根目录路径披露给 MCP server（新增/移除立即刷新处理器）"),
    Command("reload-skills", "Rebuild the skill list and system prompt", risk=RISK_WRITE,
            effect="重建 skill 列表并改写系统提示"),
    Command("doctor", "Environment and configuration self-check"),
    Command("version", "Show version information"),
    Command("exit", "Exit oAset", aliases=("quit", "q"), risk=RISK_DANGER,
            effect="退出 oAset（结束运行中的回合与后台任务）"),
    Command("dequeue", "Drop every queued message (queued with Enter while running)"),
    Command("view", "Open the last message full-screen to select & copy any part",
            "[user|answer|tools]", category="interface"),
    Command("errors", "Show the pinned error's full detail in the viewer",
            category="system"),
    Command("density", "Chat spacing: /density compact|cozy (persisted)",
            "[compact|cozy]", category="interface", risk=RISK_WRITE,
            effect="写回 config.toml 的 [ui] density"),
    Command("login", "Store an API key for a provider locally", "[provider]",
            category="model", risk=RISK_WRITE,
            effect="密钥只写入本机 ~/.oaset/credentials/（0600），不会进 config"),
]

def command_description(cmd: "Command | str") -> str:
    """Localised description for /help and the palette (P1-5).

    Built-ins resolve through the `cmd_*` catalog keys when present; anything
    else (plugin commands) falls back to the stored English description."""
    from oaset.i18n import CATALOG, t

    name = cmd.name if isinstance(cmd, Command) else str(cmd)
    key = "cmd_" + name.replace(" ", "_").replace("-", "_")
    return t(key) if key in CATALOG else (cmd.description if isinstance(cmd, Command) else name)


# Argless danger commands that open their own picker (the choice IS the
# confirmation) — see the gating comment inside execute().
# `rewind` is deliberately NOT here: its argless path is a plain numeric prompt
# (`ARG_PROMPTS["rewind"]`), not a "which one?" picker, so the choice it offers
# is not a confirmation of what will be dropped — it still gets the confirm page.
_PICKER_DANGER = frozenset({"undo"})

# Taxonomy, applied in one visible place rather than scattered across 53
# literals: the palette and /help group by category, and two thirds of the
# commands used to fall into "general" - which made the grouping useless for
# finding anything. Each command has exactly one home, chosen by what a user
# is trying to do (not by how the code is organised).
COMMAND_CATEGORIES: dict[str, str] = {
    # interface: what is on screen right now
    "help": "interface", "palette": "interface", "clear": "interface",
    "todo": "interface", "theme": "interface", "dequeue": "interface",
    "view": "interface", "density": "interface", "language": "interface",
    # session: the conversation lifecycle
    "new": "session", "sessions": "session", "session-delete": "session",
    "fork": "session", "compact": "session", "goal": "session", "worktree": "session",
    # history: looking back and going back
    "history": "history", "undo": "history", "rewind": "history",
    "export": "history",
    # input: composing and sending
    "copy": "input", "paste": "input", "image": "input", "steer": "input",
    "retry": "input",
    # model: which model, and what it costs
    "model": "model", "models": "model", "usage": "model", "insights": "model",
    "login": "model",
    # tools: the agent's toolbox
    "tools": "tools", "tools-toggle": "tools",
    # mcp
    "mcp": "mcp", "roots": "mcp", "reload-mcp": "mcp",
    # agents & skills
    "agents": "agents", "skills": "agents", "reload-skills": "agents",
    "init": "agents", "memory": "agents",
    # config
    "config": "config", "settings": "config", "editor": "config",
    "mode": "config", "yolo": "config", "reload": "config",
    # system
    "status": "system", "doctor": "system", "update": "system", "errors": "system",
    "version": "system", "exit": "system", "cron": "system", "tasks": "system",
    # plugins
    "plugins": "plugin", "reload-plugins": "plugin", "market": "plugin",
    # computer use (real tools live in computer/; there is no /browser slash command)
    "desktop": "computer",
}


def _apply_categories() -> None:
    """Move every command into its declared home (see COMMAND_CATEGORIES)."""
    from dataclasses import replace

    for index, cmd in enumerate(COMMANDS):
        home = COMMAND_CATEGORIES.get(cmd.name)
        if home and cmd.category != home:
            COMMANDS[index] = replace(cmd, category=home)


_apply_categories()

# Daily coding surface. Everything else
# stays registered and searchable; /help and an empty Ctrl+K show this set.
PRIMARY_COMMANDS = frozenset({
    "help", "login", "model", "new", "sessions", "undo", "rewind", "compact",
    "skills", "mcp", "mode", "status", "doctor", "exit", "clear", "tools",
    "memory", "init", "retry", "todo", "language",
})


# Aliases live in the registry (Command.aliases) - one source of truth.
# The old global table shadowed registry aliases and hid /quit from /help,
# so it was removed when the alias set was merged into COMMANDS below.


def alias_map() -> dict[str, str]:
    """Alias -> canonical name, built ONLY from the registry metadata.

    One source of truth: an alias is discoverable in /help and /palette exactly
    when it is declared on the command (the removed global table made `/quit`
    work while staying invisible in every listing)."""
    return {alias.lower(): cmd.name
            for cmd in COMMANDS
            for alias in cmd.aliases}


def effect_display(cmd: "Command | str") -> str:
    """i18n-resolved effect line for the palette/confirm pages.

    Built-in commands resolve through the `effect_*` catalog keys; plugin
    commands (or anything without a key) fall back to the stored text."""
    from oaset.i18n import CATALOG, t

    if isinstance(cmd, Command):
        key = "effect_" + cmd.name.replace(" ", "_").replace("-", "_")
        if key in CATALOG:
            return t(key)
        return cmd.effect
    if "effect_plugin" in CATALOG:
        return t("effect_plugin")
    return str(cmd)


def command_metadata(extra: dict | None = None) -> list[Command]:
    result = list(COMMANDS)
    reserved = {c.name for c in COMMANDS} | set(alias_map())
    for name, cmd in (extra or {}).items():
        if name not in reserved:
            result.append(Command(name, cmd.description, category="plugin", source="plugin",
                                  risk=RISK_WRITE,
                                  effect=effect_display("plugin")))
    return result


CATEGORY_ORDER = ("interface", "session", "history", "input", "model", "tools",
                  "mcp", "agents", "config", "system", "computer", "plugin",
                  "general")

CATEGORY_KEYS = {
    "interface": "cat_interface",
    "session": "cat_session",
    "history": "cat_history",
    "input": "cat_input",
    "config": "cat_config",
    "system": "cat_system",
    "computer": "cat_computer",
    "plugin": "cat_plugin",
    "model": "cat_model",
    "tools": "cat_tools",
    "mcp": "cat_mcp",
    "agents": "cat_agents",
    "general": "cat_general",
}


def category_of(cmd: "Command") -> str:
    return cmd.category if cmd.category in CATEGORY_ORDER else "general"


def commands_by_category(extra: dict | None = None) -> "list[tuple[str, list[Command]]]":
    """Commands grouped for the help screen / palette, in a stable order."""
    groups: dict[str, list[Command]] = {}
    for cmd in command_metadata(extra):
        groups.setdefault(category_of(cmd), []).append(cmd)
    for items in groups.values():
        items.sort(key=lambda c: c.name)
    return [(name, groups[name]) for name in CATEGORY_ORDER if name in groups]


def category_label(key: str) -> str:
    from oaset.i18n import t

    return t(CATEGORY_KEYS.get(key, "cat_general"))


def command_table(extra: dict | None = None) -> list[tuple[str, str]]:
    return [(c.name, c.description) for c in command_metadata(extra)]


def find_command(name: str, extra: dict | None = None) -> Command | None:
    """Resolve a name or alias to its Command metadata."""
    resolved = alias_map().get(str(name).lower(), str(name).lower())
    for cmd in command_metadata(extra):
        if cmd.name == resolved:
            return cmd
    return None


def suggest(prefix: str, extra: dict | None = None) -> list[tuple[str, str]]:
    """Slash-completion: empty prefix = daily commands; typing searches all."""
    aliases = alias_map()
    needle = prefix.lstrip("/").lower()
    cmds = command_metadata(extra)
    if not needle:
        return [(c.name, command_description(c)) for c in cmds
                if c.name in PRIMARY_COMMANDS or c.source == "plugin"]
    resolved = aliases.get(needle, needle)
    out: list[tuple[str, str]] = []
    for cmd in cmds:
        hay = " ".join((cmd.name, cmd.description, " ".join(cmd.aliases))).lower()
        if cmd.name.startswith(resolved) or needle in hay:
            out.append((cmd.name, command_description(cmd)))
    return out


async def execute(app: Any, text: str, *, confirmed: bool = False) -> None:
    """Dispatch '/name args…' to app.cmd_<name>(args) or a plugin command.

    Commands flagged requires_args get an interactive inline prompt when
    invoked with no argument, instead of a dead-end usage error.

    ``confirmed`` is set by the palette, which has already shown the
    confirmation page for a risky command; the typed path leaves it False so a
    destructive command with an argument still asks once before acting.
    """
    parts = text[1:].split(" ", 1)
    raw_name = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    if getattr(app, "host", None) is None:
        # fast-boot window: the runtime warms in a worker right after paint
        app.chat.add_notice(t("boot_pending_cmd"), "warn")
        return
    name = alias_map().get(raw_name, raw_name)
    # hyphenated commands (/session-delete) map to snake_case handlers;
    # plugin commands keep their literal name
    python_name = name.replace("-", "_")
    plugin_commands = getattr(app, "plugin_commands", {})
    plugin_cmd = plugin_commands.get(name) or plugin_commands.get(python_name)
    if plugin_cmd is not None:
        result = plugin_cmd.handler(app, args)
        if hasattr(result, "__await__"):
            await result
        return
    handler = getattr(app, f"cmd_{python_name}", None)
    if handler is None:
        # A typo is the most common way to "not find a command": answer in the
        # user's language and name the closest real command.

        known = [c.name for c in COMMANDS] + list(alias_map())
        suggestion = _closest_command(raw_name, known)
        if suggestion:
            suggestion = alias_map().get(suggestion, suggestion)
            app.chat.add_notice(
                t("unknown_command_suggest", name=raw_name, suggestion=suggestion), "warn")
        else:
            app.chat.add_notice(t("unknown_command", name=raw_name), "warn")
        return
    if not args and _requires_args(name):
        args = await app.prompt_missing_arg(name) or ""
        if not args:
            return  # user cancelled the inline prompt
    # A destructive command that acts *immediately* (given its argument) must
    # confirm first: "/session-delete abc" or "/worktree remove feat-x" typed
    # from memory is otherwise a faster route to data loss than the palette,
    # which does confirm. Only the commands in _PICKER_DANGER are exempt, and
    # only when they genuinely open a picker of their own — that choice is
    # already an explicit confirmation, and stacking a second page on top would
    # be pure friction.
    #
    # The gate stays on RISK_DANGER rather than on needs_confirmation(): ~20
    # commands carry RISK_WRITE, and confirming /new, /model, /theme or /clear
    # on the typed path would train the user to answer "y" without reading —
    # worse than the asymmetry it would fix. The asymmetry that actually
    # mattered was `/worktree remove` (a force-remove that discards uncommitted
    # work) sitting at RISK_WRITE; it is DANGER now, so both paths confirm it.
    spec = find_command(name, getattr(app, "plugin_commands", None))
    if spec is not None and spec.risk == RISK_DANGER and not confirmed:
        # Argless /undo opens its own checkpoint picker — that choice IS the
        # confirmation. Every other danger command confirms even without
        # arguments: /exit and /q used to quit immediately, with no confirm at
        # all (P1-3), and /rewind's numeric prompt does not name what it drops.
        picker_first = not args and name in _PICKER_DANGER
        if not picker_first:
            confirmed = await app._confirm_command(
                "/" + name + (f" {args}" if args else ""), spec)
            if not confirmed:
                app.chat.add_notice(t("command_cancelled", name=name), "info")
                return
    from oaset.tui import usage

    usage.record(name)  # canonical name, so the palette's recent group is useful

    result = handler(args)
    if hasattr(result, "__await__"):
        await result


def _closest_command(name: str, known: list[str]) -> str | None:
    """Best guess for a mistyped command name.

    A transposition ("/modle" for "/model") is the classic typo, and plain
    ratio scoring prefers a shorter neighbour instead ("/mode"), so anagrams
    are checked first; difflib then covers missing/extra/replaced characters.
    """
    lowered = name.lower()
    anagrams = sorted(c for c in known if c != lowered and sorted(c) == sorted(lowered))
    if anagrams:
        return anagrams[0]
    import difflib

    close = difflib.get_close_matches(lowered, known, n=1, cutoff=0.6)
    return close[0] if close else None


def _requires_args(name: str) -> bool:
    return any(c.name == name and c.requires_args for c in COMMANDS)


@dataclass(frozen=True)
class ArgSpec:
    """Structured parameter form for a command argument.

    kind:
      "text"   — free-text InlineInput (placeholder shown)
      "choice" — enum: pick from `choices` (value, label); every choice form
                 also offers a manual-entry row, so no value is unreachable
      "path"   — file picker over the workspace (`filter_suffixes` narrows it)
      "model"  — pick from the configured models
    """

    title: str
    placeholder: str = ""
    kind: str = "text"
    choices: tuple[tuple[str, str], ...] = ()
    filter_suffixes: tuple[str, ...] = ()
    allow_custom: bool = True

    @classmethod
    def coerce(cls, raw: Any) -> ArgSpec | None:
        """Back-compat: a plain (title, placeholder) tuple is a text spec."""
        if raw is None:
            return None
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, tuple) and len(raw) == 2 and all(isinstance(x, str) for x in raw):
            return cls(title=raw[0], placeholder=raw[1])
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialisable schema (used by Command.schema() and diagnostics)."""
        return {
            "title": self.title,
            "placeholder": self.placeholder,
            "kind": self.kind,
            "choices": [{"value": v, "label": lbl} for v, lbl in self.choices],
            "filter_suffixes": list(self.filter_suffixes),
            "allow_custom": self.allow_custom,
        }


def _theme_choices() -> tuple[tuple[str, str], ...]:
    from oaset.tui.themes import THEMES

    return (("auto", "auto (follow config)"), *[(name, name) for name in THEMES])


ARG_PROMPTS: dict[str, Any] = {
    # command -> ArgSpec (or legacy (title, placeholder) tuple)
    "market": ArgSpec("Open market tab", "empty = menu",
                      kind="choice",
                      choices=(("discover", "arg_market_discover"),
                               ("installable", "arg_market_installable"),
                               ("installed", "arg_market_installed"),
                               ("sources", "arg_market_sources"))),
    "session-delete": ArgSpec("Session id suffix to delete", "e.g. 3f2a1b9c"),
    "title": ArgSpec("New session title", "summary of this session"),
    "search": ArgSpec("Search history for", "keyword"),
    "editor": ArgSpec(
        "External editor command",
        kind="choice",
        choices=(
            ("code --wait", "VS Code — code --wait"),
            ("vim", "Vim — vim"),
            ("nvim", "Neovim — nvim"),
            ("nano", "Nano — nano"),
            ("notepad", "Notepad — notepad (Windows)"),
        ),
    ),
    "image": ArgSpec("Attach an image", kind="path",
                     filter_suffixes=(".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")),
    "steer": ArgSpec("Instruction to inject", "focus on the tests"),
    "worktree": ArgSpec("Worktree action", kind="choice", choices=(
        ("list", "arg_worktree_list"),
        ("new", "arg_worktree_new"),
        ("remove", "arg_worktree_remove"),
    )),
    # ---- commands whose handlers own subcommands or an enum argument --------
    "language": ArgSpec("Language / 语言", kind="choice", choices=(
        ("zh", "中文"),
        ("en", "English"),
    )),
    "mode": ArgSpec("Permission mode", kind="choice", choices=(
        ("default", "default — confirm writes & shell"),
        ("plan", "plan — read-only"),
        ("auto", "auto — allow everything (--yolo)"),
    )),
    "think": ArgSpec("Thinking level", kind="choice", choices=(
        ("off", "off — disable where the API allows"),
        ("light", "light — quick reasoning"),
        ("medium", "medium — balanced"),
        ("heavy", "heavy — deepest available"),
    )),
    "update": ArgSpec("Update center action", kind="choice", choices=(
        ("check", "check — read cache, refresh only when the TTL expired"),
        ("list", "list — list catalog entries; add a kind (plugin/mcp/…) or `detail`"),
        ("plan", "plan — build the compatible install plan"),
        ("refresh", "refresh — fetch the catalog now (network)"),
        ("apply", "apply — install a target (separate confirmation + approval)"),
        ("rollback", "rollback — restore a recorded apply (comfirm + approval)"),
        ("history", "history — show recorded update transactions"),
    )),
    "selfmaint": ArgSpec("arg_selfmaint_title", kind="choice", choices=(
        ("run", "arg_selfmaint_run"),
        ("enable", "arg_selfmaint_enable"),
    )),
    "goal": ArgSpec("Goal action", kind="choice", choices=(
        ("status", "arg_goal_status"),
        ("stop", "arg_goal_stop"),
        ("budget", "arg_goal_budget"),
    )),
    "rewind": ArgSpec("How many turns to drop", "1"),
    # Without a spec the palette form is skipped entirely and cmd_density runs
    # with an empty argument, which used to silently mean "compact". Declaring
    # the choices makes the palette ask, and keeps an empty submission from
    # changing a persisted setting by accident.
    "density": ArgSpec("Chat spacing", kind="choice", choices=(
        ("cozy", "arg_density_cozy"),
        ("compact", "arg_density_compact"),
    )),
    "insights": ArgSpec("Days of usage to summarise", "7"),
    "export": ArgSpec("Output Markdown path", "oaset-session.md"),
    "settings": ArgSpec("Settings area", kind="choice", choices=(
        ("network", "arg_settings_network"),
        ("tools", "arg_settings_tools"),
        ("hooks", "arg_settings_hooks"),
        ("rules", "arg_settings_rules"),
        ("show", "arg_settings_show"),
    )),
    "agents": ArgSpec("Sub-agent action", kind="choice", choices=(
        ("show", "arg_agents_show"),
        ("new", "arg_agents_new"),
        ("delete", "arg_agents_delete"),
        ("reload", "arg_agents_reload"),
    )),
    "skills": ArgSpec("Skill action", kind="choice", choices=(
        ("show", "arg_skills_show"),
        ("new", "arg_skills_new"),
        ("delete", "arg_skills_delete"),
    )),
    "mcp": ArgSpec("MCP action", kind="choice", choices=(
        ("list", "arg_mcp_list"),
        ("add", "arg_mcp_add"),
        ("enable", "arg_mcp_enable"),
        ("disable", "arg_mcp_disable"),
        ("remove", "arg_mcp_remove"),
        ("reload", "arg_mcp_reload"),
        ("roots", "arg_mcp_roots"),
    )),
    "model": ArgSpec("Model action", kind="choice", choices=(
        ("add", "arg_model_add"),
        ("list", "arg_model_list"),
        ("verify", "arg_model_verify"),
    )),
    "models": ArgSpec("Model action", kind="choice", choices=(
        ("add", "arg_model_add"),
        ("list", "arg_model_list"),
        ("verify", "arg_model_verify"),
    )),
    "roots": ArgSpec("Directory path to expose as an extra MCP root"),
}


def arg_spec_for(command_name: str) -> ArgSpec | None:
    spec = ArgSpec.coerce(ARG_PROMPTS.get(command_name))
    if spec is None and command_name == "theme":
        return ArgSpec("Theme", kind="choice", choices=_theme_choices())
    return spec


def localized_spec(spec: ArgSpec) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Resolve a spec's title and choice labels through i18n at DISPLAY time.

    ARG_PROMPTS stores catalogue keys (the language can switch mid-session,
    so labels must not bake at import); ``t()`` passes unknown strings
    through unchanged, so specs holding plain text keep working as-is.
    """
    from oaset.i18n import t

    title = t(spec.title)
    choices = tuple((value, t(label)) for value, label in spec.choices)
    return title, choices
