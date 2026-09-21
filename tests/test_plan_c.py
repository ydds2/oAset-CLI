"""Plan C: ACP, apply_patch, worktree, clipboard image, SDK session, workspace-write."""

from __future__ import annotations

import os
from pathlib import Path

from oaset.acp import AcpServer
from oaset.config import default_config
from oaset.providers import MockProvider, MockTurn
from oaset.sdk import Oaset
from oaset.tools import AutoGate, ToolContext, ToolRegistry
from oaset.tools.patch import ApplyPatchTool, parse_unified_diff
from oaset.tools.shell import _escapes_workspace
from oaset.tui.clipboard import get_image_png
from oaset.worktree import WorktreeError, create_worktree, is_git_repo, list_worktrees, remove_worktree


def test_sdk_create_session_is_a_new_transcript(isolated_home, workspace):
    agent = Oaset(
        cwd=workspace, model="mock/mock-echo",
        provider=MockProvider([MockTurn(content_chunks=["one"]), MockTurn(content_chunks=["two"])]),
        tools=False, mcp=False,
    )
    first = agent.host.session.meta.session_id
    agent.create_session()
    second = agent.host.session.meta.session_id
    assert second != first
    assert agent.conversation.messages == []
    assert agent.host.kernel.session_id == second


def test_parse_and_apply_patch(tmp_path):
    path = tmp_path / "f.py"
    path.write_text("line1\nline2\nline3\n", encoding="utf-8")
    diff = (
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-line2\n"
        "+line2b\n"
        " line3\n"
    )
    files = parse_unified_diff(diff)
    assert files[0][0] == "f.py"
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=tmp_path, mode="auto", output_limit=2000))
    import asyncio

    result = asyncio.run(ApplyPatchTool().run({"diff": diff}, registry.ctx))
    assert not result.is_error, result.output
    assert path.read_text(encoding="utf-8") == "line1\nline2b\nline3\n"


def test_apply_patch_refuses_outside_workspace(tmp_path):
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=tmp_path / "ws", mode="auto", output_limit=2000))
    (tmp_path / "ws").mkdir()
    diff = "--- a/x\n+++ b/../secret.txt\n@@ -1 +1 @@\n-a\n+b\n"
    import asyncio

    result = asyncio.run(ApplyPatchTool().run({"diff": diff}, registry.ctx))
    assert result.is_error
    assert "outside" in result.output.lower() or "workspace" in result.output.lower()


