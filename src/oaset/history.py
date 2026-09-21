"""Cross-session search: SQLite FTS5 index over all session transcripts.

The "FTS5 session search with LLM summarization for
cross-session recall": the agent can search its own past conversations
(`search_history` tool) and the user can browse hits via `/search`.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from oaset.tools.base import READ, Tool, ToolContext, ToolResult
from oaset.utils import oaset_home, one_line, truncate_text, workdir_bucket

MAX_SNIPPET = 160


@dataclass
class HistoryHit:
    session_id: str
    path: str
    title: str
    role: str
    snippet: str


def _db_path(home: Path | None = None) -> Path:
    return (home or oaset_home()) / "history.db"


def _connect(home: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(home))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS entries ("
        " session_id TEXT, line_no INTEGER, role TEXT, title TEXT, path TEXT, text TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entries_session ON entries(session_id, line_no)"
    )
    # multi-process (TUI /search + the agent tool + ACP): WAL + a real busy
    # wait, or the default 5s lock error surfaced as random tool failures
    with contextlib.suppress(Exception):
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _fts_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5("
                     "session_id UNINDEXED, line_no UNINDEXED, role UNINDEXED,"
                     " title UNINDEXED, path UNINDEXED, text)")
        return True
    except sqlite3.OperationalError:
        return False


def _trigram_available(conn: sqlite3.Connection) -> bool:
    """Second index: trigram tokenizer enables CJK substring search."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_cjk USING fts5("
                     "session_id UNINDEXED, line_no UNINDEXED, role UNINDEXED,"
                     " title UNINDEXED, path UNINDEXED, text, tokenize='trigram')")
        return True
    except sqlite3.OperationalError:
        return False


