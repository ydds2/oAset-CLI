"""Batch trajectory runner: run many prompts headlessly,
saving each task's id/prompt/reply/error/elapsed to ``<out>/<id>.json``.
Scenario level, fully offline-capable.

    oaset batch --tasks tasks.jsonl --out runs [--model ID] [--mock] [--yolo]
Tasks file: one JSON object per line: {"id": "...", "prompt": "..."}

Notes on the contract and the trust boundary:
- ids come from external datasets and are SANITIZED into file names: a task
  id of ``../../etc/passwd`` or an absolute path used to write outside
  ``--out`` (and a collision silently overwrote the earlier result);
- a malformed line is reported as a failed task and the batch CONTINUES
  (one typo used to abort the remaining tasks with a traceback);
- without ``--yolo`` every write/shell tool is denied by policy. A task may
  therefore "succeed" with a reply that reports it could not do the work —
  the runner cannot tell intent, so watch ``error`` and the summary.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from oaset.agent import runner as _runner
from oaset.config import AppConfig

_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


def safe_task_stem(task_id: str, fallback: str) -> str:
    """A hostile or exotic task id must not escape the output directory.

    Keeps [A-Za-z0-9._-], strips path separators and leading dots so the
    result is a plain file name inside out_dir; empty results fall back.
    """
    stem = _SAFE_ID.sub("_", str(task_id)).strip("._")
    return stem[:80] or fallback


async def run_batch(
    cfg: AppConfig,
    cwd: Path,
    tasks_file: Path,
    out_dir: Path,
    model_id: str | None = None,
    yolo: bool = False,
    with_tools: bool = True,
    on_result=None,
    provider: Any = None,
) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    used_stems: set[str] = set()
    for raw in tasks_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        index = len(results) + 1
        try:
            task = json.loads(line)
            if not isinstance(task, dict):
                raise ValueError("task line must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            # one bad line used to abort the whole batch (results lost, exit
            # traceback); it is now a failed task like any other
            record = {"id": f"task_{index}", "prompt": line[:200],
                      "reply": "", "error": f"bad task line: {exc}",
                      "elapsed": 0.0}
            results.append(record)
            if on_result:
                on_result(record)
            continue
        task_id = str(task.get("id") or f"task_{index}")
        stem = safe_task_stem(task_id, f"task_{index}")
        if stem in used_stems:
            # same id twice silently overwrote the first result
            suffix = 2
            while f"{stem}_{suffix}" in used_stems:
                suffix += 1
            stem = f"{stem}_{suffix}"
        used_stems.add(stem)
        prompt = str(task.get("prompt", ""))
        started = time.monotonic()
        try:
            reply = await _runner.execute_prompt(
                cfg, cwd, prompt, model_id=model_id, provider=provider,
                yolo=yolo, with_tools=with_tools, persist=False,
            )
            error = None
        except Exception as exc:
            reply, error = "", f"{type(exc).__name__}: {exc}"
        record = {
            "id": task_id,
            "prompt": prompt,
            "reply": reply,
            "error": error,
            "elapsed": round(time.monotonic() - started, 2),
        }
        (out_dir / f"{stem}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        results.append(record)
        if on_result:
            on_result(record)
    return results
