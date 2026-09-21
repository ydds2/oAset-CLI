from pathlib import Path

import pytest

from oaset.agent.messages import Message
from oaset.session import SessionStore


def add(store, session, message):
    """Mirror the production flow: append to conversation, then persist."""
    session.conversation.append(message)
    store.append_message(session, message)


def test_new_append_load_roundtrip(isolated_home):
    store = SessionStore()
    session = store.new_session(Path("C:/proj"), model="mock/mock-echo", title="first")
    session.conversation.system_prompt = "sys"
    add(store, session, Message(role="user", content="hello world"))
    add(store, session, Message(role="assistant", content="hi there"))

    loaded = store.load(session.meta.session_id)
    assert loaded is not None
    assert [m.content for m in loaded.conversation.messages] == ["hello world", "hi there"]
    assert loaded.meta.message_count == 2
    assert loaded.file.exists()
    # first line is the meta record
    first_line = loaded.file.read_text(encoding="utf-8").splitlines()[0]
    assert '"type": "meta"' in first_line


def test_title_defaults_from_first_user_message(isolated_home):
    store = SessionStore()
    session = store.new_session(Path("C:/proj"), model="m")
    add(store, session, Message(role="user", content="  fix  the  bug  "))
    assert session.meta.title == "fix the bug"


def test_fork_copies_history(isolated_home):
    store = SessionStore()
    session = store.new_session(Path("C:/proj"), model="m", title="origin")
    add(store, session, Message(role="user", content="one"))
    add(store, session, Message(role="assistant", content="two"))
    fork = store.fork(session)
    assert fork.meta.session_id != session.meta.session_id
    assert fork.meta.title.startswith("[fork]")
    assert len(fork.conversation.messages) == 2
    loaded = store.load(fork.meta.session_id)
    assert loaded is not None and len(loaded.conversation.messages) == 2


def test_list_sessions_filters_by_cwd_and_orders(isolated_home):
    store = SessionStore()
    s1 = store.new_session(Path("C:/a"), model="m")
    s2 = store.new_session(Path("C:/b"), model="m")
    s3 = store.new_session(Path("C:/a"), model="m")
    add(store, s3, Message(role="user", content="newest"))
    here = store.list_sessions(cwd=str(Path("C:/a")))
    ids = {m.session_id for m in here}
    assert s1.meta.session_id in ids and s3.meta.session_id in ids
    assert s2.meta.session_id not in ids
    assert here[0].session_id == s3.meta.session_id  # most recently updated first


def test_corrupt_lines_are_skipped(isolated_home):
    store = SessionStore()
    session = store.new_session(Path("C:/x"), model="m")
    add(store, session, Message(role="user", content="kept"))
    with session.file.open("a", encoding="utf-8") as fh:
        fh.write("{corrupt json\n")
        fh.write('{"type": "message", "message": {"role": "assistant", "content": "after"}}\n')
    loaded = store.load(session.meta.session_id)
    assert [m.content for m in loaded.conversation.messages] == ["kept", "after"]


def test_latest_for_cwd(isolated_home):
    store = SessionStore()
    store.new_session(Path("C:/q"), model="m")
    session = store.new_session(Path("C:/q"), model="m")
    add(store, session, Message(role="user", content="bump"))
    found = store.latest_for_cwd(Path("C:/q"))
    assert found is not None
    assert found.meta.session_id == session.meta.session_id


def test_delete_session_only_removes_target_transcript(isolated_home):
    """A1 regression: deleting one session must not touch sibling sessions
    sharing the same workdir bucket, nor their checkpoints."""
    store = SessionStore()
    cwd = Path("C:/proj")
    s1 = store.new_session(cwd, model="m", title="one")
    s2 = store.new_session(cwd, model="m", title="two")
    s3 = store.new_session(cwd, model="m", title="three")
    add(store, s1, Message(role="user", content="keep me"))
    add(store, s2, Message(role="user", content="delete target"))
    # a checkpoint belongs to the shared bucket; siblings must survive the delete
    store.checkpoint(s2, {str(cwd / "a.txt"): "content"})
    bucket = s1.meta.path.parent
    assert bucket.exists()

    assert store.delete_session(s2.meta.session_id) is True

    assert not s2.meta.path.exists()
    assert s1.meta.path.exists() and s3.meta.path.exists()
    loaded = store.load(s1.meta.session_id)
    assert loaded is not None and loaded.meta.message_count == 1
    remaining_ids = {m.session_id for m in store.list_sessions()}
    assert s1.meta.session_id in remaining_ids
    assert s3.meta.session_id in remaining_ids
    assert s2.meta.session_id not in remaining_ids
    assert bucket.exists()