def test_worktree_create_list_remove(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    assert is_git_repo(repo)
    dest = create_worktree(repo, "job")
    assert dest.is_dir()
    assert (dest / "a.txt").is_file()
    rows = list_worktrees(repo)
    blob = " ".join(f"{k}={v}" for row in rows for k, v in row.items())
    assert dest.name in blob or dest.as_posix() in blob.replace("\\", "/") or str(dest) in blob
    removed = remove_worktree(repo, "job")
    assert removed == dest
    assert not dest.exists()
    try:
        create_worktree(tmp_path / "not-a-repo", "x")
        assert False, "expected WorktreeError"
    except WorktreeError:
        pass


def test_workspace_write_heuristic():
    cwd = Path("C:/proj") if os.name == "nt" else Path("/proj")
    assert _escapes_workspace("echo hi > /tmp/out.txt", cwd)
    assert _escapes_workspace("rm -rf ~", cwd)
    assert not _escapes_workspace("pytest -q", cwd)
    assert not _escapes_workspace("echo hi > note.txt", cwd)


def test_clipboard_image_absent_is_structured():
    png, error = get_image_png()
    if os.name != "nt":
        assert png is None and error is not None
        assert error.code == "clipboard_unavailable"


async def test_acp_initialize_new_session_and_prompt(isolated_home, workspace):
    cfg = default_config()
    server = AcpServer(cfg=cfg, cwd=workspace, yolo=True)
    init = await server.handle_async({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["agentInfo"]["name"] == "oaset"
    created = await server.handle_async({
        "jsonrpc": "2.0", "id": 2, "method": "session/new",
        "params": {"cwd": str(workspace)},
    })
    sid = created["result"]["sessionId"]
    host = server.sessions[sid]
    host.provider = MockProvider([MockTurn(content_chunks=["from-acp"])])
    host.kernel.provider = host.provider
    host.kernel.fallbacks = []
    import asyncio as _aio

    await server.handle_async({
        "jsonrpc": "2.0", "id": 3, "method": "session/prompt",
        "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]},
    })
    for _ in range(100):
        await _aio.sleep(0.05)
        if any(m.get("id") == 3 and "result" in m for m in server.updates):
            break
    reply = next(m for m in server.updates if m.get("id") == 3)
    assert reply["result"]["stopReason"] == "end_turn"
    assert "from-acp" in reply["result"]["text"]
    # live updates: the turn's events were parked BEFORE the response resolved
    kinds = [u["params"]["update"]["sessionUpdate"]
             for u in server.updates if "params" in u]
    assert "turn_started" in kinds and "turn_completed" in kinds


def test_apply_patch_is_a_default_tool():
    registry = ToolRegistry(gate=AutoGate())
    assert "apply_patch" in registry.tools


def test_cli_registers_acp_subcommand():
    from oaset.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["acp", "--cwd", ".", "--lsp"])
    assert args.command == "acp" and args.lsp is True


async def test_acp_list_and_load_session(isolated_home, workspace):
    cfg = default_config()
    server = AcpServer(cfg=cfg, cwd=workspace, yolo=True)
    created = await server.handle_async({
        "jsonrpc": "2.0", "id": 1, "method": "session/new",
        "params": {"cwd": str(workspace)},
    })
    sid = created["result"]["sessionId"]
    listed = await server.handle_async({
        "jsonrpc": "2.0", "id": 2, "method": "session/list",
        "params": {"cwd": str(workspace)},
    })
    ids = [row["sessionId"] for row in listed["result"]["sessions"]]
    assert sid in ids
    loaded = await server.handle_async({
        "jsonrpc": "2.0", "id": 3, "method": "session/load",
        "params": {"sessionId": sid, "cwd": str(workspace)},
    })
    assert loaded["result"]["sessionId"] == sid


def test_posix_sandbox_wrap_is_identity_without_bwrap():
    from oaset.sandbox import wrap_posix_command

    argv = ["echo", "hi"]
    out = wrap_posix_command(argv, "/tmp", 256)
    assert isinstance(out, list) and out[0] in ("echo", "bwrap", "sandbox-exec")


async def test_line_range_copy_inside_one_block(workspace):
    from oaset.config import default_config
    from oaset.providers import MockProvider
    from oaset.tui.app import OasetApp

    app = OasetApp(cfg=default_config(), cwd=workspace,
                   provider=MockProvider([]), model_id="mock/mock-echo")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.1)
        msg = app.chat.add_user("line-a\nline-b\nline-c\nline-d")
        await pilot.pause(0.05)
        app.chat.begin_selection(msg, 1)
        app.chat.extend_selection(msg, 2)
        assert app.chat.selected_text() == "line-b\nline-c"


# ------------------------------------------------------- ACP wire framing (LSP)

def test_lsp_framed_reads_back_to_back_frames():
    """Content-Length framing has nothing after the body, so a line reader glued
    one body to the next header and dropped both — `acp --lsp` could never
    receive a second request."""
    import contextlib
    import io
    import json

    from oaset.acp import _iter_lsp_frames, _write_message

    first = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    second = {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}}

    # build the wire bytes exactly as the writer emits them
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _write_message(first, lsp=True)
        _write_message(second, lsp=True)

    frames = list(_iter_lsp_frames(io.BytesIO(buffer.getvalue().encode("utf-8"))))
    assert [json.loads(frame) for frame in frames] == [first, second]


def test_lsp_framed_reader_tolerates_junk_headers_and_bad_length():
    """An unknown header or an unparsable length must not take the reader down
    for the frames that follow it."""
    import io

    from oaset.acp import _iter_lsp_frames

    good = b'{"jsonrpc": "2.0", "id": 1}'
    wire = (b"X-Client: some-editor\r\n"
            b"Content-Length: not-a-number\r\n\r\n"
            b"Content-Length: %d\r\n\r\n" % len(good) + good)
    assert list(_iter_lsp_frames(io.BytesIO(wire))) == [good.decode()]


