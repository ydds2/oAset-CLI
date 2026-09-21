"""Always run THIS checkout. The global `oaset` command must never silently
fall back to a frozen Release exe in ~/.local/bin.

Used by ~/.local/bin/oaset.cmd (CMD) and ~/.local/bin/oaset (Git Bash).
Install / repair the global stubs with scripts/install_dev_launcher.ps1.
"""
from __future__ import annotations

import os
import runpy
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV_PY = REPO / ".venv" / "Scripts" / "python.exe"
SRC = REPO / "src"
GLOBAL_BIN = Path.home() / ".local" / "bin"
FROZEN_EXE = GLOBAL_BIN / "oaset.exe"
FROZEN_BACKUP = GLOBAL_BIN / "oaset.release.exe"


def _quarantine_frozen_exe() -> None:
    """CMD prefers oaset.exe over oaset.cmd. A Release install would shadow us."""
    if not FROZEN_EXE.is_file():
        return
    try:
        size = FROZEN_EXE.stat().st_size
    except OSError:
        return
    if size < 1_000_000:
        return  # tiny stub, not a PyInstaller build
    try:
        if FROZEN_BACKUP.exists():
            FROZEN_EXE.unlink()
        else:
            shutil.move(str(FROZEN_EXE), str(FROZEN_BACKUP))
    except OSError:
        pass


def main() -> None:
    _quarantine_frozen_exe()
    if not SRC.is_dir():
        sys.stderr.write(f"oaset: source tree missing: {SRC}\n")
        raise SystemExit(2)
    if Path(sys.executable).resolve() != VENV_PY.resolve() and VENV_PY.is_file():
        os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])
    sys.path.insert(0, str(SRC))
    sys.argv[0] = "oaset"
    runpy.run_module("oaset.cli", run_name="__main__")


if __name__ == "__main__":
    main()
