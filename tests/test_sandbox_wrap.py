"""Sandbox argv builders are pure functions — testable on any dev machine.

The Linux bwrap / macOS Seatbelt wrappers can only be SMOKE-tested on their
own OSes; these tests pin the exact argv shapes (and the no-binary fallback)
by injecting platform + a fake `which`, so a Windows dev box still verifies
the POSIX sandbox construction.
"""

from __future__ import annotations

import sys

from oaset.sandbox import build_posix_wrap, wrap_windows_command


def test_linux_bwrap_shape():
    argv = build_posix_wrap(
        ["bash", "-c", "make test"], "C:\\repo", "linux",
        which=lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    assert argv[0] == "/usr/bin/bwrap"
    assert "--die-with-parent" in argv
    # the workspace is the ONLY rw bind; the rest of / is read-only
    i = argv.index("--bind")
    assert argv[i + 1] == argv[i + 2]
    assert str(argv[i + 1]).endswith("repo")
    i = argv.index("--ro-bind")
    assert argv[i + 1] == argv[i + 2] == "/"
    assert argv[-3:] == ["bash", "-c", "make test"]


def test_linux_without_bwrap_falls_back_untouched():
    argv = build_posix_wrap(["ls"], "/repo", "linux", which=lambda _n: None)
    assert argv == ["ls"]


def test_darwin_seatbelt_profile_pins_the_workspace():
    argv = build_posix_wrap(
        ["make"], "C:\\Users\\me\\work", "darwin",
        which=lambda name: "/usr/bin/sandbox-exec" if name == "sandbox-exec" else None,
    )
    assert argv[0] == "/usr/bin/sandbox-exec"
    profile = argv[2]
    assert '(allow file-write* (subpath "C:\\Users\\me\\work"))' in profile
    assert "(deny file-write*)" in profile  # everything else: no writes


def test_windows_shim_uses_module_form_in_source_builds():
    argv = wrap_windows_command(["cmd", "/c", "echo hi"])
    assert argv[0] == sys.executable
    assert argv[1:4] == ["-m", "oaset.sandboxexec", "--"]
    assert argv[4:] == ["cmd", "/c", "echo hi"]