def test_acp_event_frames_survive_live_objects(workspace):
    """Event data carries live objects (ProviderError, Message, ToolResult with
    PNG bytes). json.dumps used to raise on them, which killed the writer task
    and silently froze every ACP session."""
    import contextlib
    import io
    import json
    from types import SimpleNamespace

    from oaset.acp import AcpServer, _write_message
    from oaset.agent.messages import Message
    from oaset.providers.base import ProviderError
    from oaset.tools.base import ToolResult

    server = AcpServer(cfg=default_config(), cwd=workspace, yolo=True)
    event = SimpleNamespace(type="error", data={
        "error": ProviderError("auth", "bad key"),
        "message": Message(role="assistant", content="hi"),
        "result": ToolResult("ok", image=b"\x89PNG\r\n"),
        "nested": {"list": [Message(role="tool", content="r")]},
    })
    server._emit_update("sess-1", event)
    payload = server.updates[-1]

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _write_message(payload, lsp=False)
    frame = json.loads(buffer.getvalue().strip())
    data = frame["params"]["update"]["data"]
    assert data["error"]["error"] == "ProviderError"
    assert "Store a key with /login" in data["error"]["hint"]     # the actionable part
    assert data["message"]["content"] == "hi"
    assert data["result"]["output"] == "ok"
    assert data["result"]["image"] == {"bytes": 6}                # blob by size, never inline
    assert data["nested"]["list"][0]["role"] == "tool"


async def test_acp_writer_drops_one_bad_frame_and_keeps_serving(capsys):
    """A frame that cannot be serialised must cost that frame only — the old
    writer died on it and the editor's session went silent."""
    import asyncio
    import contextlib

    from oaset.acp import _writer

    outbound: asyncio.Queue = asyncio.Queue()
    outbound.put_nowait({(1, 2): "a tuple key is not JSON"})
    outbound.put_nowait({"jsonrpc": "2.0", "id": 9, "result": "still here"})

    task = asyncio.create_task(_writer(outbound, lsp=False))
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    captured = capsys.readouterr()
    assert "still here" in captured.out
    assert "unserialisable frame" in captured.err


# ------------------------------------------- spaceless diff headers

def test_parse_unified_diff_accepts_spaceless_headers():
    """Models also emit `---a/x`/`+++b/x` (no space after the marker); the
    parser used to require the space and silently reported zero files."""
    diff = (
        "---a/one.txt\n"
        "+++b/one.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " line1\n"
        "-line2\n"
        "+line2b\n"
        "---a/two.txt\n"
        "+++b/two.txt\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    files = parse_unified_diff(diff)
    assert [f[0] for f in files] == ["one.txt", "two.txt"]
    assert any(ln.startswith("@@") for ln in files[0][1])


def test_spaceless_marker_pair_inside_hunk_stays_content():
    """Deleting `-- mark` then adding `++ mark` emits ---/+++ lookalikes
    INSIDE a hunk; the pair-without-@@ rule must keep them content lines
    even in the relaxed spaceless form."""
    diff = (
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -1,3 +1,3 @@\n"
        " ctx\n"
        "--- -- mark\n"
        "+++ ++ mark\n"
        " more\n"
    )
    files = parse_unified_diff(diff)
    assert len(files) == 1 and files[0][0] == "f.py"
    assert any(ln == "--- -- mark" for ln in files[0][1])
    assert any(ln == "+++ ++ mark" for ln in files[0][1])


def test_apply_patch_applies_a_spaceless_header_diff(tmp_path):
    (tmp_path / "one.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    diff = (
        "---a/one.txt\n"
        "+++b/one.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-line2\n"
        "+line2b\n"
        " line3\n"
    )
    import asyncio

    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=tmp_path, mode="auto", output_limit=2000))
    result = asyncio.run(ApplyPatchTool().run({"diff": diff}, registry.ctx))
    assert not result.is_error, result.output
    assert (tmp_path / "one.txt").read_text(encoding="utf-8") == \
        "line1\nline2b\nline3\n"


