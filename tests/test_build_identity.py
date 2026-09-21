"""Build identity: one entry point, one fingerprint, no dev-dir dependencies.

The rules these tests defend (from the "stale artifacts" post-mortem):

- a release is a *stamped, packaged* build; a checkout is a development runtime
  and must say so instead of looking like a release;
- the fingerprint changes whenever the packaged code changes, so "same version,
  old behaviour" is visible instead of invisible;
- packaging refuses to produce an artifact that cannot identify itself;
- the packaged app must not need the development directory at runtime.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import stamp_build  # noqa: E402

from oaset import runtime_info  # noqa: E402

# ------------------------------------------------------------- fingerprint

def test_stamp_writes_a_fingerprint(monkeypatch, tmp_path):
    """The stamp records commit, time and a content digest."""
    target = tmp_path / "_build_info.py"
    monkeypatch.setattr(stamp_build, "TARGET", target)
    info = stamp_build.stamp(commit="a" * 40, built_at="2026-01-02T03:04:05Z")

    assert target.is_file()
    body = target.read_text(encoding="utf-8")
    assert "FINGERPRINT" in body and "BUILT_AT" in body
    assert info["built_at"] == "2026-01-02T03:04:05Z"
    assert info["fingerprint"].startswith("aaaaaaa-"), info["fingerprint"]
    assert len(info["content_sha256"]) == 64


def test_fingerprint_tracks_content_not_just_version(monkeypatch, tmp_path):
    """Two stamps of the same version differ when the code differs."""
    monkeypatch.setattr(stamp_build, "TARGET", tmp_path / "_build_info.py")
    first = stamp_build.stamp(commit="b" * 40, built_at="2026-01-02T03:04:05Z")

    real_digest = stamp_build._content_digest
    monkeypatch.setattr(stamp_build, "_content_digest", lambda: "f" * 64)
    second = stamp_build.stamp(commit="b" * 40, built_at="2026-01-02T03:04:05Z")
    monkeypatch.setattr(stamp_build, "_content_digest", real_digest)

    assert first["fingerprint"] != second["fingerprint"]
    assert first["content_sha256"] != second["content_sha256"]


def test_packaging_refuses_without_a_stamp(tmp_path):
    """An anonymous artifact must not be buildable."""
    spec = (ROOT / "oaset.spec").read_text(encoding="utf-8")
    assert "_build_info.py" in spec
    assert "refusing to package without a build stamp" in spec


# ---------------------------------------------------------- running source

def test_dev_runtime_is_labelled_not_a_release():
    """Tests run from the checkout, so the runtime must say 'dev'."""
    info = runtime_info.running_source()
    assert info["kind"] == "dev", info
    text = runtime_info.describe_source()
    assert "dev" in text
    assert "NOT a release build" in text


def test_stamp_alone_does_not_make_a_release():
    """A stamped dev tree is still raw files, not a published build."""
    # the real stamp may exist locally; the frozen flag is what decides
    assert not getattr(sys, "frozen", False)
    assert runtime_info.running_source()["kind"] != "release"


# ------------------------------------------------- no dev-directory reads

def test_no_package_data_files_are_required_at_runtime():
    """Runtime data must be embedded, not read from a checkout.

    Built-in skill Markdown is shipped inside the package (and listed in
    oaset.spec); anything else non-code still needs a packaging hook.
    """
    package = Path(runtime_info.__file__).resolve().parent
    extras = [
        str(p.relative_to(package))
        for p in package.rglob("*")
        if p.is_file()
        and p.suffix not in (".py", ".pyc")
        and "__pycache__" not in p.parts
        and p.name != "_build_info.py"
        and not (p.name == "SKILL.md" and "skills" in p.parts and "builtin" in p.parts)
    ]
    assert not extras, f"packaged data would need embedding: {extras}"


def test_themes_and_strings_come_from_code():
    """Built-in themes and i18n live in modules, not in loadable files."""
    from oaset.i18n import CATALOG
    from oaset.tui.themes import THEMES

    assert CATALOG and THEMES, "both must be importable without reading the disk"


def test_public_github_identity_is_the_real_remote():
    """README, pyproject Homepage and User-Agent must name the same repo as origin."""
    homepage = "https://github.com/ydds2/oAset-CLI"
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f'Homepage = "{homepage}"' in pyproject
    assert homepage in readme
    assert "github.com/oAset-CLI/oAset-CLI" not in pyproject
    assert "github.com/oaset-cli)" not in (ROOT / "src" / "oaset" / "tools" / "web.py").read_text(encoding="utf-8")


def test_wheel_builds_and_ships_builtin_skills_once(tmp_path):
    """Open-source gate: `python -m build --wheel` must succeed and the
    builtin skills must ship EXACTLY once. The old force-include table
    added every SKILL.md a second time and the build died the moment a
    new skill directory appeared (caught with idea-forge)."""
    import glob
    import subprocess
    import sys
    import zipfile

    out = tmp_path / "dist"
    r = subprocess.run([sys.executable, "-m", "build", "--wheel",
                        "-o", str(out)],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-800:]
    wheels = glob.glob(str(out / "*.whl"))
    assert wheels, "no wheel produced"
    names = zipfile.ZipFile(wheels[0]).namelist()
    skills = [n for n in names if "skills/builtin" in n]
    assert any("idea-forge" in n for n in skills)
    assert any("ship-complete" in n for n in skills)
    assert len(names) == len(set(names)), "duplicate paths in wheel"
