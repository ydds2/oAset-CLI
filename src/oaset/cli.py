"""oAset CLI entry point.

  oaset                 interactive TUI
  oaset -p "prompt"     one-shot mode (prints the final answer)
  oaset -c              resume the most recent session in this directory
  oaset --resume ID     resume a specific session
  oaset models|sessions doctor version
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from oaset import __version__
from oaset.config import (
    AppConfig,
    available_model_ids,
    config_path,
    load_config,
    resolve_api_key,
    resolve_model,
    save_config,
)
from oaset.i18n import t
from oaset.utils import now_iso, oaset_home


def arun(coro) -> int:
    """asyncio.run for command handlers; asyncio itself imports only here so
    `oaset --version` / `--help` never pay its ~180ms cold-start cost."""
    import asyncio

    return asyncio.run(coro)


def read_stdin_if_piped() -> str:
    """stdin content when it is a real pipe/redirect, else "" (never blocks).

    Enables `tool < in.txt` and `cat log | oaset -p "summarise"`. Before this,
    only the interactive TUI touched stdin, so a piped run silently ignored its
    input - the classic "works interactively, useless in a pipeline" gap.

    Deliberately strict about what counts as input: a programmatic stdin stub
    (pytest's capture object, a GUI launcher, a TUI harness) has no real file
    descriptor, and reading it can block forever. Only a regular file or a FIFO
    is read.
    """
    stream = sys.stdin
    if stream is None:
        return ""
    try:
        if stream.isatty():
            return ""
    except Exception:
        return ""
    try:
        fd = stream.fileno()
    except Exception:
        return ""  # no real descriptor: not a pipe, do not touch it
    try:
        mode = os.fstat(fd).st_mode
        if stat.S_ISREG(mode) and os.fstat(fd).st_size == 0:
            return ""  # an empty redirected file carries no prompt
        if not (stat.S_ISFIFO(mode) or stat.S_ISREG(mode)):
            return ""  # sockets/devices are not prompt sources
    except Exception:
        return ""
    try:
        return stream.read()
    except Exception:
        return ""


class _OasetParser(argparse.ArgumentParser):
    """argparse that suggests the closest real command on a typo.

    The TUI has had did-you-mean for slash commands since P1; the CLI entry
    deserved the same intelligence instead of a bare usage dump.
    """

    _oaset_choices: list = []

    def error(self, message: str) -> None:
        import difflib
        import re as _re

        m = _re.search(r"invalid choice: '([^']+)'", message)
        if m:
            bad = m.group(1)
            near = difflib.get_close_matches(bad, self._oaset_choices, n=1)
            if near:
                print(f"error: unknown command '{bad}' — did you mean "
                      f"'oaset {near[0]}'?  (see: oaset --help)", file=sys.stderr)
                raise SystemExit(2)
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _OasetParser(
        prog="oaset",
        description="oAset CLI — a local-first terminal coding agent.",
    )
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("-p", "--print", dest="prompt", metavar="TEXT", help="one-shot mode")
    parser.add_argument("-c", "--continue", dest="continue_", action="store_true", help="resume latest session in this directory")
    parser.add_argument("--resume", metavar="SESSION_ID", help="resume a specific session")
    parser.add_argument("--model", metavar="MODEL_ID", help="model from config, e.g. deepseek/deepseek-chat")
    parser.add_argument("--think", metavar="LEVEL", help="thinking depth for this run: off|light|medium|heavy (aliases low/high ok)")
    parser.add_argument("--cwd", metavar="DIR", help="workspace directory (default: current)")
    parser.add_argument("--yolo", action="store_true", help="auto-approve all tool permissions")
    parser.add_argument("--plain", action="store_true",
                        help="line-oriented REPL: no ANSI/animation (screen-reader & dumb-terminal friendly)")
    parser.add_argument("--mock", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true", help="write DEBUG logs to ~/.oaset/logs/oaset.log")
    parser.add_argument("--output-format", choices=["text", "json", "stream-json"], default="text",
                        help="-p output format: plain text, one JSON document, or one JSON object per event")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="only the result on stdout; no progress on stderr")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="extra diagnostics on stderr (provider, model, timing)")
    sub = parser.add_subparsers(dest="command")
    login = sub.add_parser("login", help="store an API key for a provider")
    login.add_argument("provider")
    logout = sub.add_parser("logout", help="remove a stored provider credential")
    logout.add_argument("provider")
    sub.add_parser("upgrade", help="legacy pip self-upgrade (prefer `oaset update apply <core-release>`)")
    trustp = sub.add_parser("trust-plugin", help="trust a project-level plugin by pinning its sha256")
    trustp.add_argument("name", help="plugin file stem, e.g. demo for .oaset/plugins/demo.py")
    mktp = sub.add_parser("market", help="community plugin market: list sources / add / use / remove / install")
    mktp.add_argument("action", choices=["list", "add", "use", "remove", "install", "installed"],
                      nargs="?", default="list")
    mktp.add_argument("name", nargs="?", help="source name (add/use/remove) or plugin name (install)")
    mktp.add_argument("url", nargs="?", help="catalog https URL for `market add`")
    mktp.add_argument("--refresh", action="store_true", help="bypass the 24h catalog cache")
    mcpmgr = sub.add_parser("mcp", help="list / trust / revoke MCP servers (project servers need trust)")
    mcpmgr.add_argument("action", choices=["list", "add", "trust", "revoke"],
                        nargs="?", default="list")
    mcpmgr.add_argument("name", nargs="?", help="server name (add/trust/revoke)")
    mcpmgr.add_argument("--url", help="mcp add: add an http server by URL, skipping the registry")
    selfmp = sub.add_parser("selfmaint", help="体验计划: run one budget-capped self-maintenance cycle")
    selfmp.add_argument("--budget", type=int, help="override token budget for this run")
    selfmp.add_argument("--allow-apply", action="store_true",
                        help="allow installing the plan's patch targets (hash-gated)")
    selfmp.add_argument("--json", action="store_true", help="machine-readable report")
    updp = sub.add_parser("update", help="update center: check/refresh/list/plan/apply/rollback/history")
    updp.add_argument("action", choices=["check", "refresh", "list", "plan", "apply", "rollback", "history", "doctor"],
                      nargs="?", default="check")
    updp.add_argument("targets", nargs="*", help="plugin names for apply / rollback")
    updp.add_argument("--kind", choices=["core", "plugin", "mcp", "provider", "model", "skill", "all"],
                      default="all")
    updp.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="apply: download and verify only, write nothing")
    updp.add_argument("--json", action="store_true", help="machine-readable output")
    mcpp = sub.add_parser("mcp-serve", help="expose oAset tools as a stdio MCP server")
    mcpp.add_argument("--cwd", dest="mcp_cwd")
    mcpp.add_argument("--all-tools", action="store_true",
                      help="expose write/exec tools too (default: read-only)")
    mcpp.add_argument("--token", help="require clients to send this token in initialize params")
    acpp = sub.add_parser("acp", help="speak Agent Client Protocol on stdio (editors)")
    acpp.add_argument("--cwd", dest="acp_cwd")
    acpp.add_argument("--yolo", action="store_true", help="auto-approve tools for the editor session")
    acpp.add_argument("--lsp", action="store_true", help="use LSP Content-Length framing instead of newline JSON")
    batchp = sub.add_parser("batch", help="run a jsonl of prompts headlessly")
    batchp.add_argument("--tasks", required=True)
    batchp.add_argument("--out", default="runs")
    batchp.add_argument("--model")
    batchp.add_argument("--cwd")
    batchp.add_argument("--mock", action="store_true")
    batchp.add_argument("--yolo", action="store_true")
    matp = sub.add_parser("matrix", help="run tasks in parallel, each in its own git worktree")
    matp.add_argument("--tasks", required=True, help="jsonl file: {\"name\":…, \"prompt\":…} per line")
    matp.add_argument("--max-parallel", type=int, default=3)
    matp.add_argument("--model")
    matp.add_argument("--mock", action="store_true")
    cfgp = sub.add_parser("config", help="show or set configuration")
    cfgp.add_argument("action", choices=["list", "set"], nargs="?", default="list")
    cfgp.add_argument("key", nargs="?")
    cfgp.add_argument("value", nargs="?")
    sub.add_parser("setup", help="interactive provider/model/key wizard")
    gwp = sub.add_parser("gateway", help="serve the agent over HTTP (gateway architecture)")
    gwp.add_argument("--panel", action="store_true", help=t("gateway_panel_help"))
    gwp.add_argument("--panel-timeout", type=float, default=120.0,
                     help=t("panel_timeout_help"))
    gwp.add_argument("--host", default="127.0.0.1")
    gwp.add_argument("--port", type=int, default=8790)
    gwp.add_argument("--model")
    gwp.add_argument("--cwd")
    gwp.add_argument("--mock", action="store_true")
    gwp.add_argument("--yolo", action="store_true")
    gwp.add_argument("--telegram", action="store_true",
                     help="run the Telegram long-polling transport (token: env TELEGRAM_BOT_TOKEN or `oaset login telegram`)")
    gwp.add_argument("--discord", action="store_true",
                     help="run the Discord gateway transport (token: env DISCORD_BOT_TOKEN or `oaset login discord`)")
    gwp.add_argument("--slack", action="store_true",
                     help="run the Slack Events transport on /slack/events (token: env SLACK_BOT_TOKEN or `oaset login slack`)")
    gwp.add_argument("--send-replies", action="store_true",
                     help="allow transports to SEND replies (default receive-only, 只收不发)")
    cronp = sub.add_parser("cron", help="scheduled automations")
    cronp.add_argument("action", choices=["list", "add", "remove", "run", "daemon"])
    cronp.add_argument("--name")
    cronp.add_argument("--schedule")
    cronp.add_argument("--prompt")
    cronp.add_argument("--id")
    cronp.add_argument("--model")
    models = sub.add_parser("models", help="list / add / verify models")
    models.add_argument("action", nargs="?", default="list",
                        choices=["list", "add", "verify"],
                        help="list models; add runs the onboarding wizard; verify probes a model")
    models.add_argument("verify_model", nargs="?", metavar="MODEL_ID",
                        help="verify connectivity of one model (real API call)")
    models.add_argument("--json", action="store_true", help="machine-readable verify output")
    models.add_argument("--verify", action="store_true",
                        help=t("models_verify_flag_help"))
    models.add_argument("--all", action="store_true",
                        help=t("models_verify_all_flag_help"))
    sessionsp = sub.add_parser("sessions", help="list recorded sessions")
    sessionsp.add_argument("--json", action="store_true", help="machine-readable session list")
    sub.add_parser("doctor", help="diagnose environment and configuration")
    sub.add_parser("offline-check", help=t("offline_flag_help"))
    evidencep = sub.add_parser("evidence", help=t("evidence_flag_help"))
    evidencep.add_argument("session", metavar="SESSION_ID",
                           help="session id (a unique short id works)")
    evidencep.add_argument("--export", metavar="OUT.md",
                           help="write the audit chain as Markdown")
    # AFTER all subparsers exist: the old snapshot ran before any
    # add_parser and was forever empty — did-you-mean was dead code
    parser._oaset_choices = sorted(sub._name_parser_map)
    return parser


def _hide_conhost_scrollbar() -> None:
    """Windows conhost 会在缓冲区高于窗口时自绘滚动条（毛边来源）。
    把缓冲区收缩到窗口大小即可根除；仅 TUI 模式、尽力而为。"""
    if os.name != "nt" or not sys.stdout.isatty():
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]  # ctypes.windll exists only on Windows
        handle = kernel32.GetStdHandle(-11)

        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class SMALL_RECT(ctypes.Structure):
            _fields_ = [("L", ctypes.c_short), ("T", ctypes.c_short),
                        ("R", ctypes.c_short), ("B", ctypes.c_short)]

        class SBI(ctypes.Structure):
            _fields_ = [("dwSize", COORD), ("cursor", COORD),
                        ("attr", ctypes.c_ushort), ("win", SMALL_RECT),
                        ("maxwin", COORD)]

        info = SBI()
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return
        width = info.win.R - info.win.L + 1
        height = info.win.B - info.win.T + 1
        if width > 0 and height > 0:
            kernel32.SetConsoleScreenBufferSize(handle, COORD(width, height))
    except Exception:
        pass


def _setup_debug_logging() -> None:
    """--debug: rotate a DEBUG log under ~/.oaset/logs for post-mortem analysis."""
    import logging
    from logging.handlers import RotatingFileHandler

    log_dir = oaset_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "oaset.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.DEBUG, handlers=[handler])


def _force_utf8_stdio() -> None:
    """Frozen Windows exes default to the locale codec (GBK); our glyphs need UTF-8.

    stdin too: a GBK console piping Chinese text used to raise
    UnicodeDecodeError before the app ever saw the bytes (P4-4)."""
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        if stream is not None and hasattr(stream, "reconfigure"):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    _hide_conhost_scrollbar()
    raw = list(sys.argv[1:]) if argv is None else list(argv)
    if raw and raw[0] == "--sandbox-exec":
        # Frozen-exe re-entry for the Windows sandbox shim (source builds use
        # `python -m oaset.sandboxexec` and never hit this branch). `rest`
        # must not be named `args`: mypy fixes the variable's type at its
        # first assignment, and shadowing poisoned every later args use.
        from oaset.sandboxexec import run as _sandbox_run

        rest = raw[1:]
        if rest and rest[0] == "--":
            rest = rest[1:]
        return _sandbox_run(rest)
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "debug", False):
        _setup_debug_logging()
    if args.version:
        import sys as _sys

        from oaset.runtime_info import describe_source

        # The first thing anyone runs to check "which build is this?" - so it
        # answers that for a frozen exe (build date) as well as a checkout (sha).
        # Interactive use also gets the brand wordmark; piped/CI use stays
        # plain so version scraping never has to parse art.
        if _sys.stdout.isatty():
            try:
                from rich.console import Console as _Console

                from oaset.tui import brand as _brand

                # Box-drawing letters garble on a pre-19041 raster console —
                # same gate as the welcome page: no truecolor, no art.
                console = _Console()
                if _brand.truecolor_supported():
                    console.print(_brand.wordmark())
                console.print(f"[b]oAset CLI v{__version__}[/b]")
                print(f"  {describe_source()}")
                return 0
            except Exception:
                pass
        print(f"oAset CLI v{__version__}")
        print(f"  {describe_source()}")
        return 0
    if args.command == "login":
        return cmd_login(args.provider)
    if args.command == "logout":
        return cmd_logout(args.provider)
    if args.command == "upgrade":
        return cmd_upgrade()
    if args.command == "trust-plugin":
        return cmd_trust_plugin(args.name)
    if args.command == "mcp":
        return cmd_mcp(args)
    if args.command == "market":
        return arun(cmd_market(args))
    if args.command == "update":
        return arun(cmd_update(args))
    if args.command == "selfmaint":
        return arun(cmd_selfmaint(args))
    if args.command == "mcp-serve":
        return cmd_mcp_serve(args)
    if args.command == "acp":
        return cmd_acp(args)
    if args.command == "batch":
        return arun(cmd_batch(args))
    if args.command == "matrix":
        return arun(cmd_matrix(args))
    if args.command == "config":
        return cmd_config(args.action, args.key, args.value)
    if args.command == "setup":
        return cmd_setup()
    if args.command == "gateway":
        return cmd_gateway(args)
    if args.command == "cron":
        return cmd_cron(args)
    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "offline-check":
        return cmd_offline_check()
    if args.command == "evidence":
        return cmd_evidence(args)
    if args.command == "models":
        return cmd_models(args)
    if args.command == "sessions":
        return cmd_sessions(args)

    from oaset.i18n import t

    try:
        cfg = load_config()
    except Exception as exc:
        print(t("config_parse_failed", path=config_path(), error=exc), file=sys.stderr)
        return 2
    if args.yolo:
        cfg.permission_mode = "auto"
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()

    from oaset.session import SessionStore

    store = SessionStore()
    session = None
    if args.resume:
        session = store.load(args.resume)
        if session is None:
            from oaset.i18n import t

            print(f"error: session '{args.resume}' not found — {t('session_load_hint')}",
                  file=sys.stderr)
            return 2
    elif args.continue_:
        session = store.latest_for_cwd(cwd)
        if session is None:
            print("no previous session in this directory — starting fresh", file=sys.stderr)

    provider: Any = None
    if args.mock:
        # --mock must be a hard offline guarantee: no configured key, no
        # network call, regardless of what config.toml points at.
        from oaset.providers import MockProvider, MockTurn

        provider = MockProvider([MockTurn(content_chunks=["[mock] "])])
        cfg.default_provider = "mock"
        cfg.default_model = "mock/mock-echo"

    piped = read_stdin_if_piped()
    if args.prompt is not None or piped:
        prompt = args.prompt or ""
        if piped:
            prompt = (prompt + "\n\n" + piped).strip() if prompt else piped
        if args.verbose:
            print(f"[verbose] provider={cfg.default_provider} model={cfg.default_model} "
                  f"cwd={cwd} stdin={'yes' if piped else 'no'}", file=sys.stderr)
        from oaset.thinking import normalize_level

        think_level = normalize_level(getattr(args, "think", "") or "")
        if getattr(args, "think", "") and not think_level:
            print(f"error: --think {args.think!r}: use off|light|medium|heavy",
                  file=sys.stderr)
            return 2
        return arun(run_one_shot(cfg, cwd, prompt, args.model, provider, bool(args.yolo),
                                        output_format=args.output_format,
                                        quiet=bool(args.quiet), verbose=bool(args.verbose),
                                        resume=session, thinking=think_level))

    # --think is validated once for every surface (an invalid value must
    # exit 2 before the TUI mounts, not vanish silently inside it)
    from oaset.thinking import normalize_level

    think_level = normalize_level(getattr(args, "think", "") or "")
    if getattr(args, "think", "") and not think_level:
        print(f"error: --think {args.think!r}: use off|light|medium|heavy",
              file=sys.stderr)
        return 2

    # A mistyped --model must not surface as a raw KeyError traceback on ANY
    # interactive surface: validate once, before plain or TUI mounts.
    if args.model:
        from oaset.config import resolve_model

        try:
            resolve_model(cfg, args.model)
        except KeyError:
            from oaset.i18n import t

            print(t("model_unknown", model=args.model), file=sys.stderr)
            return 2

    if getattr(args, "plain", False):
        # Accessibility surface: same SessionHost runtime, append-only output
        from oaset.repl import PlainRepl

        return arun(PlainRepl(cfg, cwd, model_id=args.model,
                                     yolo=bool(args.yolo), provider=provider,
                                     thinking_level=think_level).run())

    from oaset.tui.app import OasetApp

    app = OasetApp(
        cfg=cfg,
        cwd=cwd,
        provider=provider,
        session=session,
        store=store,
        model_id=args.model,
        yolo=bool(args.yolo),
        thinking_level=think_level,
    )
    app.run()
    return 0


# ------------------------------------------------------------------ one-shot


async def run_one_shot(
    cfg: AppConfig,
    cwd: Path,
    prompt: str,
    model_id: str | None,
    provider: Any | None,
    yolo: bool,
    output_format: str = "text",
    quiet: bool = False,
    verbose: bool = False,
    resume=None,
    thinking: str = "",
) -> int:
    from oaset.agent import Conversation, initial_system_prompt
    from oaset.agent.loop import arguments_summary
    from oaset.providers.base import ProviderError
    from oaset.runtime import build_runtime

    auto = yolo or cfg.permission_mode == "auto"
    from oaset.config import model_availability
    from oaset.config import resolve_model as _resolve_model

    try:
        probe_model = _resolve_model(cfg, model_id)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    state, provider_name = model_availability(cfg, probe_model.id)
    injected_mock = str(getattr(provider, "model_id", "") or "").startswith("mock/")
    if state == "needs_key" and not injected_mock:
        print(t("send_needs_key", model=probe_model.id), file=sys.stderr)
        print(t("send_needs_key_hint_cli", provider=provider_name), file=sys.stderr)
        return 2
    try:
        rt = build_runtime(cfg, cwd, model_id=model_id, provider=provider,
                           mode="auto" if auto else "default")
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    model_cfg = rt.model_cfg
    provider = rt.provider
    registry = rt.registry

    system_prompt, _ = initial_system_prompt(cwd)
    # `-c`/`--resume` semantics must hold headlessly too: seed the kernel with
    # the recorded conversation and persist this exchange back to it.
    if resume is not None:
        conversation = resume.conversation
        if not conversation.system_prompt:
            conversation.system_prompt = system_prompt
    else:
        conversation = Conversation(system_prompt)
    from oaset.agent.subagent_runner import make_subagent_runner

    if not thinking:
        # the model's config default applies headlessly too — TUI/SDK seed
        # it via SessionHost; without this the same config behaved
        # differently on -p
        from oaset.thinking import normalize_level as _nl

        thinking = _nl(getattr(model_cfg, "thinking", "") or "")
    if thinking:
        # --think (or that default) seeds the state the agent loop reads
        # before every model call
        registry.ctx.session_state["thinking_level"] = thinking
    registry.ctx.session_state["subagent_runner"] = make_subagent_runner(
        provider,
        registry,
        registry.ctx,
        max_iterations=cfg.max_iterations,
        context_max_tokens=model_cfg.max_context_size,
        config=cfg,
    )
    # Phase B: headless runs execute through the kernel like TUI and SDK
    from oaset.hooks import HookRunner
    from oaset.kernel import AgentKernel

    kernel = AgentKernel(
        provider=provider,
        fallbacks=rt.fallbacks,
        registry=registry,
        conversation=conversation,
        max_iterations=cfg.max_iterations,
        max_tool_calls=cfg.max_tool_calls,
        context_max_tokens=model_cfg.max_context_size,
        hooks=HookRunner(cfg.hooks, cwd, network_mode=cfg.network_mode),
    )
    from oaset.mcp import McpManager

    mcp = McpManager(oaset_home(), cwd, network_mode=cfg.network_mode)
    try:
        await mcp.start_all()
        for adapter in mcp.adapters():
            registry.add_tool(adapter)
    except Exception:
        pass

    printed_error = False

    def emit(event: dict[str, Any], *, stream_content: bool = True) -> None:
        nonlocal printed_error
        kind = event["type"]
        if kind == "assistant_delta":
            if stream_content:
                print(event["text"], end="", flush=True)
        elif kind == "tool_call_started":
            # Progress belongs on stderr: stdout carries the answer alone, so a
            # pipeline (`oaset -p ... > out.txt`) is not polluted by tool chatter.
            if not quiet:
                from oaset.utils import display_tool_name

                label = t("tool_used", name=display_tool_name(event.get("name", "?")))
                summary = arguments_summary(event.get("arguments", ""), 80)
                extra = f" ({summary})" if summary else ""
                print(f"\n{label}{extra}", file=sys.stderr, flush=True)
        elif kind == "tool_completed":
            if not quiet:
                from oaset.utils import display_tool_name, one_line

                mark = "✗" if event.get("is_error") else "✓"
                label = t("tool_used", name=display_tool_name(event.get("name", "?")))
                preview = one_line(event.get("result") or "", 80)
                extra = f" · {preview}" if preview else ""
                print(f"{mark} {label}{extra}", file=sys.stderr, flush=True)
        elif kind == "turn_completed":
            if not quiet:
                print(flush=True)
        elif kind == "tool_denied":
            if not quiet:
                print(f"✗ {t('tool_denied_msg')}", file=sys.stderr, flush=True)
        elif kind == "notice":
            print(f"\n{event['text']}", file=sys.stderr, flush=True)
        elif kind == "iterations_exhausted":
            print(f"\n{t('iterations_exhausted_msg', max=event.get('max', '?'))}",
                  file=sys.stderr, flush=True)
        elif kind == "error":
            error = event["error"]
            printed_error = True
            print(f"\n[error] {getattr(error, 'message', error)}", file=sys.stderr, flush=True)
            if event.get("hint"):
                print(f"[hint] {event['hint']}", file=sys.stderr, flush=True)

    import itertools
    import json as _json

    _envelope_seq = itertools.count(1)

    def stream_json_line(event: dict[str, Any]) -> None:
        """stream-json contract: every line is one complete JSON object on
        stdout, nothing else ever touches stdout. Lines carry the canonical
        envelope (schema_version/sequence/timestamp) so a desktop consumer
        can order, dedupe and version-gate without parsing the payload."""
        payload = {
            "schema_version": 1,
            "sequence": next(_envelope_seq),
            "timestamp": now_iso(),
            "type": event["type"],
            "data": {k: v for k, v in event.items() if k != "type"},
        }
        print(_json.dumps(_jsonable(payload), ensure_ascii=False), flush=True)

    def _jsonable(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: _jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_jsonable(v) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)  # provider objects → readable placeholder

    from oaset.utils import extract_image_attachments

    images = extract_image_attachments(prompt, cwd, enabled=model_cfg.has("image_in"))
    resume_store = None
    if resume is not None and resume.file is not None:
        from oaset.agent.messages import Message
        from oaset.session import SessionStore
        from oaset.utils import run_io as _run_io

        resume_store = SessionStore()
        await _run_io(resume_store.append_message, resume,
                      Message(role="user", content=prompt))
    collected: list[dict[str, Any]] = []
    _deltas: list[str] = []

    def collecting_emit(event: dict[str, Any]) -> None:
        if event["type"] == "assistant_delta":
            # kept unconditionally: the resume persister needs the answer in
            # every output mode, not just the json ones
            _deltas.append(str(event.get("text") or ""))
        if event["type"] in ("turn_completed", "turn_done"):
            collected.append(dict(event))  # usage lands in every mode
        if output_format == "json":
            # stdout must carry EXACTLY ONE JSON document: stream content is
            # never printed; human progress stays on stderr
            collected.append(dict(event))
            emit(event, stream_content=False)
        elif output_format == "stream-json":
            collected.append(dict(event))
            stream_json_line(event)
        else:
            emit(event)

    try:
        async for event in kernel.chat(prompt, images=images or None):
            collecting_emit(event.payload())
    except ProviderError as exc:
        if not printed_error:  # emit() already surfaced retry-exhausted errors
            print(f"\n[error] {exc.message}", file=sys.stderr)
            if exc.hint():
                print(f"[hint] {exc.hint()}", file=sys.stderr)
        if output_format in ("json", "stream-json"):
            # machine consumers still get a parseable failure document
            error_payload = {"message": exc.message, "hint": exc.hint()}
            if output_format == "json":
                print(_json.dumps({"reply": "", "usage": None, "events": [],
                                   "error": error_payload}, ensure_ascii=False))
            else:
                print(_json.dumps({"schema_version": 1,
                                   "sequence": next(_envelope_seq),
                                   "timestamp": now_iso(),
                                   "type": "error", "data": error_payload},
                                  ensure_ascii=False), flush=True)
        return 1
    finally:
        with contextlib.suppress(Exception):
            await mcp.shutdown()
        aclose = getattr(provider, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()
    # persist the exchange (headless resume parity with the TUI)
    if resume is not None and resume_store is not None:
        final_event = next((e for e in collected
                            if e["type"] in ("turn_completed", "turn_done")), {})
        answer = str(final_event.get("content")
                     or final_event.get("text")
                     or "".join(_deltas))
        with contextlib.suppress(Exception):
            from oaset.agent.messages import Message
            from oaset.utils import run_io as _run_io2

            await _run_io2(resume_store.append_message, resume,
                           Message(role="assistant", content=answer))
            if final_event.get("usage"):
                await _run_io2(resume_store.append_usage, resume,
                               dict(final_event["usage"]), model_cfg.id)
    if output_format == "json":
        final = next((e for e in collected if e["type"] == "turn_completed"), {})
        payload = {
            "reply": final.get("content", ""),
            "usage": final.get("usage"),
            "events": [{"type": e["type"],
                        **{k: v for k, v in e.items() if k not in ("type", "message", "error", "hint")}}
                       for e in collected
                       if e["type"] in ("turn_started", "tool_call_started",
                                        "tool_completed", "turn_completed")],
            "error": None,
        }
        print(_json.dumps(_jsonable(payload), ensure_ascii=False))
    return 0


# ---------------------------------------------------------------- subcommands


def cmd_login(provider: str) -> int:
    import getpass

    try:
        cfg = load_config()
    except Exception as exc:
        from oaset.i18n import t

        print(t("config_parse_failed", path=config_path(), error=exc), file=sys.stderr)
        return 2
    # gateway transport tokens (telegram/discord/slack) are stored like
    # provider credentials even though they are not model providers — the
    # gateway error messages tell users to run exactly this
    if provider in ("telegram", "discord", "slack") and provider not in cfg.providers:
        from oaset.credentials import save_credential

        key = getpass.getpass(f"token for {provider}: ").strip()
        if not key:
            print("cancelled — empty token")
            return 1
        save_credential(provider, key)
        print(f"Stored token for {provider} in ~/.oaset/credentials/.")
        return 0
    if provider not in cfg.providers:
        print(
            f"error: unknown provider '{provider}'. Known: {', '.join(sorted(cfg.providers))}",
            file=sys.stderr,
        )
        return 2
    key = getpass.getpass(f"API key for {provider}: ").strip()
    if not key:
        print("cancelled — empty key")
        return 1
    from oaset.credentials import save_credential

    save_credential(provider, key)
    print(f"Stored API key for {provider} ({len(key)} chars) in ~/.oaset/credentials/.")
    return 0


def cmd_logout(provider: str) -> int:
    from oaset.credentials import delete_credential

    if delete_credential(provider):
        print(f"Removed stored credential for {provider}.")
        return 0
    print(f"no stored credential for '{provider}'")
    return 1


def cmd_trust_plugin(name: str) -> int:
    """Pin a project plugin's sha256 so load_plugins accepts it. Refuses
    paths/slashes: only a bare stem inside the known plugin dirs is valid."""
    from oaset.plugins import is_valid_plugin_name, plugin_dirs, trust_plugin_file

    if not is_valid_plugin_name(name):
        print(f"error: invalid plugin name {name!r}", file=sys.stderr)
        return 2
    for base in plugin_dirs(Path.cwd()):
        candidate = base / f"{name}.py"
        if candidate.is_file():
            digest = trust_plugin_file(candidate)
            print(f"Trusted {name} ({candidate})")
            print(f"  sha256 {digest}")
            print("Restart oaset or run /reload-plugins to load it.")
            return 0
    print(f"error: no plugin file '{name}.py' in any plugin directory", file=sys.stderr)
    return 1


def cmd_mcp(args: argparse.Namespace) -> int:
    """`oaset mcp list|trust|revoke` — inspect and manage the project-server
    trust boundary. User-level servers are always trusted; project-level
    servers from <cwd>/.oaset/mcp.json need an explicit trust pin (mirrors
    the plugins model: config edits invalidate the pin)."""
    from oaset.mcp.manager import load_server_specs, revoke_project_server, trust_project_server

    home = oaset_home()
    cwd = Path.cwd()
    action = args.action
    if action == "add":
        from oaset.mcp.registry import search_registry, to_server_entry
        from oaset.network import NetworkPolicy

        cfg = load_config()
        try:
            NetworkPolicy(cfg.network_mode).assert_allowed("pull")
        except Exception as exc:
            print(t("mcp_add_blocked", mode=cfg.network_mode, err=exc), file=sys.stderr)
            return 1
        name = args.name
        if not name:
            print(t("mcp_add_name_required"), file=sys.stderr)
            return 2
        if getattr(args, "url", None):
            if not args.url.startswith(("http://", "https://")):
                print(t("mcp_add_url_bad", url=args.url), file=sys.stderr)
                return 2
            candidates = [{"name": name, "description": "", "url": args.url,
                           "package": "", "transport": "http"}]
        else:
            print(t("mcp_add_searching", name=name))
            candidates = _run_sync(search_registry(name))
        if not candidates:
            print(t("mcp_add_not_found", name=name), file=sys.stderr)
            return 1
        chosen = candidates[0]
        if len(candidates) > 1:
            print(t("mcp_add_ambiguous", name=name))
            for c in candidates[:8]:
                print(f"  {c['name']:30} {c['description'][:60]}")
            return 2
        entry = to_server_entry(chosen) if not getattr(args, "url", None) else {
            "kind": "http", "url": chosen["url"]}
        try:
            if not _mcp_add_write(home, name, entry):
                print(t("mcp_add_duplicate", name=name), file=sys.stderr)
                return 1
        except Exception as exc:  # McpAdminError: refuse cleanly, no traceback
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if entry.get("kind") == "stdio":
            print(t("mcp_add_thirdparty_warn", package=entry.get("args", ["", ""])[-1]))
        print(f"  {entry.get('url') or entry.get('command')}")
        print(t("mcp_add_done", name=name, path=home / "mcp.json"))
        return 0
    if action == "list":
        specs = load_server_specs(home, cwd=cwd)
        if not specs:
            print("no MCP servers configured (~/.oaset/mcp.json or .oaset/mcp.json)")
            return 0
        for spec in specs:
            kind = spec.command or spec.url
            trust = "trusted" if spec.trusted else "UNTRUSTED (not started)"
            print(f"{spec.name}  [{spec.origin}]  {kind}  {trust}")
        return 0
    name = args.name
    if not name:
        print(f"error: `oaset mcp {action}` requires a server name", file=sys.stderr)
        return 2
    if action == "trust":
        digest = trust_project_server(home, cwd, name)
        if digest is None:
            print(
                f"error: no project-level server {name!r} in {cwd / '.oaset' / 'mcp.json'}",
                file=sys.stderr,
            )
            return 1
        print(f"Trusted MCP server {name!r} for this workspace")
        print(f"  sha256 {digest}")
        print("Restart oaset (or /mcp reload) to start it.")
        return 0
    # revoke
    if revoke_project_server(home, cwd, name):
        print(f"Revoked trust for MCP server {name!r} in this workspace")
        return 0
    print(f"error: no trust record for server {name!r} in this workspace", file=sys.stderr)
    return 1


async def cmd_market(args: argparse.Namespace) -> int:
    """`oaset market` — community plugin market:
    multiple sources, ONE active at a time; installs go through the Update
    Center (network policy + sha256 + transactional rollback)."""
    from oaset.market import MarketManager, UpdateError

    market = MarketManager(load_config(create=False))
    action, name = args.action, args.name
    try:
        if action == "list":
            active = market.active()
            for source in market.sources():
                mark = "*" if source.name == active.name else " "
                print(f"{mark} {source.name}  {source.url}")
            return 0
        if action == "add":
            if not name or not args.url:
                print("usage: oaset market add NAME https://…/index.json", file=sys.stderr)
                return 2
            market.add_source(name, args.url)
            print(f"source added: {name} -> {args.url}")
            return 0
        if action == "use":
            if not name:
                print("usage: oaset market use NAME", file=sys.stderr)
                return 2
            market.use_source(name)
            print(f"active source: {name}")
            return 0
        if action == "remove":
            if not name:
                print("usage: oaset market remove NAME", file=sys.stderr)
                return 2
            if market.remove_source(name):
                print(f"source removed: {name}")
                return 0
            print(f"error: unknown source {name!r}", file=sys.stderr)
            return 1
        if action == "installed":
            installed = market.installed()
            if not installed:
                print("(no plugins installed)")
                return 0
            for item in installed:
                version = f" v{item['version']}" if item["version"] else ""
                print(f"{item['name']}{version}  {item['path']}")
            return 0
        if action == "install":
            if not name:
                print("usage: oaset market install PLUGIN", file=sys.stderr)
                return 2
            print(f"installing {name} from {market.active().name}…", file=sys.stderr)
            result, entry = await market.install(name, dry_run=False)
            print(f"installed {name} v{entry.version}; restart oaset or /reload-plugins")
            for note in result.notes:
                print(f"  {note}")
            return 0
        print(f"error: unknown action {action!r}", file=sys.stderr)
        return 2
    except UpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        return 1


def _is_frozen() -> bool:
    """Running as a PyInstaller one-file exe (sys.executable is oaset.exe,
    NOT a python interpreter — `pip` via sys.executable cannot work there)."""
    return bool(getattr(sys, "frozen", False))


def cmd_upgrade() -> int:
    """Legacy self-upgrade: direct pip — source checkouts only.

    A frozen exe has no usable pip inside sys.executable; its update path is
    the Update Center or a fresh GitHub Release download."""
    if _is_frozen():
        print("This is a packaged binary — pip self-upgrade does not apply.", file=sys.stderr)
        print("  update:  oaset update check / oaset update apply <core-release>", file=sys.stderr)
        print("  manual:  download the matching release from GitHub Releases", file=sys.stderr)
        return 5
    print("Upgrading oaset-cli via pip…")
    try:
        proc = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "oaset-cli"])
        return proc.returncode
    except OSError as exc:
        print(f"upgrade failed: {exc}", file=sys.stderr)
        return 1


async def cmd_selfmaint(args: argparse.Namespace) -> int:
    """体验计划 headless run: token-powered check/plan/compat/turn, with an
    optional hash-gated apply. Never mutates config; report or JSON out."""
    from oaset.config import build_provider_chain
    from oaset.maintenance import MaintenanceError, SelfMaintenance

    try:
        cfg = load_config()
        cfg.maintenance.enabled = True  # explicit CLI invocation IS the opt-in
        model_cfg = resolve_model(cfg, args.model if hasattr(args, "model") else None)
    except Exception as exc:
        from oaset.i18n import t

        print(t("config_parse_failed", path=config_path(), error=exc), file=sys.stderr)
        return 2
    provider, _fallbacks = build_provider_chain(cfg, model_cfg)
    if getattr(args, "allow_apply", False):
        cfg.maintenance.allow_apply = True
    maint = SelfMaintenance(cfg, Path.cwd(), provider=provider)
    try:
        result = await maint.run(
            apply=True if getattr(args, "allow_apply", False) else None,
            budget_tokens=getattr(args, "budget", None))
    except MaintenanceError as exc:
        print(f"error: [{exc.code}] {exc}" + (f"\nhint: {exc.hint}" if exc.hint else ""),
              file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        import json as _json

        print(_json.dumps({
            "phases": result.phases,
            "plan": result.plan,
            "applied": result.applied,
            "compat": result.compat,
            "usage": result.usage,
            "errors": result.errors,
            "cancelled": result.cancelled,
            "requested_apply": result.requested_apply,
            "effective_apply": result.effective_apply,
            "blocked": result.blocked_reasons,
        }, ensure_ascii=False))
    else:
        print(result.render())
    return 0 if not result.errors else 1


async def cmd_update(args: argparse.Namespace) -> int:
    """`oaset update` — non-interactive Update Center entry (read-only actions)."""
    from oaset.i18n import t
    from oaset.update import UpdateError, UpdateManager, UpdatePlan, UpdateResult

    try:
        cfg = load_config()
    except Exception as exc:
        print(t("config_parse_failed", path=config_path(), error=exc), file=sys.stderr)
        return 2
    mgr = UpdateManager(cfg)
    kind = getattr(args, "kind", "all")
    try:
        if args.action == "doctor":
            from oaset.update import _catalog_cache_path

            path = _catalog_cache_path()
            entries, refreshed_at = mgr._read_cache()
            print(f"source     {mgr.source.name} ({mgr.source.url})")
            print(f"cache      {path} ({'present' if path.exists() else 'missing'})")
            print(f"entries    {len(entries)}")
            print(f"network    {cfg.network_mode}")
            print("apply       plugin(file) / mcp(mcp.json) / provider+model(config.toml)")
            print("            core(zip bundle, transactional: hash+backup+rollback)")
            print("            one apply = one transaction: staging dir + manifest; a")
            print("            failure restores every committed step in reverse order")
            print("rollback    last | <target> | txn:<id> (per-key for shared config files)")
            return 0
        if args.action == "history":
            records = mgr.history(limit=50)
            if getattr(args, "json", False):
                import json as _json

                print(_json.dumps(records, ensure_ascii=False))
                return 0
            if not records:
                print(t("upd_history_empty"))
                return 0
            for r in records:
                print(f"{r.get('ts', '?')}  {r.get('action'):>8}  {r.get('target') or r.get('applied') or ''}"
                      + (f"  v{r.get('version')}" if r.get("version") else ""))
            return 0
        if args.action == "apply":
            result: UpdateResult | UpdatePlan = await mgr.apply(
                targets=list(args.targets), dry_run=bool(args.dry_run))
        elif args.action == "rollback":
            result = await mgr.rollback(args.targets[0] if args.targets else "last")
        elif args.action == "list":
            result = mgr.list_entries(kind)
        elif args.action == "plan":
            result = mgr.plan(kind)
        else:
            result = await mgr.run(args.action, kind)
            if kind != "all" and isinstance(result, UpdateResult):
                result.entries = [e for e in result.entries if e.kind == kind]
    except UpdateError as exc:
        print(f"error: [{exc.code}] {exc}" + (f"\nhint: {exc.hint}" if exc.hint else ""), file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        import json as _json

        if isinstance(result, UpdatePlan):
            payload: dict = {
                "action": "plan",
                "targets": [
                    {"kind": e.kind, "name": e.name, "version": e.version,
                     "requires_oaset": e.requires_oaset, "description": e.description}
                    for e in result.targets
                ],
                "skipped": [{"name": n, "reason": r} for n, r in result.skipped],
            }
        else:
            payload = {
                "action": result.action,
                "refreshed_at": result.refreshed_at,
                "from_cache": result.from_cache,
                "errors": result.errors,
                "entries": [
                    {"kind": e.kind, "name": e.name, "version": e.version,
                     "requires_oaset": e.requires_oaset, "description": e.description}
                    for e in result.entries
                ],
            }
        print(_json.dumps(payload, ensure_ascii=False))
        return 0
    print(result.render() if hasattr(result, "render") else str(result))
    return 0 if getattr(args, "action", "") == "plan" or not getattr(result, "errors", []) else 1


def cmd_config(action: str, key: str | None, value: str | None) -> int:
    try:
        cfg = load_config()
    except Exception as exc:
        from oaset.i18n import t

        print(t("config_parse_failed", path=config_path(), error=exc), file=sys.stderr)
        return 2
    if action == "list" or not key:
        for name in sorted(vars(cfg)):
            val = getattr(cfg, name)
            if isinstance(val, (str, int, float, bool)):
                print(f"{name} = {val}")
        print(f"providers = {', '.join(sorted(cfg.providers))}")
        print(f"models = {len(cfg.models)} configured")
        return 0
    if not value:
        print("usage: oaset config set <key> <value>", file=sys.stderr)
        return 2
    simple = {
        "default_provider": str, "default_model": str, "permission_mode": str,
        "shell_backend": str, "ssh_target": str, "docker_container": str,
        "tool_output_limit": int, "max_iterations": int,
    }
    if key == "ui.theme":
        cfg.ui.theme = value
    elif key in simple:
        try:
            cfg.__dict__[key] = simple[key](value)
        except ValueError:
            print(f"error: {key} expects a value of type {simple[key].__name__}",
                  file=sys.stderr)
            return 2
    elif key.startswith("enabled_tools.") or key == "enabled_tools":
        cfg.enabled_tools = [t for t in value.split(",") if t]
    elif key == "disabled_tools":
        cfg.disabled_tools = [t for t in value.split(",") if t]
    else:
        print(f"error: unknown config key '{key}'", file=sys.stderr)
        return 2
    save_config(cfg)
    print(f"{key} = {value}")
    return 0


def cmd_setup() -> int:
    """One wizard for provider, key and model."""
    import getpass

    try:
        cfg = load_config()
    except Exception as exc:
        print(t("config_parse_failed", path=config_path(), error=exc), file=sys.stderr)
        return 2
    print("oAset setup — configure a provider, key and default model")
    providers = sorted(cfg.providers)
    for i, name in enumerate(providers, 1):
        print(f"  {i}. {name}  ({cfg.providers[name].base_url})")
    try:
        choice = input(t("setup_provider_choice", count=len(providers))).strip()
    except (EOFError, KeyboardInterrupt):
        print(t("cli_no_input_cancelled"), file=sys.stderr)
        return 1
    if not choice.isdigit() or not (1 <= int(choice) <= len(providers)):
        print(t("setup_cancelled"))
        return 1
    provider = providers[int(choice) - 1]
    try:
        key = getpass.getpass(f"API key for {provider} (empty = keep existing): ").strip()
    except (EOFError, KeyboardInterrupt):
        print(t("cli_no_input_cancelled"), file=sys.stderr)
        return 1
    if key:
        from oaset.credentials import save_credential

        save_credential(provider, key)
        print(f"  key stored for {provider}")
    models = [mid for mid, m in sorted(cfg.models.items()) if m.provider == provider]
    if not models:
        print(t("setup_no_models", provider=provider, path=config_path()))
        return 1
    for i, mid in enumerate(models, 1):
        print(f"  {i}. {mid}  ({cfg.models[mid].display_name or mid})")
    try:
        pick = input(t("setup_model_choice", count=len(models))).strip()
    except (EOFError, KeyboardInterrupt):
        print(t("cli_no_input_cancelled"), file=sys.stderr)
        return 1
    if not pick.isdigit() or not (1 <= int(pick) <= len(models)):
        print(t("setup_cancelled"))
        return 1
    cfg.default_model = models[int(pick) - 1]
    cfg.default_provider = provider
    save_config(cfg)
    print(f"Done. default_model = {cfg.default_model}. Start with: oaset")
    return 0


def cmd_gateway(args) -> int:
    from oaset.gateway import serve
    from oaset.i18n import t
    from oaset.network import NetworkPolicy

    try:
        cfg = load_config()
    except Exception as exc:
        print(t("config_parse_failed", path=config_path(), error=exc), file=sys.stderr)
        return 2
    if args.yolo:
        cfg.permission_mode = "auto"
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    provider = None
    if args.mock:
        from oaset.providers.mock import MockProvider

        provider = MockProvider()
    # BEFORE any transport branch returns: the cleanup Job Object must cover
    # every gateway face (README promises exactly that), not just the HTTP one
    _mount_process_guards()

    policy = NetworkPolicy(cfg.network_mode)
    using_transport = any(getattr(args, f, False) for f in ("telegram", "discord", "slack"))
    # gate the SENDING, not the safe default: the old condition rejected the
    # receive-only default outright while --send-replies (the actual push!)
    # bypassed the policy — pull_only then pushed user content to a third
    # party, exactly what the mode forbids
    if using_transport and args.send_replies and not policy.allows("push"):
        print(t("gateway_recv_only"), file=sys.stderr)
        return 2
    if using_transport and policy.mode == "local_only":
        print(t("gateway_local_only"), file=sys.stderr)
        return 2
    service = __import__("oaset.gateway", fromlist=["GatewayService"]).GatewayService(
        cfg, cwd, model_id=args.model, yolo=bool(args.yolo), provider=provider,
        panel=bool(getattr(args, "panel", False)),
        panel_timeout=float(getattr(args, "panel_timeout", 120.0)),
    )
    allow_send = bool(getattr(args, "send_replies", False))
    import os as _os

    def _token(env_name, cred_name):
        from oaset.credentials import load_credential

        return _os.environ.get(env_name) or load_credential(cred_name)

    if getattr(args, "discord", False):
        from oaset.transports import DiscordTransport

        token = _token("DISCORD_BOT_TOKEN", "discord")
        if not token:
            print("error: no Discord token (set DISCORD_BOT_TOKEN or `oaset login discord`)", file=sys.stderr)
            return 2
        print("oAset gateway: Discord gateway transport started")
        try:
            arun(DiscordTransport(service, token).run_forever())
        except KeyboardInterrupt:
            pass
        return 0
    if getattr(args, "slack", False):
        from oaset.transports import SlackEventsTransport

        token = _token("SLACK_BOT_TOKEN", "slack")
        if not token:
            print("error: no Slack token (set SLACK_BOT_TOKEN or `oaset login slack`)", file=sys.stderr)
            return 2
        print(f"oAset gateway: Slack Events transport on http://{args.host}:{args.port}/slack/events")
        transport = SlackEventsTransport(service, token, allow_send=allow_send)
        arun(transport.serve_http(host=args.host, port=args.port))
        return 0
    if getattr(args, "telegram", False):
        import os

        from oaset.credentials import load_credential
        from oaset.transports import TelegramTransport

        token = os.environ.get("TELEGRAM_BOT_TOKEN") or load_credential("telegram")
        if not token:
            print("error: no Telegram token (set TELEGRAM_BOT_TOKEN or `oaset login telegram`)", file=sys.stderr)
            return 2
        print("oAset gateway: Telegram long-polling started")
        try:
            arun(TelegramTransport(service, token, allow_send=allow_send).run_forever())
        except KeyboardInterrupt:
            pass
        return 0
    print(f"oAset gateway on http://{args.host}:{args.port}  (POST /message {{\"text\": ...}})")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: non-loopback bind — the agent is reachable from your network; "
              "local-first default is 127.0.0.1", file=sys.stderr)
    server = serve(cfg, cwd, host=args.host, port=args.port, model_id=args.model,
                   yolo=bool(args.yolo), provider=provider,
                   panel=bool(getattr(args, "panel", False)),
                   panel_timeout=float(getattr(args, "panel_timeout", 120.0)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        from oaset.gateway import shutdown_server

        shutdown_server(server)
    return 0


def cmd_cron(args) -> int:
    from oaset import cron as cronmod

    if args.action == "list":
        jobs = cronmod.load_jobs()
        if not jobs:
            print("(no jobs)")
            return 0
        for j in jobs:
            print(f"[{j.id[-8:]}] {'enabled ' if j.enabled else 'disabled'} {j.name} · {j.schedule} · last {j.last_run or 'never'}")
        return 0
    if args.action == "add":
        if not (args.name and args.schedule and args.prompt):
            print("usage: oaset cron add --name N --schedule 'daily 09:00|every 20m|cron5' --prompt '...'", file=sys.stderr)
            return 2
        # garbage schedules used to be accepted, stored, and then NEVER fire
        # (is_due is permanently False for unparseable input) — dead jobs
        # with a model prompt attached, listed as if they worked
        if (cronmod.parse_interval_minutes(args.schedule) is None
                and not cronmod._DAILY_RE.match(args.schedule)
                and len(args.schedule.split()) != 5):
            print("error: unsupported schedule — use 'every Nm', 'daily HH:MM', "
                  "or a 5-field cron expression", file=sys.stderr)
            return 2
        job = cronmod.add_job(args.name, args.schedule, args.prompt, model=args.model or "")
        print(f"added {job.name} ({job.schedule}) id={job.id[-8:]}")
        return 0
    if args.action == "remove":
        ok = cronmod.remove_job(args.id or "")
        print("removed" if ok else "not found")
        return 0 if ok else 1
    if args.action == "run":
        # `cron run` means "run what is due now". Firing EVERY job for real
        # (model calls + side effects) used to need no confirmation at all.
        return arun(_cron_fire_due(args.id or None, force=bool(args.id)))
    if args.action == "daemon":
        return arun(_cron_daemon())
    return 2


async def _cron_fire_due(job_id: str | None, force: bool = False) -> int:
    import datetime as dt

    from oaset import cron as cronmod
    from oaset.agent.runner import execute_prompt

    try:
        cfg = load_config()
    except Exception as exc:
        from oaset.i18n import t

        print(t("config_parse_failed", path=config_path(), error=exc), file=sys.stderr)
        return 2
    cwd = Path.cwd()
    fired = 0
    for job in cronmod.load_jobs():
        if job_id and not job.id.endswith(job_id):
            continue
        now = dt.datetime.now()
        if not force and not cronmod.is_due(job, now):
            continue
        print(f"[cron] firing {job.name} ({job.schedule})…")
        try:
            reply = await execute_prompt(cfg, cwd, job.prompt, model_id=job.model or None)
            print(f"[cron] {job.name} -> {reply[:160]}")
            fired += 1
        except Exception as exc:
            print(f"[cron] {job.name} FAILED: {exc}", file=sys.stderr)
        job.last_run = cronmod.now_iso()
        jobs = cronmod.load_jobs()
        for j in jobs:
            if j.id == job.id:
                j.last_run = job.last_run
        cronmod.save_jobs(jobs)
    if not fired and not force:
        print("nothing due")
    if job_id and not fired:
        # a --id that matches nothing used to exit 0 in silence — scripts
        # could not tell a typo from success
        print(f"error: no cron job matches id '{job_id}'", file=sys.stderr)
        return 1
    return 0


async def _cron_daemon() -> int:
    print("oAset cron daemon started (Ctrl-C to stop)…")
    try:
        while True:
            await _cron_fire_due(None, force=False)
            import asyncio

            await asyncio.sleep(30)
    except KeyboardInterrupt:
        print("daemon stopped")
    return 0


async def cmd_batch(args) -> int:
    from oaset.batch import run_batch

    try:
        cfg = load_config()
    except Exception as exc:
        from oaset.i18n import t

        print(t("config_parse_failed", path=config_path(), error=exc), file=sys.stderr)
        return 2
    if args.yolo:
        cfg.permission_mode = "auto"
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    provider = None
    if args.mock:
        from oaset.providers.mock import MockProvider

        provider = MockProvider()
    tasks = Path(args.tasks)
    if not tasks.is_file():
        print(f"error: tasks file '{tasks}' not found", file=sys.stderr)
        return 2

    def on_result(record):
        status = "ok" if not record["error"] else f"ERROR {record['error']}"
        print(f"[batch] {record['id']}: {status} ({record['elapsed']}s)")

    results = await run_batch(
        cfg, cwd, tasks, Path(args.out), model_id=args.model, yolo=bool(args.yolo),
        on_result=on_result, provider=provider,
    )
    ok = sum(1 for r in results if not r["error"])
    print(f"done: {ok}/{len(results)} succeeded -> {args.out}")
    return 0 if ok == len(results) else 1


async def cmd_matrix(args) -> int:
    """`oaset matrix`: parallel tasks, one git worktree each (kept for review)."""
    import json as _json

    from oaset.matrix import render_report, run_matrix

    try:
        cfg = load_config()
    except Exception as exc:
        from oaset.i18n import t

        print(t("config_parse_failed", path=config_path(), error=exc), file=sys.stderr)
        return 2
    cwd = Path.cwd()
    tasks_path = Path(args.tasks)
    if not tasks_path.is_file():
        print(f"error: tasks file '{tasks_path}' not found", file=sys.stderr)
        return 2
    tasks: list[dict] = []
    for line in tasks_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            tasks.append(_json.loads(line))
        except _json.JSONDecodeError as exc:
            print(f"error: bad task line: {exc}", file=sys.stderr)
            return 2
    if not tasks:
        print("error: no tasks in file", file=sys.stderr)
        return 2
    provider = None
    if args.mock:
        from oaset.providers.mock import MockProvider

        provider = MockProvider()
    print(f"[matrix] {len(tasks)} task(s), max {args.max_parallel} parallel — "
          "each in its own worktree (kept for review: TUI /worktree lists them)")
    results = await run_matrix(cfg, cwd, tasks, model_id=args.model,
                               max_parallel=int(args.max_parallel),
                               on_event=print, provider=provider)
    print()
    print(render_report(results))
    return 0 if all(r["ok"] for r in results) else 1


def cmd_acp(args) -> int:
    """ACP-lite on stdio: editors drive SessionHost without a TUI."""
    from oaset.acp import serve_stdio

    cwd = Path(args.acp_cwd).resolve() if getattr(args, "acp_cwd", None) else Path.cwd()
    print(f"oAset ACP server on stdio (cwd={cwd})", file=sys.stderr, flush=True)
    _mount_process_guards()
    serve_stdio(cwd, yolo=bool(getattr(args, "yolo", False)),
                lsp=bool(getattr(args, "lsp", False)))
    return 0


def _mount_process_guards() -> None:
    """Serve-face lifecycle guard: kill-on-close Job Object (Windows) so MCP
    servers and shells die with this process, plus graceful SIGINT/SIGTERM.
    The TUI mounts its own; every stdio/HTTP serve face mounts here."""
    try:
        if os.name == "nt":
            from oaset.sandbox import attach_current_process_to_cleanup_job

            attach_current_process_to_cleanup_job()
    except Exception:
        pass
    try:
        import signal

        def _bye(signum, frame) -> None:
            raise SystemExit(0)

        for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
            handler = getattr(signal, sig, None)
            if handler is not None:
                try:
                    signal.signal(handler, _bye)
                except (ValueError, OSError):
                    pass
    except Exception:
        pass


def cmd_mcp_serve(args) -> int:
    """把 oAset 工具集暴露为 stdio MCP 服务器。"""
    from oaset.mcp_server import serve_stdio

    cwd = Path(args.mcp_cwd).resolve() if args.mcp_cwd else Path.cwd()
    print(f"oAset MCP server on stdio (cwd={cwd}, "
          f"expose={'all' if args.all_tools else 'readonly'})", file=sys.stderr, flush=True)
    if args.all_tools:
        # the MCP client becomes the only approver: say so loudly at startup
        print(t("mcp_serve_all_tools_warning"), file=sys.stderr, flush=True)
    _mount_process_guards()
    serve_stdio(cwd, expose="all" if args.all_tools else "readonly", token=args.token)
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    cfg = load_config(create=False)
    action = getattr(args, "action", "list") or "list"
    if action == "add":
        from oaset.config import save_config
        from oaset.models_wizard import WizardCancelled, run_models_add_wizard

        try:
            summary = run_models_add_wizard(cfg)  # mutates in-memory cfg only
        except WizardCancelled:
            print(t("cli_no_input_cancelled"), file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if _confirm_save(summary):
            save_config(cfg)
            print(t("models_add_saved", model=summary["model"]))
            if getattr(args, "verify", False):
                rc = arun(cmd_models_verify(cfg, summary["model"], json_out=bool(getattr(args, "json", False))))
                return rc if rc == 0 else 1
            return 0
        print(t("models_add_discarded"))
        return 1
    if action == "verify":
        if getattr(args, "all", False):
            return arun(cmd_models_verify_all(cfg, json_out=bool(args.json)))
        model_id = getattr(args, "verify_model", None)
        if not model_id:
            print(t("models_verify_needs_id"), file=sys.stderr)
            return 2
        return arun(cmd_models_verify(cfg, model_id, json_out=bool(args.json)))
    try:
        from oaset.local_models import refresh_local_models

        arun(refresh_local_models(cfg, network_mode=cfg.network_mode))
    except Exception:
        pass
    for mid in available_model_ids(cfg):
        m = cfg.models[mid]
        caps = ",".join(m.capabilities)
        print(f"{mid:38} provider={m.provider:12} context={m.max_context_size:<8} caps=[{caps}]")
    # zero-config integration: if a local model server is already running on
    # this machine, say so — the user adds it with /models add, nothing else
    try:
        from oaset.local_servers import discover_local_servers

        for server in arun(discover_local_servers()):
            print(t("models_local_detected", vendor=server["vendor"],
                    url=server["base_url"], count=len(server["models"])))
    except Exception:
        pass  # discovery is an annotation, never a failure
    return 0


def _confirm_save(summary: dict) -> bool:
    """Explicit consent before writing config.

    A closed stdin (piped EOF, Ctrl-D) is NOT a yes — the same rule the tool
    gate follows: silence is never approval.
    """
    try:
        answer = input(t("setup_confirm_write")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


async def _probe_model(cfg: AppConfig, model_id: str,
                       provider: Any = None) -> tuple[dict, int]:
    """One tiny completion through the actual provider -> (verdict, exit code).

    rc 2 = the model/provider could not be resolved at all; rc 1 = the probe
    ran and failed; rc 0 = verified. Never mutates config; every transport
    failure still produces a verdict so a batch run reports EVERY model
    instead of dying on the first. `provider` is injectable for tests.
    """
    import time as _time

    from oaset.providers.base import ProviderError

    try:
        model_cfg = resolve_model(cfg, model_id)
    except KeyError as exc:
        return {"model": model_id, "provider": "?", "endpoint": "", "ok": False,
                "error": str(exc)}, 2
    if provider is None:
        from oaset.config import build_provider

        try:
            provider = build_provider(cfg, model_cfg)
        except Exception as exc:
            return {"model": model_id, "provider": model_cfg.provider,
                    "endpoint": "", "ok": False,
                    "error": f"provider construction failed: {exc}"}, 2
    from oaset.network import NetworkBlockedError

    started = _time.monotonic()
    verdict = {"model": model_cfg.id, "provider": model_cfg.provider,
               "endpoint": getattr(provider, "base_url", "") or cfg.providers[model_cfg.provider].base_url,
               "ok": False}
    try:
        from oaset.providers.base import ContentDelta

        chunks = []
        async for chunk in provider.stream(
            [{"role": "user", "content": "Reply with exactly: OK"}], tools=[],
        ):
            if isinstance(chunk, ContentDelta):
                chunks.append(chunk.text)
        verdict["ok"] = True
        verdict["reply"] = "".join(chunks)[:80]
    except NetworkBlockedError as exc:
        verdict["error"] = f"network blocked: {exc}"
    except ProviderError as exc:
        verdict["error"] = str(getattr(exc, "message", exc))
        verdict["hint"] = exc.hint() if callable(getattr(exc, "hint", None)) else ""
    except Exception as exc:  # transport failures must still produce a verdict
        verdict["error"] = f"{type(exc).__name__}: {exc}"
    verdict["elapsed_ms"] = int((_time.monotonic() - started) * 1000)

    aclose = getattr(provider, "aclose", None)
    if aclose is not None:
        with contextlib.suppress(Exception):
            await aclose()
    return verdict, (0 if verdict["ok"] else 1)


async def cmd_models_verify(cfg: AppConfig, model_id: str, json_out: bool = False,
                            provider: Any = None) -> int:
    """Real connectivity probe: one tiny completion through the actual provider.

    This is the verification half of UPDATE-05 — a catalog-applied model is
    only trustworthy after this passes. Never mutates config; --json emits a
    machine-readable verdict for scripting. `provider` is injectable for tests.
    """
    verdict, rc = await _probe_model(cfg, model_id, provider=provider)
    if rc == 2:
        print(f"error: {verdict['error']}", file=sys.stderr)
        return 2

    if json_out:
        import json as _json

        print(_json.dumps(verdict, ensure_ascii=False))
    else:
        status = "OK" if verdict["ok"] else "FAIL"
        print(f"{verdict['model']}: {status}  ({verdict['elapsed_ms']}ms via {verdict['endpoint']})")
        if verdict["ok"]:
            print(f"  reply: {verdict.get('reply', '')!r}")
        else:
            print(f"  error: {verdict.get('error', '')}")
            if verdict.get("hint"):
                print(f"  hint:  {verdict['hint']}")
    return 0 if verdict["ok"] else 1


async def cmd_models_verify_all(cfg: AppConfig, json_out: bool = False) -> int:
    """Stage-0 honesty lever: probe EVERY configured model and print the matrix.

    The catalog can claim anything; this command is what makes the claim check-
    able. The offline mock is reported as skipped, never as verified — a green
    row must mean a real endpoint answered. Exit 0 only when every probed
    model passed.
    """
    from oaset.config import is_mock_provider

    ids = available_model_ids(cfg)
    rows: list[dict] = []
    skipped = 0
    for mid in ids:
        model_cfg = cfg.models.get(mid)
        pcfg = cfg.providers.get(model_cfg.provider) if model_cfg else None
        if model_cfg is None or (pcfg is not None
                                 and is_mock_provider(model_cfg.provider, pcfg)):
            skipped += 1
            rows.append({"model": mid, "provider": model_cfg.provider if model_cfg else "?",
                         "endpoint": "", "ok": None, "elapsed_ms": 0})
            continue
        verdict, _rc = await _probe_model(cfg, mid)
        rows.append(verdict)

    probed = [r for r in rows if r["ok"] is not None]
    failed = [r for r in probed if not r["ok"]]
    ok = [r for r in probed if r["ok"]]

    if json_out:
        import json as _json

        print(_json.dumps({"results": rows, "ok": not failed and bool(probed),
                           "verified": len(ok), "failed": len(failed),
                           "skipped": skipped}, ensure_ascii=False, indent=2))
    else:
        print(t("verify_all_title", count=len(ids)))
        for r in rows:
            if r["ok"] is None:
                print(f"  {r['model']:40} {t('verify_all_skipped')}")
            elif r["ok"]:
                print(f"  {r['model']:40} OK    {r['elapsed_ms']}ms  {r['endpoint']}")
            else:
                detail = " ".join(str(r.get("error", "")).split())[:80]
                print(f"  {r['model']:40} FAIL  {r['elapsed_ms']}ms  {detail}")
        if probed:
            print(t("verify_all_summary", ok=len(ok), failed=len(failed),
                    skipped=skipped))
        else:
            print(t("verify_all_nothing"))
    return 0 if probed and not failed else (0 if not probed else 1)


def cmd_sessions(args=None) -> int:
    import json as _json

    from oaset.session import SessionStore

    store = SessionStore()
    metas = store.list_sessions(limit=30)
    if getattr(args, "json", False):
        # stable machine contract for desktop shells / scripts
        print(_json.dumps([
            {"session_id": m.session_id, "cwd": m.cwd, "title": m.title,
             "model": m.model, "created_at": m.created_at,
             "updated_at": m.updated_at, "message_count": m.message_count}
            for m in metas
        ], ensure_ascii=False, indent=2))
        return 0
    if not metas:
        print(t("sessions_none"))
        return 0
    here = Path.cwd()
    for m in metas:
        title = m.title[:48] or t("sessions_untitled")
        # Sessions from other working directories are listed too: without a
        # marker they are indistinguishable from this project's history.
        where = ""
        if m.cwd and _other_dir(m.cwd, here):
            where = "  " + t("sessions_other_dir", dir=Path(m.cwd).name or m.cwd)
        print(f"{m.session_id[-13:]}  {m.updated_at[:16]}  {m.message_count:>3} msgs  "
              f"{m.model:28} {title}{where}")
    return 0


def _other_dir(cwd: str, here: Path) -> bool:
    try:
        return Path(cwd).resolve() != here
    except OSError:
        return cwd != str(here)


def _mcp_add_write(home, name: str, entry: dict) -> bool:
    """Add one entry to the USER mcp.json (user-origin entries are trusted by
    ownership — that is the trust record for this path). Returns False when an
    entry with the same name already exists (the caller refuses: a silent
    overwrite used to replace a trusted server's config wholesale).

    Raises McpAdminError when the existing file cannot be PARSED — treating
    a truncated/hand-mangled mcp.json as empty config used to silently
    erase every configured server on the next write."""
    import json

    from oaset.tui.mcp_admin import McpAdminError, write_servers

    path = home / "mcp.json"
    servers: dict = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # refuse to touch a file we cannot read: the old fallback to {}
            # wiped every configured server with exit 0 (data loss repro)
            raise McpAdminError(
                "mcp_unparseable",
                f"{path} is not valid JSON ({exc}); fix or remove it before "
                "adding servers — refusing to overwrite it") from exc
        if not isinstance(raw, dict):
            raise McpAdminError("mcp_not_object",
                                f"{path} does not contain a JSON object; "
                                "refusing to overwrite it")
        # preserve entries declared under either accepted key — writing the
        # canonical shape back after reading only "mcpServers" used to DROP
        # every server a file had declared under "servers"
        servers = dict(raw.get("mcpServers") or raw.get("servers") or {})
    if name in servers:
        return False
    servers[name] = entry
    write_servers(path, servers)
    return True


def cmd_offline_check(cfg: AppConfig | None = None) -> int:
    """Air-gap readiness: what still works when the network is gone.

    The promise being checked is the product's first README scenario — the
    plane, the intranet, the classified bench. Nothing here dials out except
    loopback probes (allowed under every network mode).
    """

    from oaset.config import is_mock_provider, load_config, provider_credential_state
    from oaset.local_servers import discover_local_servers

    cfg = cfg or load_config()
    rows: list[tuple[str, str]] = []

    # 1) local model servers already running on this machine
    servers = _run_sync(discover_local_servers())
    for s in servers:
        rows.append(("local-server",
                     f"{s['vendor']} @ {s['base_url']} ({len(s['models'])} models)"))
    if not servers:
        rows.append(("local-server", t("offline_no_server")))

    # 2) models usable offline: the offline mock, or a provider whose local
    #    server was JUST detected — a preset without a running server is not
    #    "ready" (listing it claimed readiness the machine could not back)
    detected_vendors = {s["vendor"] for s in servers}
    detected_urls = {s["base_url"] for s in servers}
    offline_models: list[str] = []
    for mid in available_model_ids(cfg):
        m = cfg.models[mid]
        pcfg = cfg.providers.get(m.provider)
        if pcfg is None:
            continue
        base = pcfg.base_url or ""
        local_ready = bool(servers) and (m.provider in detected_vendors
                                         or base in detected_urls)
        if is_mock_provider(m.provider, pcfg) or local_ready:
            if provider_credential_state(cfg, m.provider) == "ready":
                offline_models.append(mid)
    for mid in offline_models[:6]:
        m = cfg.models[mid]
        rows.append(("offline-model",
                     f"{mid} (context {m.max_context_size:,} tok)"))
    if len(offline_models) > 6:
        rows.append(("offline-model", f"... +{len(offline_models) - 6} more"))
    if not offline_models:
        rows.append(("offline-model", t("offline_no_models")))

    # 3) tools that never need the network — DERIVED from the registry's
    #    network flags, not a hand-written list: a snapshot here drifted
    #    every time a tool was added or renamed, quietly mis-claiming
    #    air-gap capability
    from oaset.tools import default_tools

    local_names = [t.name for t in default_tools() if not t.network]
    if not local_names:
        rows.append(("offline-tools", t("offline_no_tools")))
    for i in range(0, len(local_names), 8):
        rows.append(("offline-tools" if i == 0 else "",
                     " ".join(local_names[i:i + 8])))

    # 4) transcript / checkpoint durability (all local, lock-protected)
    rows.append(("durability", f"{config_path().parent} (sessions, checkpoints, evidence)"))

    # 5) headroom: disk space for transcripts/checkpoints/evidence, and the
    #    context budget the offline models give the agent
    import shutil as _shutil

    try:
        free = _shutil.disk_usage(config_path().parent).free
        rows.append(("disk-free", f"{free / (1 << 30):.1f} GB on {config_path().parent.anchor}"))
    except OSError:
        rows.append(("disk-free", "unknown"))
    if offline_models:
        best = max(cfg.models[mid].max_context_size or 0 for mid in offline_models)
        rows.append(("context-headroom", t("offline_context_headroom", tok=f"{best:,}")))

    print(t("offline_title"))
    for name, detail in rows:
        print(f"  {name:14} {detail}")
    verdict = bool(offline_models)
    print(t("offline_verdict", ready=("YES" if verdict else "NO")))
    return 0 if verdict else 1


def _run_sync(coro):
    import asyncio as _asyncio

    return _asyncio.run(coro)


def _is_loopback_base(base: str) -> bool:
    from oaset.network import is_loopback_url

    return is_loopback_url(base or "")


def cmd_evidence(args: argparse.Namespace) -> int:
    """Replay/export the /evidence audit chain of one session.

    This is the receipt side of Evidence-Gated Done: the transcript already
    records WHY each completion was justified; this command is how a human
    (or a reviewer) reads it without opening the JSONL.
    """
    from oaset.session import SessionStore

    store = SessionStore()
    entries = store.evidence_entries(args.session)
    if not entries:
        print(t("evidence_none", sid=args.session))
        return 1
    lines = [t("evidence_replay_title", sid=args.session, count=len(entries))]
    for e in entries:
        mark = "PASS" if e.get("ok") else "INFO"
        lines.append(f"- [{e.get('at', '')[:16]}] {mark} {e.get('kind', '')} "
                     f"[{e.get('tool', '')}]: {e.get('summary', '')}"
                     + (f"  ({e['artifact']})" if e.get("artifact") else ""))
    report = "\n".join(lines)
    print(report)
    if getattr(args, "export", None):
        out = Path(args.export)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(t("evidence_export_header", sid=args.session) + "\n\n"
                       + report + "\n", encoding="utf-8")
        print(t("evidence_exported", path=out))
    return 0


def cmd_doctor() -> int:
    checks: list[tuple[bool, str, str]] = []

    def add(ok: bool, name: str, detail: str = "") -> None:
        checks.append((ok, name, detail))

    # Interactive doctor opens with the brand wordmark (piped use — CI logs,
    # `oaset doctor | grep` — stays plain so scraping never parses art).
    if sys.stdout.isatty():
        try:
            from rich.console import Console as _Console

            from oaset.tui import brand as _brand

            console = _Console()
            if _brand.truecolor_supported():
                console.print(_brand.wordmark())
            console.print(f"[b]oAset CLI v{__version__} doctor[/b]\n")
        except Exception:
            pass

    add(sys.version_info >= (3, 11), "python>=3.11", sys.version.split()[0])
    try:
        from oaset.local_servers import discover_local_servers_sync

        local = discover_local_servers_sync()
        add(True, "local-servers",
            ", ".join(f"{s['vendor']}({len(s['models'])} models)" for s in local)
            or "none detected")
    except Exception:
        add(True, "local-servers", "probe failed")
    try:
        import textual

        add(True, "textual", textual.__version__)
    except Exception as exc:  # pragma: no cover
        add(False, "textual", str(exc))
    try:
        import openai

        add(True, "openai-sdk", openai.__version__)
    except Exception as exc:  # pragma: no cover
        add(False, "openai-sdk", str(exc))

    # A fresh checkout/download has no config yet: doctor is the entry point
    # people run first, so it CREATES the default config instead of failing a
    # check that only means "never launched yet" (fresh installs used to see
    # two critical failures before doing anything wrong).
    path = config_path()
    existed = path.exists()
    cfg = load_config(create=True)
    add(True, "config-file",
        str(path) + ("" if existed else t("doctor_config_created")))

    from oaset.network import EGRESS_INVENTORY, NetworkPolicy

    policy = NetworkPolicy(cfg.network_mode)

    try:
        model_cfg = resolve_model(cfg)
        provider_cfg = cfg.providers.get(model_cfg.provider)
        base = provider_cfg.base_url if provider_cfg else ""
        key_value = resolve_api_key(provider_cfg.api_key) if provider_cfg else ""
        env_based = bool(provider_cfg and provider_cfg.api_key.startswith("env:"))
        key_ok = bool(key_value) and key_value != "EMPTY"
        add(key_ok or not env_based, "api-key",
            f"{model_cfg.provider}: " + ("set" if key_ok else "NOT SET")
            + ("" if key_ok else "  ->  " + t("doctor_key_hint")))
        reachable = False
        detail = base
        if base.startswith("http"):
            from oaset.network import is_loopback_url

            if not policy.allows("model_call") and not is_loopback_url(base):
                # local_only still allows loopback (Ollama / LM Studio)
                detail = f"{base} (skipped: network.mode='{policy.mode}')"
                add(True, "endpoint", detail)
            else:
                import httpx

                try:
                    resp = httpx.head(base, timeout=4, follow_redirects=True)
                    reachable = resp.status_code < 500
                    detail = f"{base} (HTTP {resp.status_code})"
                except Exception as exc:
                    detail = f"{base} ({type(exc).__name__})"
        add(reachable, "endpoint", detail)
        default_model = model_cfg.id
    except KeyError as exc:
        add(False, "default-model", str(exc))
        default_model = "?"

    add(shutil.which("rg") is not None, "ripgrep", shutil.which("rg") or "fallback to python grep")
    shell = shutil.which("bash")
    add(shell is not None or os_name_is_windows(), "shell", shell or "cmd/powershell")

    # sandbox posture: ON is the safe default; OFF is a deliberate opt-out worth flagging
    sandbox_on = bool(getattr(cfg, "shell_sandbox_default", False))
    add(sandbox_on, "run-shell-sandbox",
        ("on (job-object + restricted-token fs guard)" if os.name == "nt"
         else "on (rlimit + bwrap/seatbelt when present)") if sandbox_on
        else "OFF — enable [shell] sandbox_default = true in config.toml")

    try:
        from oaset.capabilities import native_web_search_supported

        native_on = (cfg.search.native != "off"
                     and native_web_search_supported(model_cfg.provider))
        from oaset.capabilities import capability_report
        from oaset.tools.search import default_engines

        report = capability_report(model_cfg.provider)
        native_line = " · ".join(report) if (native_on and report) else "off"
        print(f" search: engines={','.join(default_engines())}"
              f" · native: {native_line}")
    except Exception:
        pass
    print(f"oAset CLI v{__version__} doctor — default model: {default_model}")
    wf = "on" if "web_fetch" not in cfg.disabled_tools else "off"
    print(
        f" network: {policy.mode} — " + " | ".join(
            f"{kind}:{'on' if policy.allows(kind) else 'off'}" for kind in EGRESS_INVENTORY
        ) + f" | web_fetch:{wf}"
    )
    failed = 0
    for ok, name, detail in checks:
        glyph = "✔" if ok else "✗"
        if not ok:
            failed += 1
        print(f" {glyph} {name:<12} {detail}")
    warn_only = {"ripgrep", "endpoint", "shell", "run-shell-sandbox"}
    hard_failures = [c for c in checks if not c[0] and c[1] not in warn_only]
    if hard_failures:
        print(f"\n{len(hard_failures)} critical check(s) failed.")
        return 1
    print("\nReady. (endpoint/ripgrep warnings are non-fatal; grep has a Python fallback)")
    return 0


def os_name_is_windows() -> bool:
    return sys.platform == "win32"


if __name__ == "__main__":
    raise SystemExit(main())
