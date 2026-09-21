"""Session persistence: per-workdir JSONL transcripts + a global append-only index.

Directory layout mirrors the verified Kimi Code scheme:
  ~/.oaset/sessions/wd_<name>_<hash8>/session_<uuid>.jsonl
  ~/.oaset/session_index.jsonl  (one line per save: last line wins per session)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oaset.agent.messages import Conversation

import contextlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oaset.utils import new_id, now_iso, oaset_home, workdir_bucket

INDEX_VERSION = 1


def _local_day(raw: str) -> str:
    """UTC timestamp -> the user's LOCAL calendar day.

    now_iso() writes UTC; slicing at[:10] bucketed an evening (16:00-24:00
    UTC) into "tomorrow" for UTC+8 users, which reads as wrong "daily" usage.
    """
    import datetime as _dt

    try:
        return _dt.datetime.fromisoformat(str(raw)).astimezone().date().isoformat()
    except Exception:
        return str(raw)[:10]


def _entry_text(entry: dict[str, Any]) -> str:
    """Plain text of a ``type: message`` line (multimodal content flattened)."""
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content
                        if isinstance(part, dict))
    return str(content or "")


def _is_user_turn_entry(entry: dict[str, Any]) -> bool:
    """True when this transcript line is a turn the user actually typed."""
    from oaset.agent.messages import is_injected_user_text

    return (entry.get("type") == "message"
            and (entry.get("message") or {}).get("role") == "user"
            and not is_injected_user_text(_entry_text(entry)))


def apply_transcript_entry(conversation: Conversation, entry: dict[str, Any]) -> None:
    """Replay one JSONL line into an in-memory conversation.

    ``type: compact`` replaces older messages with the stored summary so a
    restored session matches what ``/compact`` left in RAM.
    """
    kind = entry.get("type")
    if kind == "message":
        from oaset.agent.messages import Message

        payload = entry.get("message") or {}
        conversation.append(Message.from_dict(payload))
        return
    if kind != "compact":
        return
    from oaset.agent.context import SUMMARY_KEEP_RECENT, safe_boundary
    from oaset.agent.messages import Message

    summary = str(entry.get("summary") or "").strip() or "(empty summary)"
    # `dropped` is the boundary that was used live (messages replaced by the
    # summary); older entries stored a fixed-count guess, so the index is
    # normalised back onto a turn boundary either way — a kept window starting
    # with a tool result whose call was summarized away is a payload the
    # provider rejects on the next turn.
    keep_from = entry.get("dropped")
    if not isinstance(keep_from, int) or keep_from < 0:
        keep_from = len(conversation.messages) - SUMMARY_KEEP_RECENT
    keep_from = safe_boundary(conversation.messages, keep_from)
    keep = list(conversation.messages[keep_from:])
    conversation.messages = [
        Message(
            role="user",
            content=f"[conversation summary — earlier turns compacted]\n{summary}",
        ),
        *keep,
    ]


@dataclass
class SessionMeta:
    session_id: str
    path: Path
    cwd: str
    title: str = ""
    model: str = ""
    created_at: str = ""
    updated_at: str = ""
    message_count: int = 0


def _new_conversation() -> "Conversation":
    """Deferred: session/store sits on the first-frame path; the agent
    chain it used to import here cost the welcome ~150ms."""
    from oaset.agent.messages import Conversation

    return Conversation()


@dataclass
class Session:
    meta: SessionMeta
    conversation: "Conversation" = field(default_factory=_new_conversation)
    file: Path | None = None


class SessionStore:
    def __init__(self, home: Path | None = None):
        self.home = home or oaset_home()
        self.base = self.home / "sessions"
        self.base.mkdir(parents=True, exist_ok=True)
        self.index_path = self.home / "session_index.jsonl"

    # ------------------------------------------------------------------ new

    def new_session(self, cwd: Path, model: str, title: str = "") -> Session:
        session_id = new_id("session")
        bucket = self.base / workdir_bucket(cwd)
        bucket.mkdir(parents=True, exist_ok=True)
        path = bucket / f"{session_id}.jsonl"
        meta = SessionMeta(
            session_id=session_id,
            path=path,
            cwd=str(cwd),
            title=title,
            model=model,
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        session = Session(meta=meta, file=path)
        self._write_line(
            path,
            {
                "type": "meta",
                "version": INDEX_VERSION,
                "session_id": session_id,
                "cwd": str(cwd),
                "model": model,
                "title": title,
                "created_at": meta.created_at,
            },
        )
        self._sync_index(meta)
        return session

    def usage_by_days(self, days: int = 7) -> list[tuple[str, dict[str, int]]]:
        """/insights: aggregate usage lines per local day, most recent last."""
        from collections import defaultdict

        agg: dict[str, dict[str, int]] = defaultdict(
            lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "turns": 0}
        )
        for meta in self._all_metas():
            if not meta.path.exists():
                continue
            try:
                content = meta.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in content.splitlines():
                if '"type": "usage"' not in line and '"type":"usage"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "usage":
                    continue
                day = _local_day(entry.get("at", ""))
                if not day:
                    continue
                a = agg[day]
                for field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    a[field_name] += int(entry.get(field_name, 0) or 0)
                a["turns"] += 1
        # days<=0 used to mean "[-0:]" == EVERY day ever recorded
        if days <= 0:
            return []
        return sorted(agg.items())[-days:]

    def rewind_turns(self, session, turns: int = 1) -> int:
        """/undo N: drop the last `turns` user-initiated exchanges from
        the transcript (soft-delete semantics simplified to a hard
        truncate). Returns the remaining message count.

        Only real user turns count. Steering injections, verification nudges
        and background results are persisted as user-role messages too; counting
        them made /undo 1 drop a synthetic continuation instead of the exchange
        the user meant to take back.

        Raises ValueError when there are fewer user turns than requested.
        """
        from oaset.utils import atomic_write_text, file_lock

        # soft-delete: keep the pre-rewind transcript restorable
        backup = session.file.with_suffix(".jsonl.pre-rewind" + time.strftime("%Y%m%d-%H%M%S"))
        # Read AND rewrite inside one lock section: parsing from a snapshot read
        # before the lock let a turn appended in between be silently deleted —
        # the rewrite was built from stale lines.
        with file_lock(session.file):
            raw = session.file.read_text(encoding="utf-8")
            lines = raw.splitlines()
            if not lines:
                raise ValueError("session transcript is empty")
            meta_line, body = lines[0], lines[1:]
            parsed = []
            for line in body:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            user_positions = [
                i for i, e in enumerate(parsed)
                if e.get("type") == "message" and _is_user_turn_entry(e)
            ]
            if turns < 1 or len(user_positions) < turns:
                raise ValueError(f"only {len(user_positions)} turn(s) recorded")
            cutoff = user_positions[len(user_positions) - turns]
            kept = parsed[:cutoff]
            backup.write_text(raw, encoding="utf-8")
            atomic_write_text(
                session.file,
                meta_line + "\n" + "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept),
            )
        from oaset.agent.messages import Conversation

        session.conversation = Conversation()
        for e in kept:
            apply_transcript_entry(session.conversation, e)
        session.meta.message_count = len(session.conversation.messages)
        self._sync_index(session.meta)
        return len(session.conversation.messages)

    def set_title(self, session_id: str, title: str) -> bool:
        """Explicit /title: rewrite the meta line + index entry."""
        session = self.load(session_id)
        if session is None:
            return False
        session.meta.title = title
        assert session.file is not None, "loaded session must have a file"
        import json as _json

        from oaset.utils import atomic_write_text, file_lock, now_iso

        # Read AND rewrite inside one lock section: the rewrite is a full-file
        # replacement, so a turn appended between a pre-lock read and the write
        # used to be deleted from the live transcript.
        try:
            with file_lock(session.file):
                lines = session.file.read_text(encoding="utf-8").splitlines()
                if not lines:
                    return False
                try:
                    meta = _json.loads(lines[0])
                except _json.JSONDecodeError:
                    return False
                if meta.get("type") != "meta":
                    return False
                meta["title"] = title
                meta["updatedAt"] = now_iso()
                lines[0] = _json.dumps(meta, ensure_ascii=False)
                atomic_write_text(session.file, "\n".join(lines) + "\n")
        except OSError:
            return False
        self._sync_index(session.meta)
        return True

    # ---------------------------------------------------------------- append

    def append_message(self, session: Session, message) -> None:
        entry: dict[str, Any] = {"type": "message", "message": message.to_dict()}
        self._write_line(session.file, entry)
        session.meta.updated_at = now_iso()
        session.meta.message_count = len(session.conversation.messages)
        if not session.meta.title and message.role == "user" and message.content:
            text = message.content if isinstance(message.content, str) else " ".join(
                str(p.get("text", "")) for p in message.content if isinstance(p, dict))
            session.meta.title = " ".join(text.split())[:80]
        self._sync_index(session.meta)

    def append_usage(self, session: Session, usage: dict, model: str = "") -> None:
        payload = {k: int(v) for k, v in (usage or {}).items() if isinstance(v, (int, float))}
        self._write_line(
            session.file,
            {"type": "usage", "model": model, "at": now_iso(), **payload},
        )

    def last_usage(self, session: Session) -> dict | None:
        """The newest usage line of a session (cache fields included), or None.

        /context reads this to show real provider accounting instead of only
        estimates; a reverse scan keeps it cheap on long transcripts.
        """
        if session.file is None or not session.file.exists():
            return None
        for line in reversed(
                session.file.read_text(encoding="utf-8", errors="replace").splitlines()):
            if '"type": "usage"' not in line and '"type":"usage"' not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "usage":
                return entry
        return None

    def session_usage(self, session_id: str) -> dict:
        """Aggregate usage lines of one session."""
        session = self.load(session_id)
        totals = {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "entries": 0,
        }
        if session is None or session.file is None or not session.file.exists():
            return totals
        for line in session.file.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "usage":
                continue
            totals["entries"] += 1
            for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                        "cache_read_tokens", "cache_write_tokens"):
                totals[key] += int(entry.get(key, 0) or 0)
        return totals

    def usage_summary(self, session: Session) -> dict:
        """Session-wide token totals AND the newest usage line, one file read.

        /context wants both — the last turn's cache numbers say "is the
        prefix being reused right now", the session sums say "how much has
        caching saved me so far" — and reading the transcript twice for that
        doubles the stall on long sessions.
        """
        totals: dict[str, Any] = {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "entries": 0,
            "last": None,
        }
        if session.file is None or not session.file.exists():
            return totals
        for line in session.file.read_text(encoding="utf-8", errors="replace").splitlines():
            if '"type": "usage"' not in line and '"type":"usage"' not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "usage":
                continue
            totals["entries"] += 1
            for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                        "cache_read_tokens", "cache_write_tokens"):
                totals[key] += int(entry.get(key, 0) or 0)
            totals["last"] = entry
        return totals

    def append_evidence(self, session: Session, items: list[dict]) -> int:
        """Append /evidence receipts (one JSONL line per item)."""
        from oaset.utils import file_lock

        assert session.file is not None, "evidence requires a session file"
        written = 0
        with file_lock(session.file):
            for item in items:
                self._append_line_unlocked(
                    session.file,
                    {"type": "evidence", "at": now_iso(), **item},
                )
                written += 1
        return written

    def evidence_entries(self, session_id: str) -> list[dict]:
        """All evidence receipts of a session, oldest first."""
        meta = self._latest_meta(session_id)
        if meta is None or not meta.path.exists():
            return []
        entries: list[dict] = []
        for line in meta.path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "evidence":
                entries.append(entry)
        return entries

    def append_compaction(self, session: Session, summary: str, dropped: int) -> None:
        self._write_line(
            session.file,
            {
                "type": "compact",
                "summary": summary[:2000],
                "dropped": dropped,
                "compacted_at": now_iso(),
            },
        )

    # ------------------------------------------------------------ checkpoints

    def _ckpt_dirs(self, session: Session) -> tuple[Path, Path]:
        """(per-session, legacy) checkpoint directories.

        D-7: checkpoints used to live in ONE bucket-level directory shared by
        every session of the same working directory, so /undo in session B
        could restore a snapshot taken in session A. New checkpoints go to
        ``checkpoints/<session_id>/``; the legacy shared directory is still
        readable for sessions created before the split (until they make their
        first new checkpoint)."""
        assert session.file is not None, "checkpoints require a session file"
        base = session.file.parent / "checkpoints"
        return base / session.meta.session_id, base

    def _checkpoint_dir(self, session: Session) -> Path:
        primary, legacy = self._ckpt_dirs(session)
        return primary if primary.is_dir() else legacy

    def checkpoint(self, session: Session, snapshot: dict[str, str | None]) -> int:
        """Persist a pre-write snapshot {path: content | None(=did not exist)}."""
        from oaset.utils import atomic_write_text, file_lock

        assert session.file is not None, "checkpoint requires a session file"
        checkpoints, _legacy = self._ckpt_dirs(session)
        checkpoints.mkdir(parents=True, exist_ok=True)
        # Number + prune + write under one lock: two surfaces checkpointing
        # concurrently used to compute the same cp_N and one snapshot silently
        # overwrote the other (so /undo could restore the wrong one).
        with file_lock(checkpoints / ".seq"):
            numbers = [p.stem for p in checkpoints.glob("cp_*.json")]
            next_no = 1 + max((int(s.split("_")[1]) for s in numbers if s.split("_")[1].isdigit()), default=0)
            # keep the stack bounded: drop the oldest third once it grows past 20
            entries = sorted(checkpoints.glob("cp_*.json"))
            if len(entries) >= 20:
                for old in entries[: len(entries) - 15]:
                    old.unlink(missing_ok=True)
            atomic_write_text(checkpoints / f"cp_{next_no}.json",
                              json.dumps({"n": next_no, "at": now_iso(), "files": snapshot},
                                         ensure_ascii=False))
        return next_no

    def list_checkpoints(self, session: Session) -> list[dict[str, Any]]:
        """Checkpoints newest-first: {n, at, files: [path...], count}."""
        if session.file is None:
            return []
        checkpoints = self._checkpoint_dir(session)
        if not checkpoints.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for path in checkpoints.glob("cp_*.json"):
            parts = path.stem.split("_")
            if len(parts) < 2 or not parts[1].isdigit():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            files = list((data.get("files") or {}).keys())
            rows.append({"n": int(data.get("n", parts[1])),
                         "at": str(data.get("at", "")),
                         "files": files, "count": len(files),
                         "path": str(path)})
        return sorted(rows, key=lambda r: r["n"], reverse=True)

    def undo_checkpoint(self, session: Session, number: int) -> tuple[int, int] | None:
        """Restore one specific checkpoint (and drop it from the stack)."""
        if session.file is None:
            return None
        target = self._checkpoint_dir(session) / f"cp_{number}.json"
        if not target.is_file():
            return None
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        restored = 0
        for path_str, content in (data.get("files") or {}).items():
            destination = Path(path_str)
            try:
                if content is None:
                    destination.unlink(missing_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(content, encoding="utf-8")
                restored += 1
            except OSError:
                continue
        target.unlink(missing_ok=True)
        return int(data.get("n", number)), restored

    def undo_latest(self, session: Session) -> tuple[int, int] | None:
        """Restore the most recent checkpoint. Returns (checkpoint_no, files_restored)."""
        assert session.file is not None, "undo requires a session file"
        checkpoints = self._checkpoint_dir(session)
        entries = sorted(
            (p for p in checkpoints.glob("cp_*.json") if p.stem.split("_")[1].isdigit()),
            key=lambda p: int(p.stem.split("_")[1]),
        )
        if not entries:
            return None
        latest = entries[-1]
        try:
            data = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        restored = 0
        for path_str, content in (data.get("files") or {}).items():
            target = Path(path_str)
            try:
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                restored += 1
            except OSError:
                continue
        latest.unlink(missing_ok=True)
        return int(data.get("n", 0)), restored

    def delete_session(self, session_id: str, home: Path | None = None) -> bool:
        """删除会话：只移除该会话的转录文件与索引行（重写索引实现真删）。

        bucket 目录（wd_<name>_<hash8>）被同一工作目录的所有会话共享，
        绝不能整体 rmtree——只有当 bucket 内已无任何会话转录时才清理整个目录
        （此时其中的共享 checkpoints 也全部失去归属）。
        """
        meta, _matches = self._match_meta(session_id)
        if meta is None:
            return False
        bucket = meta.path.parent
        with contextlib.suppress(OSError):
            meta.path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            shutil.rmtree(bucket / "checkpoints" / meta.session_id, ignore_errors=True)
        with contextlib.suppress(OSError):
            if not any(bucket.glob("session_*.jsonl")):
                shutil.rmtree(bucket)
        self.rebuild_index()
        return True

    def export_markdown(self, session_id: str, out_path: Path) -> Path | None:
        """导出会话为 Markdown 转写（用户可读、可归档）。"""
        session = self.load(session_id)
        if session is None:
            return None
        from oaset.i18n import t

        lines = [t("export_title", sid=session.meta.session_id),
                 "",
                 t("export_dir", cwd=session.meta.cwd),
                 t("export_model", model=session.meta.model),
                 t("export_time", created=session.meta.created_at),
                 ""]
        for m in session.conversation.messages:
            role = {'user': t("export_user"), 'assistant': '## oAset',
                    'tool': t("export_tool")}.get(m.role)
            if role is None:
                continue
            text = (m.content or '') if not isinstance(m.content, list) else ' '.join(
                str(part.get('text', '')) for part in m.content if isinstance(part, dict))
            entry = role + '\n\n' + text + '\n'
            lines.append(entry)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text('\n'.join(lines), encoding='utf-8')
        return out_path

    def rebuild_index(self) -> None:
        """重写全局索引（删除会话后调用）。"""
        from oaset.utils import atomic_write_text, file_lock

        metas = self._all_metas()
        existing = {m.session_id for m in metas}
        # Atomic replacement under the lock: the old unlink→append window let a
        # concurrent reader see a missing or half-written index (every session
        # "disappearing" from the picker until the rebuild finished).
        lines: list[str] = []
        for sid in sorted(existing):
            m = self.load(sid)
            if m is None:
                continue
            meta = m.meta
            lines.append(json.dumps({
                "session_id": meta.session_id,
                "path": str(meta.path),
                "cwd": meta.cwd,
                "title": meta.title,
                "model": meta.model,
                "created_at": meta.created_at,
                "updated_at": meta.updated_at,
                "message_count": meta.message_count,
            }, ensure_ascii=False))
        with file_lock(self.index_path, timeout=10.0):
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.index_path, "".join(line + "\n" for line in lines))


    # ------------------------------------------------------------------ load

    def load(self, session_id: str) -> Session | None:
        meta = self._latest_meta(session_id)
        if meta is None or not meta.path.exists():
            return None
        from oaset.agent.messages import Conversation

        conversation = Conversation()
        try:
            for line in meta.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue  # corrupt line: skip, keep the rest
                try:
                    apply_transcript_entry(conversation, entry)
                except (KeyError, TypeError, ValueError):
                    continue  # well-formed JSON, malformed entry: skip too
        except OSError:
            return None
        meta.message_count = len(conversation.messages)
        return Session(meta=meta, conversation=conversation, file=meta.path)

    def latest_for_cwd(self, cwd: Path) -> Session | None:
        entries = self.list_sessions(cwd=str(cwd), limit=1)
        return self.load(entries[0].session_id) if entries else None

    # ------------------------------------------------------------------ list

    def list_sessions(self, cwd: str | None = None, limit: int = 20) -> list[SessionMeta]:
        metas = self._all_metas()
        if cwd:
            metas = [m for m in metas if m.cwd == str(cwd)]
        # _all_metas returns index-line order == true chronological order of the
        # last update per session (immune to clock-granularity ties)
        metas.reverse()
        return metas[:limit]

    def fork(self, session: Session) -> Session:
        """Copy full history into a new session (fork behavior)."""
        clone = self.new_session(
            Path(session.meta.cwd), session.meta.model, title=f"[fork] {session.meta.title}".strip()
        )
        from oaset.agent.messages import Conversation

        clone.conversation = Conversation(
            system_prompt=session.conversation.system_prompt,
            messages=[*session.conversation.messages],
        )
        for message in clone.conversation.messages:
            self._write_line(clone.file, {"type": "message", "message": message.to_dict()})
        clone.meta.message_count = len(clone.conversation.messages)
        self._sync_index(clone.meta)
        return clone

    # --------------------------------------------------------------- private

    def _write_line(self, path: Path | None, entry: dict[str, Any]) -> None:
        if path is None:
            return
        from oaset.utils import file_lock

        path.parent.mkdir(parents=True, exist_ok=True)
        # Cross-process append safety: the TUI, an ACP server and a one-shot
        # CLI can append to the same transcript/index concurrently.
        with file_lock(path):
            self._append_line_unlocked(path, entry)

    def _append_line_unlocked(self, path: Path, entry: dict[str, Any]) -> None:
        """Append while the CALLER holds the lock (batch appends stay atomic)."""
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _sync_index(self, meta: SessionMeta) -> None:
        self._write_line(
            self.index_path,
            {
                "session_id": meta.session_id,
                "path": str(meta.path),
                "cwd": meta.cwd,
                "title": meta.title,
                "model": meta.model,
                "created_at": meta.created_at,
                "updated_at": meta.updated_at,
                "message_count": meta.message_count,
            },
        )

    def _all_metas(self) -> list[SessionMeta]:
        latest: dict[str, tuple[int, SessionMeta]] = {}
        seq = 0
        if not self.index_path.exists():
            return []
        for line in self.index_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            meta = SessionMeta(
                session_id=entry["session_id"],
                path=Path(entry["path"]),
                cwd=entry.get("cwd", ""),
                title=entry.get("title", ""),
                model=entry.get("model", ""),
                created_at=entry.get("created_at", ""),
                updated_at=entry.get("updated_at", ""),
                message_count=int(entry.get("message_count", 0)),
            )
            seq += 1
            latest[meta.session_id] = (seq, meta)  # append-only index: last line wins
        return [meta for _, meta in sorted(latest.values(), key=lambda pair: pair[0])]

    def _match_meta(self, session_id: str) -> tuple[SessionMeta | None, int]:
        """(meta, match count) for a full id or an unambiguous short one.

        The sessions list prints ids the user can shorten, so `--resume 1f3a`
        and `/delete 1f3a` must behave the same. An exact id always wins; a
        suffix resolves only when it matches exactly one session — acting on
        the wrong transcript (resuming into it, or deleting it) is worse than
        asking for more digits.
        """
        exact = None
        partial: list[SessionMeta] = []
        for meta in self._all_metas():
            if meta.session_id == session_id:
                exact = meta
                break
            if meta.session_id.endswith(session_id):
                partial.append(meta)
        if exact is not None:
            return exact, 1
        if len(partial) == 1:
            return partial[0], 1
        return None, len(partial)

    def _latest_meta(self, session_id: str) -> SessionMeta | None:
        meta, _matches = self._match_meta(session_id)
        if meta is not None:
            return meta
        # fallback: direct file search (index may have been deleted)
        matches = sorted(self.base.rglob(f"{session_id}.jsonl"))
        if not matches:
            return None
        path = matches[0]
        return SessionMeta(session_id=session_id, path=path, cwd="")