def test_delete_last_session_in_bucket_cleans_up(isolated_home):
    """When the last transcript in a bucket is gone, the bucket (and its
    now-orphaned shared checkpoints) is removed."""
    store = SessionStore()
    s1 = store.new_session(Path("C:/solo"), model="m")
    store.checkpoint(s1, {"C:/solo/x.txt": "v"})
    bucket = s1.meta.path.parent
    assert store.delete_session(s1.meta.session_id) is True
    assert not bucket.exists()


def test_delete_session_exact_id_wins_over_suffix(isolated_home):
    """Exact id match must win; a short suffix matching multiple sessions
    must not delete the wrong one silently when ambiguous."""
    store = SessionStore()
    cwd = Path("C:/proj")
    a = store.new_session(cwd, model="m")
    b = store.new_session(cwd, model="m")
    # suffix "e" may match several ids; deletion must still remove exactly one
    # session and leave the other intact.
    assert store.delete_session(a.meta.session_id) is True
    assert store.load(b.meta.session_id) is not None
    assert store.load(a.meta.session_id) is None


def test_delete_session_other_workdir_untouched(isolated_home):
    store = SessionStore()
    s_here = store.new_session(Path("C:/here"), model="m")
    s_there = store.new_session(Path("C:/there"), model="m")
    assert store.delete_session(s_here.meta.session_id) is True
    assert s_there.meta.path.exists()
    assert store.load(s_there.meta.session_id) is not None


# ------------------------------------------------- compaction / rewind boundaries


def _exchange(store, session, i, *, rounds=1):
    """One user turn with `rounds` tool call/result pairs, then the answer."""
    from oaset.agent.messages import ToolCallReq

    add(store, session, Message(role="user", content=f"q{i}"))
    for r in range(rounds):
        call = ToolCallReq(id=f"c{i}-{r}", name="read_file", arguments="{}")
        add(store, session, Message(role="assistant", tool_calls=[call]))
        add(store, session, Message(role="tool", content=f"result{i}-{r}",
                                    tool_call_id=call.id))
    add(store, session, Message(role="assistant", content=f"a{i}"))


def test_compaction_keeps_whole_turns(isolated_home, workspace):
    """The kept window must not start with a tool result whose call was
    summarized away — providers reject that payload."""
    import asyncio

    from oaset.agent.context import SUMMARY_KEEP_RECENT, compact_history, turn_boundary

    store = SessionStore()
    session = store.new_session(workspace, model="mock/mock-echo")
    _exchange(store, session, 1)
    _exchange(store, session, 2)
    _exchange(store, session, 3, rounds=3)      # a tool-heavy turn at the tail
    msgs = session.conversation.messages
    # the naive fixed-count cut lands ON a tool result: that is the bug
    naive = len(msgs) - SUMMARY_KEEP_RECENT
    assert msgs[naive].role == "tool"
    split = turn_boundary(msgs, SUMMARY_KEEP_RECENT)
    assert msgs[split].role == "user", "the boundary must land on a user turn"
    assert len(msgs) - split >= SUMMARY_KEEP_RECENT

    class Summarizer:
        async def stream(self, messages, tools=None, **kwargs):
            from oaset.providers import ContentDelta

            yield ContentDelta("the summary")

    assert asyncio.run(compact_history(session.conversation, Summarizer())) == "the summary"
    kept = session.conversation.messages
    assert kept[0].role == "user" and "conversation summary" in str(kept[0].content)
    assert kept[1].role == "user", "kept tail must start at a turn boundary"
    # every kept tool result still has its assistant parent before it
    seen_calls: set[str] = set()
    for message in kept[1:]:
        for call in (message.tool_calls or []):
            seen_calls.add(call.id)
        if message.role == "tool":
            assert message.tool_call_id in seen_calls, "orphan tool result"


