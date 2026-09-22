import json

import pytest

from oaset.tools import AutoGate, ReadOnlyGate, ToolContext, ToolRegistry
from oaset.tools.fs import (
    EditFileTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from oaset.tools.shell import RunShellTool
from oaset.tools.todo import TodoWriteTool


def make_ctx(tmp_path, mode="default"):
    return ToolContext(cwd=tmp_path, mode=mode, output_limit=500)


async def test_read_write_edit_roundtrip(tmp_path):
    ctx = make_ctx(tmp_path)
    target = tmp_path / "src" / "app.py"
    res = await WriteFileTool().run({"path": "src/app.py", "content": "a = 1\nb = 2\n"}, ctx)
    assert not res.is_error and target.exists()
    res = await ReadFileTool().run({"path": "src/app.py"}, ctx)
    assert "1\ta = 1" in res.output
    res = await EditFileTool().run(
        {"path": "src/app.py", "old_text": "b = 2", "new_text": "b = 3"}, ctx
    )
    assert not res.is_error
    assert "b = 3" in target.read_text(encoding="utf-8")


async def test_edit_requires_unique_match(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "dup.txt").write_text("x\nx\n", encoding="utf-8")
    res = await EditFileTool().run({"path": "dup.txt", "old_text": "x", "new_text": "y"}, ctx)
    assert res.is_error and "2 locations" in res.output
    res = await EditFileTool().run(
        {"path": "dup.txt", "old_text": "x", "new_text": "y", "replace_all": True}, ctx
    )
    assert not res.is_error
    assert (tmp_path / "dup.txt").read_text(encoding="utf-8").replace("\r\n", "\n") == "y\ny\n"


async def test_read_missing_and_binary(tmp_path):
    ctx = make_ctx(tmp_path)
    res = await ReadFileTool().run({"path": "nope.txt"}, ctx)
    assert res.is_error
    (tmp_path / "bin.dat").write_bytes(b"ab\x00cd")
    res = await ReadFileTool().run({"path": "bin.dat"}, ctx)
    assert res.is_error and "Binary" in res.output


async def test_glob_and_grep(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("value = 42\n", encoding="utf-8")
    (tmp_path / "pkg" / "n.txt").write_text("value = 1\n", encoding="utf-8")
    res = await GlobTool().run({"pattern": "**/*.py"}, ctx)
    assert "m.py" in res.output
    res = await GrepTool().run({"pattern": r"value = \d+", "include": "*.py"}, ctx)
    assert not res.is_error
    assert "value = 42" in res.output
    res = await GrepTool().run({"pattern": "not-there-xyz"}, ctx)
    assert "No matches" in res.output


async def test_list_dir(tmp_path):
    ctx = make_ctx(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.txt").write_text("hi", encoding="utf-8")
    res = await ListDirTool().run({}, ctx)
    assert "sub/" in res.output and "f.txt" in res.output


@pytest.mark.skipif(not __import__("shutil").which("bash") and __import__("os").name != "posix",
                    reason="needs a POSIX-ish shell (Git Bash on Windows)")
async def test_shell_echo(tmp_path):
    ctx = make_ctx(tmp_path)
    res = await RunShellTool().run({"command": "echo hello-shell"}, ctx)
    assert not res.is_error
    assert "[exit 0]" in res.output and "hello-shell" in res.output


async def test_shell_sandbox_job_runs(tmp_path, host_shell_unavailable_reason,
                                       restricted_spawn_unavailable_reason):
    """sandbox=True: job object assigned, command still executes (Windows)."""
    if host_shell_unavailable_reason:
        pytest.skip(host_shell_unavailable_reason)
    if restricted_spawn_unavailable_reason:
        pytest.skip(restricted_spawn_unavailable_reason)
    ctx = ToolContext(cwd=tmp_path, mode="auto", output_limit=300)
    ctx.session_state["shell_config"] = {"backend": "local", "sandbox_default": True, "sandbox_memory_mb": 512}
    res = await RunShellTool().run({"command": "echo sandbox-live", "sandbox": True}, ctx)
    assert not res.is_error and "sandbox-live" in res.output


async def test_shell_timeout_kills(tmp_path, host_shell_unavailable_reason):
    if host_shell_unavailable_reason:
        pytest.skip(host_shell_unavailable_reason)
    ctx = make_ctx(tmp_path)
    res = await RunShellTool().run({"command": "sleep 5", "timeout": 1}, ctx)
    assert "[exit -1]" in res.output and "timeout" in res.output


async def test_todo_write(tmp_path):
    ctx = make_ctx(tmp_path)
    res = await TodoWriteTool().run(
        {"todos": [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "bogus"}]}, ctx
    )
    assert not res.is_error
    assert ctx.session_state["todos"][0]["status"] == "in_progress"
    assert ctx.session_state["todos"][1]["status"] == "pending"


def test_registry_schemas_are_cached_until_tools_change():
    registry = ToolRegistry(gate=AutoGate())
    first = registry.schemas()
    second = registry.schemas()
    assert first is second
    registry.add_tool(TodoWriteTool())
    third = registry.schemas()
    assert third is not first
    assert any(s["function"]["name"] == "todo_write" for s in third)


async def test_registry_denies_exec_in_readonly_gate(tmp_path):
    registry = ToolRegistry(gate=ReadOnlyGate())
    registry.bind(make_ctx(tmp_path))
    res = await registry.dispatch("run_shell", json.dumps({"command": "echo hi"}))
    assert res.is_error and "denied" in res.output or "拒绝" in res.output


async def test_registry_blocks_writes_in_plan_mode(tmp_path):
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(make_ctx(tmp_path, mode="plan"))
    res = await registry.dispatch("write_file", json.dumps({"path": "x.txt", "content": "y"}))
    assert res.is_error and "plan mode" in res.output.lower()


async def test_registry_unknown_tool_and_bad_json(tmp_path):
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(make_ctx(tmp_path))
    assert (await registry.dispatch("nope", "{}")).is_error
    assert (await registry.dispatch("read_file", "{bad json")).is_error


async def test_registry_session_allow_skips_gate(tmp_path):
    class CountingGate(AutoGate):
        calls = 0

        async def request(self, tool_name, level, summary, preview=None):
            CountingGate.calls += 1
            return "always"

    registry = ToolRegistry(gate=CountingGate())
    ctx = make_ctx(tmp_path)
    registry.bind(ctx)
    payload = json.dumps({"path": "a.txt", "content": "1"})
    await registry.dispatch("write_file", payload)
    await registry.dispatch("write_file", payload)
    assert CountingGate.calls == 1  # second call short-circuits via session_allowed
    assert "write_file" in ctx.session_allowed


def _outside_dir(tmp_path):
    outside = tmp_path.parent / f"oaset-fence-{tmp_path.name[:8]}"
    outside.mkdir(exist_ok=True)
    (outside / "note.txt").write_text("outside", encoding="utf-8")
    return outside


async def test_read_inside_workspace_never_prompts(tmp_path):
    class ExplodingGate(AutoGate):
        async def request(self, *a, **k):
            raise AssertionError("gate must not fire for in-workspace reads")

    registry = ToolRegistry(gate=ExplodingGate())
    registry.bind(make_ctx(tmp_path))
    (tmp_path / "ok.txt").write_text("hi", encoding="utf-8")
    for tool, args in [
        ("read_file", {"path": "ok.txt"}),
        ("glob", {"pattern": "*.txt"}),
        ("grep", {"pattern": "hi"}),
        ("list_dir", {}),
    ]:
        res = await registry.dispatch(tool, json.dumps(args))
        assert not res.is_error, f"{tool}: {res.output}"


async def test_read_outside_workspace_prompts_and_remembers_dir(tmp_path):
    outside = _outside_dir(tmp_path)
    try:
        class RecordingGate(AutoGate):
            calls = 0

            async def request(self, tool_name, level, summary, preview=None):
                RecordingGate.calls += 1
                assert level == "read"
                assert "OUTSIDE" in summary
                return "always"

        registry = ToolRegistry(gate=RecordingGate())
        ctx = make_ctx(tmp_path)
        registry.bind(ctx)
        res = await registry.dispatch("read_file", json.dumps({"path": str(outside / "note.txt")}))
        assert not res.is_error
        assert RecordingGate.calls == 1
        assert str(outside.resolve()) in ctx.session_paths
        # second read from the same directory: no new prompt
        res = await registry.dispatch("read_file", json.dumps({"path": str(outside / "note.txt")}))
        assert not res.is_error
        assert RecordingGate.calls == 1
        # a different READ tool in the same approved directory also skips the gate
        res = await registry.dispatch("list_dir", json.dumps({"path": str(outside)}))
        assert not res.is_error
        assert RecordingGate.calls == 1
    finally:
        import shutil as _shutil

        _shutil.rmtree(outside, ignore_errors=True)


async def test_read_outside_workspace_denied(tmp_path):
    outside = _outside_dir(tmp_path)
    try:
        class DenyGate(AutoGate):
            async def request(self, tool_name, level, summary, preview=None):
                return "deny"

        registry = ToolRegistry(gate=DenyGate())
        registry.bind(make_ctx(tmp_path))
        res = await registry.dispatch("read_file", json.dumps({"path": str(outside / "note.txt")}))
        assert res.is_error and "denied" in res.output or "拒绝" in res.output
        # ReadOnlyGate (one-shot -p mode) denies outside reads too
        registry2 = ToolRegistry(gate=ReadOnlyGate())
        registry2.bind(make_ctx(tmp_path))
        res = await registry2.dispatch("glob", json.dumps({"pattern": "*.txt", "path": str(outside)}))
        assert res.is_error and "denied" in res.output or "拒绝" in res.output
    finally:
        import shutil as _shutil

        _shutil.rmtree(outside, ignore_errors=True)


async def test_read_outside_skipped_in_auto_mode(tmp_path):
    outside = _outside_dir(tmp_path)
    try:
        class ExplodingGate(AutoGate):
            async def request(self, *a, **k):
                raise AssertionError("auto mode must not prompt")

        registry = ToolRegistry(gate=ExplodingGate())
        registry.bind(make_ctx(tmp_path, mode="auto"))
        res = await registry.dispatch("read_file", json.dumps({"path": str(outside / "note.txt")}))
        assert not res.is_error
    finally:
        import shutil as _shutil

        _shutil.rmtree(outside, ignore_errors=True)

async def test_registry_missing_required_args_clean_error(tmp_path):
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(make_ctx(tmp_path))
    res = await registry.dispatch("read_file", json.dumps({}))
    assert res.is_error
    assert "Missing required argument(s) path" in res.output
    # outside-workspace gating must not crash on missing args either
    res2 = await registry.dispatch("read_file", "{}")
    assert res2.is_error and "KeyError" not in res2.output



async def test_always_is_session_scoped_and_durable_grant_is_opt_in(tmp_path):
    """A7 regression: 'always' grants at least the session; only opt-in tools
    (workspace-fenced writes, normal shell) may persist a durable grant."""

    class MemoryToolLike:
        """A WRITE tool that does NOT opt in to durable grants (e.g. memory)."""
        name = "global_writer"
        description = "writes global state"
        permission = "write"
        required: list[str] = []
        parameters: dict = {}

        def schema(self):
            return {"type": "function", "function": {"name": self.name,
                    "description": self.description, "parameters": {"type": "object", "properties": {}}}}

        def subject(self, args, ctx):
            return ""

        def gate_summary(self, args, ctx):
            return "write global state"

        def in_fence(self, args, ctx):
            return True

        def preview(self, args, ctx):
            return ""

        def permanent_allow(self, args, ctx) -> bool:
            return False  # global-state writes must not persist an "always"

        async def run(self, args, ctx):
            from oaset.tools.base import ToolResult
            return ToolResult("ok")

    persisted: list[str] = []

    class AlwaysGate(AutoGate):
        async def request(self, tool_name, level, summary, preview=None):
            return "always"

    registry = ToolRegistry(gate=AlwaysGate())
    registry.add_tool(MemoryToolLike())
    ctx = make_ctx(tmp_path)
    registry.bind(ctx)

    def fake_persist(name):
        persisted.append(name)
    ctx.persist_allow = fake_persist

    result = await registry.dispatch("global_writer", "{}")
    assert not result.is_error
    assert "global_writer" in ctx.session_allowed   # at least session-scoped
    assert persisted == []                          # but NOT persisted (no opt-in)

    # write_file opts in → persisted
    res = await registry.dispatch("write_file", json.dumps({"path": "x.txt", "content": "1"}))
    assert not res.is_error
    assert persisted == ["write_file"]


def test_workspace_allow_tools_binding(tmp_path):
    """Durable grants are bound to the approving workspace; legacy bare names
    keep working globally (documented back-compat)."""
    from oaset.tools import workspace_allow_tools

    here = tmp_path / "this-project"
    entries = [
        "write_file",                        # legacy global grant
        f"edit_file::{workdir_bucket(here)}",  # bound to this workspace
        f"shell::{workdir_bucket(tmp_path / 'other-project')}",  # bound elsewhere
    ]
    allowed = workspace_allow_tools(entries, here)
    assert allowed == {"write_file", "edit_file"}


from oaset.utils import workdir_bucket  # noqa: E402  (used by the test above)

# ------------------------------------------------ bounded capture (OOM guard)

async def test_run_shell_output_capture_is_bounded(tmp_path, monkeypatch):
    """A chatty command used to be fully accumulated (list of chunks + join +
    decode ≈ 3x its size) before being truncated to MAX_CAPTURE — 300MB of
    output measured ~900MB of heap. Head and tail are all the model can see,
    so head and tail are all we may hold."""
    import asyncio as _asyncio

    from oaset.tools.shell import MAX_CAPTURE, RunShellTool

    payload = (b"x" * 1024 + b"\n") * 400          # ~400KB in 4KB chunks

    class FakeStdout:
        def __init__(self, data):
            self._data = data
            self._pos = 0

        async def read(self, n):
            chunk = self._data[self._pos:self._pos + n]
            self._pos += len(chunk)
            await _asyncio.sleep(0)
            return chunk

    class FakeProc:
        def __init__(self, data):
            self.stdout = FakeStdout(data)
            self.returncode = None
            self.pid = 0

        async def wait(self):
            self.returncode = 0
            return 0

    captured_proc: list[FakeProc] = []

    async def fake_exec(*argv, **kwargs):
        proc = FakeProc(payload)
        captured_proc.append(proc)
        return proc

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_exec)

    ctx = ToolContext(cwd=tmp_path, mode="auto", output_limit=MAX_CAPTURE)
    result = await RunShellTool().run({"command": "produce-big-output"}, ctx)
    assert not result.is_error
    assert len(payload) > MAX_CAPTURE, "the fixture must exceed the budget"
    assert len(result.output) < MAX_CAPTURE * 2, (
        "the tool must not materialise the whole stream")
    assert "elided" in result.output
    assert result.output.count("x") < len(payload), "the middle must be dropped, not kept"
    assert result.exit_code == 0
