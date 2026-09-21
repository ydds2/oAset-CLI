"""Desktop-core readiness: the cross-process guarantees a desktop shell
relies on — live ACP updates, mid-turn cancel, permission requests over the
wire, image pass-through, and file locks under concurrency. Local-first
defaults are asserted, not just implemented (silence on approval = deny).
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from oaset.acp import AcpGate, AcpServer, _prompt_parts
from oaset.config import default_config
from oaset.utils import file_lock


def _server(workspace, yolo: bool = True) -> AcpServer:
    return AcpServer(cfg=default_config(), cwd=workspace, yolo=yolo)


def _slow_chat(host, events):
    """Replace host.chat with a generator that takes its time — the point at
    which the old buffered ACP delivered NOTHING until the turn finished."""
    async def gen(*args, **kwargs):
        for event, delay in events:
            await asyncio.sleep(delay)
            yield event
    return gen


class _FakeEvent:
    """Minimal Event stand-in: AcpServer only reads .type and .data."""

    def __init__(self, type: str, data: dict) -> None:
        self.type = type
        self.data = data


def _event(etype: str, **data) -> _FakeEvent:
    return _FakeEvent(etype, data)


async def test_acp_updates_arrive_before_the_turn_ends(workspace):
    server = _server(workspace)
    created = await server.handle_async({"jsonrpc": "2.0", "id": 1,
                                         "method": "session/new", "params": {}})
    sid = created["result"]["sessionId"]
    host = server.sessions[sid]
    host.chat = _slow_chat(host, [
        (_event("turn_started", model="mock"), 0.05),
        (_event("assistant_delta", text="hel"), 0.15),
        (_event("turn_completed", content="hello"), 0.05),
    ])

    returned = await server.handle_async({
        "jsonrpc": "2.0", "id": 2, "method": "session/prompt",
        "params": {"sessionId": sid, "prompt": "hi"}})
    assert returned is None  # the read loop is free while the turn runs
    # while the turn is still running, a delta has ALREADY been emitted and
    # the final response has NOT — both halves of the old bug, in one shot
    await asyncio.sleep(0.25)
    kinds = [u["params"]["update"]["sessionUpdate"]
             for u in server.updates if "params" in u]
    assert "assistant_delta" in kinds
    assert not any(m.get("id") == 2 and "result" in m for m in server.updates),         "response must wait for the turn"
    for _ in range(100):
        await asyncio.sleep(0.05)
        if any(m.get("id") == 2 and "result" in m for m in server.updates):
            break
    reply = next(m for m in server.updates if m.get("id") == 2)
    assert reply["result"]["stopReason"] == "end_turn"
    assert reply["result"]["text"] == "hello"


async def test_acp_cancel_notification_lands_mid_turn(workspace):
    server = _server(workspace)
    created = await server.handle_async({"jsonrpc": "2.0", "id": 1,
                                         "method": "session/new", "params": {}})
    sid = created["result"]["sessionId"]
    host = server.sessions[sid]
    host.chat = _slow_chat(host, [
        (_event("turn_started", model="mock"), 0.05),
        (_event("assistant_delta", text="par"), 5.0),   # far in the future
        (_event("turn_completed", content="done"), 0.0),
    ])
    # the serve loop must NOT block on the turn — handle_async returns at
    # once and the response is delivered asynchronously
    returned = await server.handle_async({
        "jsonrpc": "2.0", "id": 2, "method": "session/prompt",
        "params": {"sessionId": sid, "prompt": "hi"}})
    assert returned is None, "prompt must not block the read loop"
    await asyncio.sleep(0.2)
    # cancel is a NOTIFICATION: no id — the old router dropped it entirely
    await server.handle_async({"jsonrpc": "2.0", "method": "session/cancel",
                               "params": {"sessionId": sid}})
    for _ in range(100):
        await asyncio.sleep(0.05)
        if any(m.get("id") == 2 and "result" in m for m in server.updates):
            break
    reply = next(m for m in server.updates if m.get("id") == 2)
    assert reply["result"]["stopReason"] == "cancelled"


async def test_acp_permission_request_round_trip_and_defaults(workspace):
    server = _server(workspace, yolo=False)
    created = await server.handle_async({"jsonrpc": "2.0", "id": 1,
                                         "method": "session/new", "params": {}})
    sid = created["result"]["sessionId"]
    gate = AcpGate(server, sid)

    # allow_once over the wire
    ask = asyncio.create_task(gate.request("run_shell", "exec", "ls -la"))
    await asyncio.sleep(0.05)
    requests = [u for u in server.updates if u.get("method") == "session/request_permission"]
    assert requests, "the gate must ask the client over the wire"
    req_id = requests[-1]["id"]
    server._resolve_client_response({"id": req_id, "result": {"decision": "allow"}})
    assert await ask == "allow"

    # optionId mapping (official ACP clients answer with optionId)
    ask2 = asyncio.create_task(gate.request("write_file", "write", "patch"))
    await asyncio.sleep(0.05)
    req2 = [u for u in server.updates if u.get("method") == "session/request_permission"][-1]["id"]
    server._resolve_client_response({"id": req2, "result": {"optionId": "allow_always"}})
    assert await ask2 == "always"

    # cancel denies a pending approval: an interrupted turn stops prompting
    ask3 = asyncio.create_task(gate.request("run_shell", "exec", "rm"))
    await asyncio.sleep(0.05)
    server._cancel(sid)
    assert await asyncio.wait_for(ask3, timeout=2) == "deny"


def test_acp_prompt_parts_split_text_and_images():
    text, images = _prompt_parts([
        {"type": "text", "text": "look at "},
        {"type": "image", "data": "QUJD", "mimeType": "image/png"},
    ])
    assert text == "look at "
    assert images == [{"type": "image_url",
                       "image_url": {"url": "data:image/png;base64,QUJD"}}]


def test_file_lock_is_mutually_exclusive_and_times_out(tmp_path):
    target = tmp_path / "shared.jsonl"
    counter = {"n": 0}

    def worker():
        for _ in range(50):
            with file_lock(target):
                counter["n"] += 1
                current = counter["n"]
                asyncio.run(asyncio.sleep(0))  # yield inside the lock
                assert counter["n"] == current

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert counter["n"] == 200

    with file_lock(target):
        with pytest.raises(TimeoutError):
            with file_lock(target, timeout=0.1):
                pass


def test_concurrent_transcript_appends_all_land(isolated_home, workspace):
    from oaset.agent.messages import Message
    from oaset.session.store import SessionStore

    store = SessionStore()
    session = store.new_session(workspace, "mock/mock-echo")
    errors: list[Exception] = []

    def appender(i: int) -> None:
        try:
            for j in range(20):
                store.append_message(session, Message(role="user", content=f"t{i}-{j}"))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=appender, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors
    loaded = store.load(session.meta.session_id)
    contents = [str(m.content) for m in loaded.conversation.messages]
    assert len(contents) == 80, f"lost appends: {len(contents)}/80"
    assert len(set(contents)) == 80  # no interleaved/corrupt lines


def test_gateway_token_file_is_created_and_read(isolated_home):
    from oaset.gateway import load_or_create_token

    token = load_or_create_token()
    assert len(token) >= 24
    assert load_or_create_token() == token  # stable across calls


def test_acp_serves_with_mcp_enabled_by_default(workspace):
    server = _server(workspace)
    out = asyncio.run(server.handle_async({"jsonrpc": "2.0", "id": 1,
                                           "method": "session/new", "params": {}}))
    sid = json.loads(json.dumps(out))["result"]["sessionId"]
    host = server.sessions[sid]
    assert host.mcp_enabled is True  # desktop clients get tools without asking twice