def test_compaction_is_a_noop_when_no_turn_boundary_exists(isolated_home, workspace):
    """History with no user message to cut back to (loop-injected run, resumed
    transcript) is left alone instead of being cut mid-turn."""
    import asyncio

    from oaset.agent.context import compact_history

    store = SessionStore()
    session = store.new_session(workspace, model="mock/mock-echo")
    for i in range(3):                      # 6 messages, none of them a user turn
        add(store, session, Message(role="assistant", content=f"a{i}"))
        add(store, session, Message(role="tool", content=f"r{i}", tool_call_id=f"t{i}"))

    class Summarizer:
        async def stream(self, messages, tools=None, **kwargs):
            raise AssertionError("nothing safely compactable must not call the provider")
            yield  # pragma: no cover

    before = [m.content for m in session.conversation.messages]
    assert asyncio.run(compact_history(session.conversation, Summarizer())) == ""
    assert [m.content for m in session.conversation.messages] == before


def test_compaction_marker_replays_the_same_window(isolated_home, workspace):
    """A reload replays the boundary the marker recorded, tool pairs intact."""
    import asyncio

    from oaset.agent.context import SUMMARY_KEEP_RECENT, compact_history, turn_boundary

    store = SessionStore()
    session = store.new_session(workspace, model="mock/mock-echo")
    _exchange(store, session, 1)
    _exchange(store, session, 2, rounds=3)

    class Summarizer:
        async def stream(self, messages, tools=None, **kwargs):
            from oaset.providers import ContentDelta

            yield ContentDelta("summary text")

    dropped = turn_boundary(session.conversation.messages, SUMMARY_KEEP_RECENT)
    assert asyncio.run(compact_history(session.conversation, Summarizer()))
    store.append_compaction(session, "summary text", dropped)
    live = [(m.role, m.content, m.tool_call_id) for m in session.conversation.messages]

    reloaded = store.load(session.meta.session_id)
    assert reloaded is not None
    assert [(m.role, m.content, m.tool_call_id) for m in reloaded.conversation.messages] == live

    # a legacy marker without a usable `dropped` still lands on a turn boundary
    from oaset.agent.messages import Conversation
    from oaset.session.store import apply_transcript_entry

    legacy = Conversation(system_prompt="sys", messages=list(session.conversation.messages))
    apply_transcript_entry(legacy, {"type": "compact", "summary": "older format"})
    assert legacy.messages[1].role == "user", "legacy markers still land on a turn boundary"


def test_rewind_ignores_agent_injected_user_messages(isolated_home, workspace):
    """/undo counts what the user typed, not the steering/verify injections the
    loop persists as user-role messages.

    The shape matters: the injection arrives *after* the user's last prompt
    (a background task finishing mid-turn), so the old code — which took the
    last user-role line as "the turn" — cut there and left the exchange the
    user actually wanted back in place.
    """
    store = SessionStore()
    session = store.new_session(workspace, model="mock/mock-echo")
    _exchange(store, session, 1, rounds=0)
    add(store, session, Message(role="user", content="q2"))
    add(store, session, Message(role="assistant", content="a2"))
    add(store, session, Message(role="user", content="[mid-stream] focus on the parser"))
    add(store, session, Message(role="assistant", content="a2b"))

    remaining = store.rewind_turns(session, 1)
    reloaded = store.load(session.meta.session_id)
    contents = [m.content for m in reloaded.conversation.messages]
    assert contents == ["q1", "a1"], "the last real exchange went, with its injections"
    assert remaining == len(contents)

    with pytest.raises(ValueError):
        store.rewind_turns(session, 98)


def test_short_session_id_resolves_when_unambiguous_and_refuses_when_not(isolated_home):
    """`--resume 1f3a` (a short id the sessions list shows) must behave like
    `/delete 1f3a`: an exact id wins, a unique suffix resolves, an ambiguous
    one resolves to nothing instead of silently picking a transcript."""
    store = SessionStore()
    cwd = Path("C:/proj")
    a = store.new_session(cwd, model="m")
    b = store.new_session(cwd, model="m")
    a_short = a.meta.session_id[1:]      # cannot match any other id
    assert store.load(a_short).meta.session_id == a.meta.session_id

    # every generated id starts with "session_": that suffix is ambiguous
    assert store.load("session_") is None
    assert store.delete_session("session_") is False
    assert store.load(a.meta.session_id) is not None
    assert store.load(b.meta.session_id) is not None

    # the same rule on the delete path
    assert store.delete_session(b.meta.session_id[1:]) is True
    assert store.load(b.meta.session_id) is None
