"""Blind-review P1 fixes + P2 machine contracts (2026-09-16 round).

Each test pins a finding from the second blind review: a mistyped --model
must fail with hints (exit 2), not a traceback; user-controlled session
titles must not be able to MarkupError /sessions; headless -c/-p must keep
its resume promise (transcript + usage persisted); and stream-json lines
carry the versioned envelope desktop consumers order and gate on.
"""

from __future__ import annotations

import json

from oaset.cli import main, run_one_shot
from oaset.config import default_config
from oaset.i18n import t
from oaset.providers import MockProvider, MockTurn


def test_mistyped_model_fails_with_a_hint_not_a_traceback(isolated_home, capsys):
    rc = main(["--model", "nope/not-a-model"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "nope/not-a-model" in err
    assert "models list" in err


def test_delete_confirmation_has_its_own_words():
    # the delete button used to read "save configuration" — a dangerous mislabel
    assert t("sessions_confirm_yes") != t("wizard_save")
    assert "delete" in t("sessions_confirm_yes").lower() \
        or "删除" in t("sessions_confirm_yes")


async def test_headless_resume_persists_the_exchange(isolated_home, workspace):
    from oaset.session.store import SessionStore

    store = SessionStore()
    session = store.new_session(workspace, "mock/mock-echo")
    cfg = default_config()
    rc = await run_one_shot(
        cfg, workspace, "hello headless", "mock/mock-echo",
        MockProvider([MockTurn(content_chunks=["headless reply"],
                               usage={"total_tokens": 11})]),
        yolo=True, resume=session)
    assert rc == 0
    loaded = store.load(session.meta.session_id)
    roles = [m.role for m in loaded.conversation.messages]
    assert roles == ["user", "assistant"]
    assert str(loaded.conversation.messages[1].content) == "headless reply"
    usage = store.session_usage(session.meta.session_id)
    assert usage["total_tokens"] == 11


async def test_stream_json_lines_carry_the_envelope(isolated_home, workspace, capsys):
    cfg = default_config()
    rc = await run_one_shot(
        cfg, workspace, "enveloped", "mock/mock-echo",
        MockProvider([MockTurn(content_chunks=["ok"])]),
        yolo=True, output_format="stream-json")
    assert rc == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines, "stream-json must print events"
    seqs = []
    for line in lines:
        doc = json.loads(line)
        assert doc["schema_version"] == 1
        assert "timestamp" in doc and "type" in doc and "data" in doc
        seqs.append(doc["sequence"])
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_sessions_json_is_machine_readable(isolated_home, workspace, capsys):
    from oaset.session.store import SessionStore

    SessionStore().new_session(workspace, "mock/mock-echo")
    rc = main(["sessions", "--json"])
    assert rc == 0
    docs = json.loads(capsys.readouterr().out)
    assert isinstance(docs, list) and docs
    assert {"session_id", "cwd", "title", "updated_at", "message_count"} <= set(docs[0])
