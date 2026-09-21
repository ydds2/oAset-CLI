"""Ship-complete contract: mutating source without evidence is not done."""

from __future__ import annotations

from pathlib import Path

from oaset.agent import AgentLoop, Conversation, initial_system_prompt
from oaset.agent.verify import is_source_path, needs_verify, nudge_text
from oaset.providers import MockProvider, MockToolCall, MockTurn
from oaset.skills import delete_skill, find_skill, load_skills
from oaset.tools import AutoGate, ToolContext, ToolRegistry


def build(workspace, provider, max_iterations=0):
    conversation = Conversation("sys")
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=2000))
    loop = AgentLoop(provider, registry, conversation, max_iterations=max_iterations)
    return loop, conversation


async def test_verify_gate_nudges_then_allows_answer(workspace):
    (workspace / "v.py").write_text("value = 1\n", encoding="utf-8")
    provider = MockProvider([
        MockTurn(tool_calls=[MockToolCall("edit_file", {
            "path": "v.py", "old_text": "value = 1", "new_text": "value = 2",
        })]),
        MockTurn(content_chunks=["edited, shipping now"]),
        MockTurn(content_chunks=["still missing tests"]),
    ])
    loop, conversation = build(workspace, provider)
    events: list[dict] = []
    await loop.run("fix it", events.append)
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "turn_done"
    assert any(e.get("type") == "notice" and "verify-gate" in str(e.get("text", ""))
               for e in events)
    nudged = [m for m in conversation.messages
              if m.role == "user" and m.content and "verify-gate" in str(m.content)]
    assert nudged
    premature = [m for m in conversation.messages
                 if m.role == "assistant" and m.content and "shipping now" in str(m.content)]
    assert premature
    assert "still missing tests" in events[-1]["content"]


async def test_verify_gate_passes_when_shell_ran(workspace):
    (workspace / "v.py").write_text("value = 1\n", encoding="utf-8")
    provider = MockProvider([
        MockTurn(tool_calls=[MockToolCall("edit_file", {
            "path": "v.py", "old_text": "value = 1", "new_text": "value = 2",
        })]),
        MockTurn(tool_calls=[MockToolCall("run_shell", {"command": "echo proved"})]),
        MockTurn(content_chunks=["proved"]),
    ])
    loop, conversation = build(workspace, provider)
    events: list[dict] = []
    await loop.run("fix then prove", events.append)
    assert events[-1]["type"] == "turn_done"
    assert events[-1]["content"] == "proved"
    assert not any(m.role == "user" and m.content and "verify-gate" in str(m.content)
                   for m in conversation.messages[1:])


async def test_plain_notes_do_not_trip_the_gate(workspace):
    provider = MockProvider([
        MockTurn(tool_calls=[MockToolCall("write_file", {
            "path": "note.txt", "content": "saved-marker",
        })]),
        MockTurn(content_chunks=["saved"]),
    ])
    loop, _conversation = build(workspace, provider)
    events: list[dict] = []
    await loop.run("save a note", events.append)
    assert events[-1]["type"] == "turn_done"
    assert events[-1]["content"] == "saved"
    assert not any(e.get("type") == "notice" and "verify-gate" in str(e.get("text", ""))
                   for e in events)


def test_verify_helpers():
    assert is_source_path("src/app.py")
    assert is_source_path("Main.tsx")
    assert not is_source_path("note.txt")
    assert not is_source_path("SKILL.md")
    assert needs_verify(["a.py"], False)
    assert not needs_verify(["a.py"], True)
    assert not needs_verify([], False)
    assert "run_shell" in nudge_text(["src/app.py"])


def test_builtin_ship_complete_skill_is_always_loaded(tmp_path):
    skills = load_skills(tmp_path)
    ship = find_skill(tmp_path, "ship-complete")
    assert ship is not None
    assert ship.scope == "builtin"
    assert "Never treat compiling code as done" in ship.description
    assert any(s.name == "Ship complete" for s in skills)


def test_cannot_delete_builtin_skill(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="built-in"):
        delete_skill(tmp_path, "ship-complete")
    assert find_skill(tmp_path, "ship-complete") is not None