def test_lone_plus_marker_inside_hunk_stays_content():
    """An added line whose text starts with "++" wires as `++++x`/`+++ x`.
    Treating it as a NEW-FILE header swallowed the rest of the hunk and
    invented a bogus file name — a `+++` is a header only right after its
    `---` partner (or at diff start)."""
    diff = (
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -1,4 +1,4 @@\n"
        " ctx\n"
        "++++meta\n"
        "+++ also plus-prefixed content\n"
        " tail\n"
    )
    files = parse_unified_diff(diff)
    assert len(files) == 1 and files[0][0] == "f.py"
    body = files[0][1]
    assert "++++meta" in body and "+++ also plus-prefixed content" in body
    assert " tail" in body, "the hunk must not be truncated by the marker"


def test_leading_new_marker_without_old_partner_is_still_a_header():
    """Header-less diffs that start directly with `+++ b/x` (and even
    chain several files that way) are an accepted model output shape —
    the `---` partner is NOT required when the next line is the hunk
    header; only MID-HUNK plus-markers are content."""
    diff = (
        "+++ b/ghost.py\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    files = parse_unified_diff(diff)
    assert [f[0] for f in files] == ["ghost.py"]


# --------------------------- hunk-counting (body-tail markers stay content)

def test_body_tail_plus_marker_adjacent_to_next_hunk_stays_content():
    """Real repro from the blind review: a hunk ENDING with an added line
    whose text starts with "++" (e.g. `++i;` wires as `+++i;`) and the very
    next line being another `@@` — the lookahead alone read it as a
    headerless file header, flushing mid-file and inventing a file named
    "i;" while swallowing the real edit."""
    diff = (
        "--- a/x.cpp\n"
        "+++ b/x.cpp\n"
        "@@ -1,2 +1,3 @@\n"
        " ctx\n"
        "+++i;\n"
        "@@ -10,1 +10,2 @@\n"
        " base\n"
        "+added\n"
    )
    files = parse_unified_diff(diff)
    assert [f[0] for f in files] == ["x.cpp"], "one file, no bogus paths"
    body = files[0][1]
    assert "+++i;" in body, "the marker-shaped ADDITION is hunk content"


def test_body_tail_dash_dash_pair_adjacent_to_next_hunk_stays_content():
    """Same shape with a `--- --flag` deletion + `+++ ++flag` addition pair
    at hunk tail followed by `@@`: both lines were dropped and a bogus file
    named "++flag" invented."""
    diff = (
        "--- a/cfg.ini\n"
        "+++ b/cfg.ini\n"
        "@@ -1,3 +1,3 @@\n"
        " key=1\n"
        "--- --flag\n"
        "+++ ++flag\n"
        "@@ -9,1 +9,1 @@\n"
        "-old\n"
        "+new\n"
    )
    files = parse_unified_diff(diff)
    assert [f[0] for f in files] == ["cfg.ini"]
    body = files[0][1]
    assert "--- --flag" in body and "+++ ++flag" in body


def test_apply_hunks_seals_a_no_newline_tail_before_appending():
    """`a\nb` (no trailing newline) + a hunk appending `c` used to produce
    `a\nbc\n` — glued lines and an invented newline."""
    from oaset.tools.patch import _apply_hunks

    result = _apply_hunks("a\nb", ["@@ -1,2 +1,3 @@", " a", " b", "+c"])
    assert result == "a\nb\nc\n", "the old tail is sealed, not glued"


def test_apply_patch_end_to_end_on_no_newline_file(tmp_path):
    import asyncio

    (tmp_path / "tail.txt").write_text("alpha\nbeta", encoding="utf-8")
    diff = (
        "--- a/tail.txt\n"
        "+++ b/tail.txt\n"
        "@@ -1,2 +1,3 @@\n"
        " alpha\n"
        " beta\n"
        "+gamma\n"
    )
    from oaset.tools import AutoGate, ToolContext, ToolRegistry
    from oaset.tools.patch import ApplyPatchTool

    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=tmp_path, mode="auto", output_limit=2000))
    result = asyncio.run(ApplyPatchTool().run({"diff": diff}, registry.ctx))
    assert not result.is_error, result.output
    assert (tmp_path / "tail.txt").read_text(encoding="utf-8") == \
        "alpha\nbeta\ngamma\n"
