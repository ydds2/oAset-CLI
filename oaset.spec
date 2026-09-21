# PyInstaller spec: single-file oaset.exe (textual + providers bundled)
# Build (CI only):  python scripts/stamp_build.py && pyinstaller --clean oaset.spec
# Output: dist/oaset.exe — copy anywhere on PATH; config lives in ~/.oaset/
#
# A build without the stamp would be an artifact that cannot say which build it
# is, so packaging refuses to continue instead of shipping an anonymous exe.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

_stamp = Path("src/oaset/_build_info.py")
if not _stamp.is_file():
    raise SystemExit(
        "refusing to package without a build stamp: run "
        "`python scripts/stamp_build.py` first (CI does this automatically)."
    )
_fingerprint = ""
for _line in _stamp.read_text(encoding="utf-8").splitlines():
    if _line.startswith("FINGERPRINT ="):
        _fingerprint = _line.split("=", 1)[1].strip().strip("'\"")
        break
print(f"packaging fingerprint: {_fingerprint}")
if not _fingerprint:
    raise SystemExit("the build stamp has no fingerprint")

hiddenimports = (
    collect_submodules("textual")
    + collect_submodules("oaset")
    + ["textual.document._document", "textual.document._syntax_aware_document"]
)
datas = collect_data_files("textual")
_builtin = Path("src/oaset/skills/builtin")
for _skill in _builtin.rglob("SKILL.md"):
    datas.append((str(_skill), str(_skill.parent.relative_to("src"))))

a = Analysis(
    ["src/oaset/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="oaset",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