def test_system_prompt_carries_ship_complete_contract(tmp_path):
    prompt, _ = initial_system_prompt(tmp_path)
    assert "Done is observed" in prompt
    assert "shortest path" in prompt
    assert "Ship complete" in prompt
    assert str(find_skill(tmp_path, "ship-complete").path) in prompt


# ------------------------------------- the proof must actually prove something


async def test_failing_command_is_not_verification(workspace):
    """run_shell returns non-zero exits as ordinary output; only the exit code
    distinguishes a real build from `run_shell("echo proved")` theater."""
    (workspace / "v.py").write_text("value = 1\n", encoding="utf-8")
    provider = MockProvider([
        MockTurn(tool_calls=[MockToolCall("edit_file", {
            "path": "v.py", "old_text": "value = 1", "new_text": "value = 2",
        })]),
        MockTurn(tool_calls=[MockToolCall("run_shell",
                                          {"command": "exit 3"})]),
        MockTurn(content_chunks=["it failed but I'll call it done"]),
        MockTurn(content_chunks=["acknowledged"]),
    ])
    loop, conversation = build(workspace, provider)
    events: list[dict] = []
    await loop.run("fix then pretend", events.append)
    assert any(m.role == "user" and m.content and "verify-gate" in str(m.content)
               for m in conversation.messages[1:]), "a failing command must not arm the gate"


async def test_apply_patch_arms_the_gate(workspace):
    """apply_patch carries its paths inside the diff; the gate used to read
    only a top-level "path" argument and never armed for it."""
    from oaset.tools.patch import parse_unified_diff

    (workspace / "v.py").write_text("value = 1\n", encoding="utf-8")
    diff = "\n".join([
        "--- a/v.py", "+++ b/v.py", "@@ -1 +1 @@",
        "-value = 1", "+value = 2",
    ])
    assert parse_unified_diff(diff), "test diff must parse"
    provider = MockProvider([
        MockTurn(tool_calls=[MockToolCall("apply_patch", {"diff": diff})]),
        MockTurn(content_chunks=["patched, shipping now"]),
        MockTurn(content_chunks=["verified later"]),
    ])
    loop, conversation = build(workspace, provider)
    events: list[dict] = []
    await loop.run("patch without proof", events.append)
    assert any(e.get("type") == "notice" and "verify-gate" in str(e.get("text", ""))
               for e in events), "apply_patch must arm the verify gate"


async def test_second_mutation_invalidates_earlier_proof(workspace):
    (workspace / "v.py").write_text("value = 1\n", encoding="utf-8")
    provider = MockProvider([
        MockTurn(tool_calls=[MockToolCall("edit_file", {
            "path": "v.py", "old_text": "value = 1", "new_text": "value = 2",
        })]),
        MockTurn(tool_calls=[MockToolCall("run_shell", {"command": "echo proved"})]),
        MockTurn(tool_calls=[MockToolCall("edit_file", {
            "path": "v.py", "old_text": "value = 2", "new_text": "value = 3",
        })]),
        MockTurn(content_chunks=["done again, shipping now"]),
        MockTurn(content_chunks=["fine"]),
    ])
    loop, conversation = build(workspace, provider)
    events: list[dict] = []
    await loop.run("edit, prove, edit again", events.append)
    assert any(m.role == "user" and m.content and "verify-gate" in str(m.content)
               for m in conversation.messages[1:]), \
        "proof from before the second edit must not cover it"


# -------------------------------- evidence gate + requirement completeness


