"""The offline task eval is a version gate (docs/plans/release-distribution
Task 8 Step 5): every task in eval/tasks.jsonl must pass in-process, so a
pipeline regression cannot merge behind a green unit suite.

Run standalone via `python scripts/run_eval.py --offline` for the full table.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_eval  # noqa: E402


def test_eval_task_file_defines_at_least_twenty_tasks():
    tasks = run_eval.load_tasks()
    assert len(tasks) >= 20, "the eval set must keep growing, not shrink"
    ids = [t["id"] for t in tasks]
    assert len(ids) == len(set(ids)), "duplicate task ids"


def test_all_offline_eval_tasks_pass():
    failures: list[str] = []

    async def run_all():
        for task in run_eval.load_tasks():
            ok, detail = await run_eval.run_task(task)
            if not ok:
                failures.append(f"{task['id']}: {detail}")

    import asyncio

    asyncio.run(run_all())
    assert not failures, "offline eval failures: " + "; ".join(failures)
