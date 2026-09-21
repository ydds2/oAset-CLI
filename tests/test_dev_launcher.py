"""The global `oaset` command must always run THIS checkout, never a frozen exe."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "run_oaset.py"
_spec = importlib.util.spec_from_file_location("oaset_dev_launcher", _LAUNCHER)
launcher = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(launcher)


def test_quarantine_moves_frozen_exe_aside(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    frozen = bin_dir / "oaset.exe"
    frozen.write_bytes(b"MZ" + b"\0" * 1_000_001)
    backup = bin_dir / "oaset.release.exe"
    monkeypatch.setattr(launcher, "FROZEN_EXE", frozen)
    monkeypatch.setattr(launcher, "FROZEN_BACKUP", backup)
    launcher._quarantine_frozen_exe()
    assert not frozen.exists()
    assert backup.exists() and backup.stat().st_size > 1_000_000


def test_quarantine_does_not_touch_tiny_stubs(tmp_path, monkeypatch):
    stub = tmp_path / "oaset.exe"
    stub.write_bytes(b"MZ tiny")
    monkeypatch.setattr(launcher, "FROZEN_EXE", stub)
    monkeypatch.setattr(launcher, "FROZEN_BACKUP", tmp_path / "oaset.release.exe")
    launcher._quarantine_frozen_exe()
    assert stub.exists()


def test_repo_src_is_the_launcher_source():
    assert launcher.SRC == Path(__file__).resolve().parents[1] / "src"
    assert (launcher.SRC / "oaset" / "cli.py").is_file()
