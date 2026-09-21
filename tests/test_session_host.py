"""SessionHost is the public runtime every surface (TUI, SDK, desktop) drives."""

from __future__ import annotations

from pathlib import Path

from oaset.config import default_config
from oaset.host import SessionHost
from oaset.network import NetworkBlockedError, NetworkPolicy, is_loopback_url
from oaset.providers import MockProvider, MockTurn
from oaset.session import SessionStore


async def test_host_chat_persists_jsonl(isolated_home, workspace):
    cfg = default_config()
    provider = MockProvider([MockTurn(content_chunks=["from host"])])
    async with SessionHost(
        cfg, workspace, provider=provider, model_id="mock/mock-echo",
        persist=True, mcp=False, tools=False,
    ) as host:
        answer = await host.ask("hello")
        assert answer == "from host"
        snap = host.snapshot()
        assert snap["cwd"] == str(workspace)
        assert snap["message_count"] >= 2
        loaded = host.store.load(host.session.meta.session_id)
        assert loaded is not None
        texts = [m.content for m in loaded.conversation.messages]
        assert "hello" in texts
        assert "from host" in texts


async def test_host_ephemeral_does_not_write_sessions(isolated_home, workspace):
    cfg = default_config()
    provider = MockProvider([MockTurn(content_chunks=["tmp"])])
    async with SessionHost(
        cfg, workspace, provider=provider, model_id="mock/mock-echo",
        persist=False, mcp=False, tools=False,
    ) as host:
        await host.ask("ephemeral")
        assert host.session.file is None
        assert SessionStore().list_sessions(cwd=str(workspace)) == []


def test_loopback_urls_are_local():
    assert is_loopback_url("http://127.0.0.1:11434/v1")
    assert is_loopback_url("http://localhost:1234/v1")
    assert is_loopback_url("mock://offline")
    assert not is_loopback_url("https://api.deepseek.com/v1")


def test_local_only_allows_loopback_model_calls():
    policy = NetworkPolicy("local_only")
    policy.assert_allowed("model_call", endpoint="http://127.0.0.1:11434/v1")
    try:
        policy.assert_allowed("model_call", endpoint="https://api.deepseek.com/v1")
        raise AssertionError("remote model_call must stay blocked")
    except NetworkBlockedError:
        pass
    try:
        policy.assert_allowed("pull")
        raise AssertionError("web_fetch must stay blocked under local_only")
    except NetworkBlockedError:
        pass


def test_compact_marker_roundtrips_on_load(isolated_home):
    store = SessionStore()
    session = store.new_session(Path("C:/proj"), model="mock/mock-echo")
    from oaset.agent.messages import Message

    for i in range(6):
        session.conversation.append(Message(role="user", content=f"u{i}"))
        store.append_message(session, session.conversation.messages[-1])
        session.conversation.append(Message(role="assistant", content=f"a{i}"))
        store.append_message(session, session.conversation.messages[-1])
    store.append_compaction(session, "older turns summarized", dropped=8)
    loaded = store.load(session.meta.session_id)
    assert loaded is not None
    first = loaded.conversation.messages[0].content
    assert "older turns summarized" in first
    assert first.startswith("[conversation summary")


async def test_headless_host_checkpoints_writes_before_they_happen(tmp_path, isolated_home):
    """Only the TUI installed a checkpoint sink, so outside it every WRITE
    tool's declared revertibility was fiction — /undo had nothing to restore."""
    from oaset.config import default_config
    from oaset.host import SessionHost
    from oaset.providers import MockProvider, MockTurn
    from oaset.tools import ToolRegistry

    cfg = default_config()
    cfg.default_model = "mock/mock-echo"
    provider = MockProvider([MockTurn(content_chunks=["ok"])])
    host = SessionHost(config=cfg, cwd=tmp_path, provider=provider,
                       model_id="mock/mock-echo", tools=True, persist=True)
    assert isinstance(host.registry, ToolRegistry)
    sink = host.registry.ctx.session_state.get("checkpoint_sink")
    assert callable(sink), "the host must provide a default checkpoint sink"

    target = tmp_path / "before.txt"
    target.write_text("old content", encoding="utf-8")
    sink({str(target): "old content"})

    checkpoints = host.store.list_checkpoints(host.session)
    assert checkpoints and checkpoints[0]["count"] == 1
    restored = checkpoints[0]["files"][0]
    assert restored == str(target)


async def test_same_session_turns_are_mutex_across_host_instances(tmp_path, isolated_home):
    """The panel and a TUI in another terminal are different PROCESSES driving
    the same session; the per-process busy check cannot see that. The sidecar
    `.turn` lock makes the second surface get SessionBusyError instead of two
    turns interleaving into one transcript."""
    import asyncio

    from oaset.config import default_config
    from oaset.errors import SessionBusyError
    from oaset.host import SessionHost
    from oaset.providers import MockProvider, MockTurn

    cfg = default_config()
    cfg.default_model = "mock/mock-echo"

    release = asyncio.Event()

    class Gate:
        """Provider that holds its turn open until the test releases it."""

        async def stream(self, messages, tools=None, **kwargs):
            await release.wait()
            from oaset.providers.base import ContentDelta, StreamDone

            yield ContentDelta("slow turn")
            yield StreamDone(finish_reason="stop", usage=None)

        async def aclose(self):
            pass

    # the slow provider must be wired at CONSTRUCTION: the kernel captures it
    host_a = SessionHost(
        config=cfg, cwd=tmp_path, provider=Gate(),
        model_id="mock/mock-echo", tools=True, persist=True,
    )
    sid = host_a.session.meta.session_id
    from oaset.session import SessionStore

    host_b = SessionHost(
        config=cfg, cwd=tmp_path,
        provider=MockProvider([MockTurn(content_chunks=["b"])]),
        model_id="mock/mock-echo", tools=True, persist=True,
    )
    # simulate a second PROCESS: a separately loaded transcript object that
    # points at the same session file
    same = SessionStore(home=host_a.store.home).load(sid)
    host_b.attach_session(same)

    task_a = asyncio.ensure_future(_drain(host_a.chat("slow turn")))
    await asyncio.sleep(0.15)
    try:
        async for _ in host_b.chat("parallel turn"):
            pass
        raised = False
    except SessionBusyError:
        raised = True
    finally:
        release.set()
    await task_a
    assert raised, "the second surface must be refused while a turn runs"


async def _drain(gen):
    async for _ in gen:
        pass
