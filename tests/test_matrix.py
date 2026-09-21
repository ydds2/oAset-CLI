"""Matrix runs (parallel worktree agents) — the 2026 community wave, built in.

Each task gets its own worktree; the user's tree is never touched; failures
are isolated per task; results come back as a review table.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from oaset.config import default_config
from oaset.matrix import render_report, run_matrix
from oaset.providers import MockProvider

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=path, check=True, capture_output=True)
    return path


def _mock_cfg():
    cfg = default_config()
    cfg.default_provider = "mock"
    cfg.default_model = "mock/mock-echo"
    return cfg


async def test_matrix_runs_each_task_in_its_own_worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("OASET_HOME", str(tmp_path / "home"))
    repo = _git_repo(tmp_path / "repo")
    (repo / "dirty.txt").write_text("user's uncommitted work", encoding="utf-8")

    events: list[str] = []
    results = await run_matrix(
        _mock_cfg(), repo,
        [{"name": "docs", "prompt": "update docs"}, {"name": "tests", "prompt": "add tests"}],
        provider=MockProvider(), on_event=events.append,
    )
    assert len(results) == 2 and all(r["ok"] for r in results), results
    # each task lived in its OWN worktree under .oaset/worktrees/matrix-*
    names = {r["path"].name for r in results}
    assert names == {"matrix-docs", "matrix-tests"}, names
    assert all((repo / ".oaset" / "worktrees" / n).is_dir() for n in names)
    # the user's dirty tree is untouched and worktrees are KEPT for review
    assert (repo / "dirty.txt").exists()
    # progress events were reported, one done per task
    assert sum(1 for e in events if e.endswith(": done")) == 2
    # report table renders every task with its status
    report = render_report(results)
    assert "docs" in report and "tests" in report and " ok " in report


async def test_matrix_isolates_failures_and_orders_stably(tmp_path, monkeypatch):
    monkeypatch.setenv("OASET_HOME", str(tmp_path / "home"))
    repo = _git_repo(tmp_path / "repo")

    class BoomOnce(MockProvider):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def stream(self, messages, tools):
            self.calls += 1
            if "bad" in str(messages[-1].get("content") if hasattr(messages[-1], "get") else messages[-1]):
                raise RuntimeError("boom")
            yield {"type": "text", "text": "[mock] ok"}
            yield {"type": "turn_done"}

    provider = BoomOnce()
    results = await run_matrix(
        _mock_cfg(), repo,
        [{"name": "bad", "prompt": "this is the bad one"},
         {"name": "good", "prompt": "fine task"},
         {"name": "empty", "prompt": ""}],
        provider=provider,
    )
    assert [r["name"] for r in results] == ["bad", "empty", "good"], "stable order"
    by = {r["name"]: r for r in results}
    assert by["empty"]["ok"] is False and "empty prompt" in by["empty"]["head"]
    # the failing task reports, the batch still returns everything
    assert "bad" in by and "good" in by