async def test_evidence_mode_demands_receipts_and_closed_scenarios(workspace):
    """With /evidence on: (1) open todo items are unmet requirements the gate
    lists back, and (2) 'done' without any verification receipt is pushed
    back. The completeness loop closes when the todos are finished and a
    receipt exists."""
    from oaset.providers import MockToolCall

    (workspace / "v.py").write_text("value = 1\n", encoding="utf-8")
    provider = MockProvider([
        MockTurn(tool_calls=[MockToolCall("edit_file", {
            "path": "v.py", "old_text": "value = 1", "new_text": "value = 2",
        })]),
        MockTurn(tool_calls=[MockToolCall("todo_write", {"todos": [
            {"content": "scenario: parser handles empty input", "status": "pending"},
        ]})]),
        MockTurn(content_chunks=["all done, shipping now"]),
        MockTurn(tool_calls=[MockToolCall("run_shell", {"command": "echo receipt"})]),
        MockTurn(tool_calls=[MockToolCall("todo_write", {"todos": [
            {"content": "scenario: parser handles empty input", "status": "completed"},
        ]})]),
        MockTurn(content_chunks=["now it is done"]),
    ])
    conversation = Conversation("sys")
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=2000))
    registry.ctx.session_state["evidence"] = True
    loop = AgentLoop(provider, registry, conversation, max_iterations=0)
    events: list[dict] = []
    await loop.run("implement with evidence", events.append)

    nudges = [m for m in conversation.messages
              if m.role == "user" and m.content
              and ("[requirements]" in str(m.content) or "verify-gate" in str(m.content))]
    assert nudges, "open scenarios must be listed as unmet requirements"
    assert "[requirements]" in str(nudges[0].content)

    evidence_events = [e for e in events if e.get("type") == "evidence"]
    assert evidence_events, "verification receipts must be emitted as evidence"
    items = evidence_events[-1]["items"]
    assert any(i.get("tool") == "run_shell" and i.get("ok") for i in items)


async def test_evidence_off_keeps_the_old_advisory_gate(workspace):
    from oaset.providers import MockToolCall

    (workspace / "v.py").write_text("value = 1\n", encoding="utf-8")
    provider = MockProvider([
        MockTurn(tool_calls=[MockToolCall("edit_file", {
            "path": "v.py", "old_text": "value = 1", "new_text": "value = 2",
        })]),
        MockTurn(tool_calls=[MockToolCall("todo_write", {"todos": [
            {"content": "scenario: forgotten one", "status": "pending"},
        ]})]),
        MockTurn(content_chunks=["done, shipping now"]),
        MockTurn(content_chunks=["ok"]),
    ])
    conversation = Conversation("sys")
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=2000))
    loop = AgentLoop(provider, registry, conversation, max_iterations=0)
    events: list[dict] = []
    await loop.run("evidence off", events.append)
    # no mutations-nudge demands receipts, and no evidence events are emitted
    assert not [e for e in events if e.get("type") == "evidence"]


def test_code_outline_tool_indexes_symbols(tmp_path):
    import asyncio
    import json as _json

    from oaset.tools import AutoGate, ToolContext, ToolRegistry

    (tmp_path / "calc.py").write_text(
        "class Calc:\n"
        "    def add(self, a, b):\n"
        "        return a + b\n"
        "\n"
        "def top_level():\n"
        "    pass\n", encoding="utf-8")
    registry = ToolRegistry(gate=AutoGate())
    registry.bind(ToolContext(cwd=tmp_path, mode="auto"))
    res = asyncio.run(registry.dispatch(
        "code_outline", _json.dumps({"path": "calc.py"})))
    assert not res.is_error
    assert "class Calc" in res.output and "def add(self, a, b)" in res.output
    assert "L1" in res.output and "L2" in res.output
    assert "ast" in res.output, "the parse mode is disclosed (transparency)"


async def test_evidence_mode_persists_screenshots_as_receipts(workspace):
    """Under /evidence, a computer-tool screenshot is written to disk and the
    receipt names the artifact — the audit chain can show what the agent saw."""
    from oaset.providers import MockToolCall

    conversation = Conversation("sys")
    registry = ToolRegistry(gate=AutoGate())
    out_dir = workspace / "spill"
    registry.bind(ToolContext(cwd=workspace, mode="auto", output_limit=2000))
    registry.ctx.session_state["evidence"] = True
    registry.ctx.session_state["tool_output_dir"] = str(out_dir)
    loop = AgentLoop(None, registry, conversation, max_iterations=0)

    call = MockToolCall("browser_screenshot", {})
    call.id = "shot-1"
    def emit(_e):
        None
    await loop._finish_tool(call, "screenshot ok", False, emit,
                            image=b"\x89PNG-fake", exit_code=None)

    shots = [i for i in loop._evidence_entries if i.get("kind") == "screenshot"]
    assert shots and shots[0]["artifact"], "the PNG must be persisted and named"
    assert Path(shots[0]["artifact"]).read_bytes() == b"\x89PNG-fake"