def index_session(session_path: Path, home: Path | None = None, force: bool = False) -> int:
    """Index one session transcript; returns number of indexed messages."""
    session_path = Path(session_path)
    if not session_path.is_file():
        return 0
    conn = _connect(home)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS indexed (path TEXT PRIMARY KEY, mtime REAL, lines INTEGER)")
        stat = session_path.stat()
        row = conn.execute("SELECT mtime, lines FROM indexed WHERE path=?", (str(session_path),)).fetchone()
        if row and not force and abs(row[0] - stat.st_mtime) < 1 and row[1] > 0:
            return 0  # unchanged since last index
        session_id = session_path.stem
        entries: list[tuple] = []
        title = ""
        for line_no, line in enumerate(session_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "meta":
                title = str(entry.get("title", "")) or title
                continue
            if entry.get("type") != "message":
                continue
            message = entry.get("message", {})
            text = message.get("content")
            if isinstance(text, list):  # multipart (images) — keep text parts only
                text = " ".join(str(p.get("text", "")) for p in text if isinstance(p, dict) and p.get("type") == "text")
            if not text or message.get("role") == "tool":
                continue
            entries.append((session_id, line_no, str(message.get("role")), title, str(session_path), str(text)))
        if not _fts_available(conn):
            return 0
        rows = [(sid, ln, role, title, path, text[:8000]) for sid, ln, role, title, path, text in entries]
        conn.execute("DELETE FROM search WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM indexed WHERE path=?", (str(session_path),))
        conn.executemany("INSERT INTO search VALUES (?,?,?,?,?,?)", rows)
        if _trigram_available(conn):
            conn.execute("DELETE FROM search_cjk WHERE session_id=?", (session_id,))
            conn.executemany("INSERT INTO search_cjk VALUES (?,?,?,?,?,?)", rows)
        conn.execute("INSERT INTO indexed VALUES (?,?,?)", (str(session_path), stat.st_mtime, len(entries)))
        conn.commit()
        return len(entries)
    finally:
        conn.close()


def index_all(home: Path | None = None, sessions_dir: Path | None = None) -> int:
    """(Re)index every recorded session. Returns total messages indexed this run."""
    base = sessions_dir or (home or oaset_home()) / "sessions"
    total = 0
    if not base.is_dir():
        return 0
    for transcript in base.rglob("session_*.jsonl"):
        total += index_session(transcript, home)
    return total


def search_history(
    query: str,
    home: Path | None = None,
    limit: int = 10,
    workspace: Path | None = None,
) -> list[HistoryHit]:
    """Full-text search across indexed sessions (empty query → latest sessions).

    `workspace` restricts hits to the sessions recorded under that working
    directory's bucket — the agent-facing tool passes its cwd so one project's
    conversations never leak into another's context. Omit it for an explicit,
    user-driven search over all history (TUI /search)."""
    index_all(home)
    conn = _connect(home)
    scope_sql, scope_params = "", []
    if workspace is not None:
        bucket = workdir_bucket(Path(workspace))
        # transcript paths store the OS-native separator; match both
        scope_sql = " AND (path LIKE ? OR path LIKE ?)"
        scope_params = [f"%{bucket}\\%", f"%{bucket}/%"]
    try:
        if not _fts_available(conn):
            return []
        if query.strip():
            has_cjk = any("一" <= ch <= "鿿" for ch in query)
            if has_cjk and _trigram_available(conn):
                # trigram index: CJK substring match without tokenization
                rows = conn.execute(
                    "SELECT session_id, title, role, path, snippet(search_cjk, 5, '＜', '＞', '…', 12) "
                    f"FROM search_cjk WHERE search_cjk MATCH ?{scope_sql} ORDER BY rank LIMIT ?",
                    (f'"{query.replace(chr(34), " ")}"', *scope_params, limit),
                ).fetchall()
            else:
                sanitized = " ".join(
                    f'"{token}"' for token in query.replace('"', " ").split()
                )  # phrase-quote each token: keeps FTS5 syntax safe
                rows = conn.execute(
                    "SELECT session_id, title, role, path, snippet(search, 5, '＜', '＞', '…', 12) "
                    f"FROM search WHERE search MATCH ?{scope_sql} ORDER BY rank LIMIT ?",
                    (sanitized, *scope_params, limit),
                ).fetchall()
        else:
            rows = conn.execute(
                "SELECT session_id, title, role, path, text FROM search "
                f"WHERE 1=1{scope_sql} "
                "ORDER BY line_no DESC LIMIT ?", (*scope_params, limit),
            ).fetchall()
        return [
            HistoryHit(session_id=r[0], title=r[1] or "(untitled)", role=r[2], path=r[3], snippet=one_line(r[4], MAX_SNIPPET))
            for r in rows
        ]
    finally:
        conn.close()


def load_session_messages(session_path: str, home: Path | None = None, max_chars: int = 12000) -> list[dict]:
    """Messages of one session for LLM summarization."""
    path = Path(session_path)
    if not path.is_file():
        return []
    out: list[dict] = []
    budget = max_chars
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "message":
            continue
        message = entry.get("message", {})
        text = message.get("content")
        if isinstance(text, list):
            text = " ".join(str(p.get("text", "")) for p in text if isinstance(p, dict) and p.get("type") == "text")
        if not text:
            continue
        chunk = f"{message.get('role')}: {text}"
        budget -= len(chunk)
        out.append({"role": "user", "content": chunk if budget > 0 else chunk[:200]})
        if budget <= -2000:
            break
    return out


class SearchHistoryTool(Tool):
    name = "search_history"
    description = (
        "Full-text search over past sessions recorded in THIS workspace "
        "(your own conversation history here). Use to recall prior decisions, "
        "files touched, or how a problem was solved before."
    )
    permission = READ
    required = ["query"]
    parameters = {
        "query": {"type": "string", "description": "Keywords to search for (empty lists recent messages)"},
        "limit": {"type": "integer", "description": "Max hits (default 8)"},
    }

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        query = str(args.get("query", ""))
        limit = min(20, max(1, int(args.get("limit") or 8)))
        hits = search_history(query, limit=limit, workspace=ctx.cwd)
        if not hits:
            return ToolResult(f"No past conversation matches '{query}'.")
        lines = [
            f"[{i}] {h.title} ({h.session_id[-8:]}, {h.role}) {h.snippet}"
            for i, h in enumerate(hits, 1)
        ]
        return ToolResult(truncate_text(
            f"{len(hits)} hit(s) in past sessions:\n" + "\n".join(lines), ctx.output_limit
        ))