# ------------------------------------------- interrupted-turn carry

async def test_interrupted_edits_carry_into_the_next_turn(workspace):
    """A mid-turn-Esc used to drop _mutated_paths with the loop instance (a
    fresh loop every turn): the edited-but-never-verified file was never
    named again. The carry lives in session_state; the next turn's gate
    must nudge about it, and a clean completion closes the matter."""
    import asyncio
    import contextlib

    (workspace / "v.py").write_text("value = 1\n", encoding="utf-8")

    class SlowProvider(MockProvider):
        async def stream(self, messages, tools):
            async for event in super().stream(messages, tools):
                await asyncio.sleep(0.05)
                yield event

    provider = SlowProvider([
        MockTurn(tool_calls=[MockToolCall("edit_file", {
            "path": "v.py", "old_text": "value = 1", "new_text": "value = 2",
        })]),
        MockTurn(content_chunks=["chunk"] * 100),
    ])
    loop, conversation = build(workspace, provider)
    state = loop.registry.ctx.session_state
    events: list[dict] = []

    async def run():
        await loop.run("fix it", events.append)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.6)  # edit executed, answer stream underway
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert state.get("verify_pending_paths") == ["v.py"], (
        "an interrupted turn must not forget which files it left unverified")

    # NEXT turn: a FRESH loop on the same registry/conversation names it
    answer = MockProvider([
        MockTurn(content_chunks=["done now"]),
        MockTurn(content_chunks=["final"]),
    ])
    loop2 = AgentLoop(answer, loop.registry, conversation)
    events2: list[dict] = []
    await loop2.run("next request", events2.append)
    nudges = [m for m in conversation.messages
              if m.role == "user" and m.content and "verify-gate" in str(m.content)]
    assert nudges and "v.py" in str(nudges[-1].content), (
        "the next turn's gate must name the interrupted turn's edit")
    assert events2[-1]["type"] == "turn_done"
    assert "verify_pending_paths" not in state, (
        "a clean completion closes the carry — no perpetual nagging")


async def test_verify_success_clears_the_carry(workspace):
    (workspace / "v.py").write_text("value = 1\n", encoding="utf-8")
    provider = MockProvider([
        MockTurn(tool_calls=[MockToolCall("edit_file", {
            "path": "v.py", "old_text": "value = 1", "new_text": "value = 2",
        })]),
        MockTurn(tool_calls=[MockToolCall("run_shell", {"command": "echo proved"})]),
        MockTurn(content_chunks=["proved"]),
    ])
    loop, _conversation = build(workspace, provider)
    state = loop.registry.ctx.session_state
    await loop.run("fix then prove", lambda e: None)
    assert "verify_pending_paths" not in state, (
        "a successful verification releases the carry immediately")


async def test_carry_survives_attach_and_new_loop_instances(workspace):
    (workspace / "v.py").write_text("value = 1\n", encoding="utf-8")
    provider = MockProvider([
        MockTurn(tool_calls=[MockToolCall("edit_file", {
            "path": "v.py", "old_text": "value = 1", "new_text": "value = 2",
        })]),
        MockTurn(content_chunks=["chunk"] * 100),
    ])
    loop, _ = build(workspace, provider)
    state = loop.registry.ctx.session_state

    import asyncio
    import contextlib

    async def run():
        await loop.run("fix", lambda e: None)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.6)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # the seeded list is deduplicated and ordered
    state["verify_pending_paths"] = ["v.py", "v.py", "other.py"]
    fresh = AgentLoop(MockProvider([MockTurn(content_chunks=["x"]),
                                    MockTurn(content_chunks=["y"])]),
                      loop.registry, Conversation("sys"))
    fresh._mutated_paths  # noqa: B018 - attribute exists pre-run
    assert fresh._pending_state().get("verify_pending_paths") == [
        "v.py", "v.py", "other.py"], "state is the storage, run() seeds from it"
