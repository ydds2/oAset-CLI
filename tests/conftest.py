"""Shared fixtures: isolated OASET_HOME per test, no live network."""

from __future__ import annotations

import pytest

from oaset.config import default_config
from oaset.i18n import set_language
from oaset.network import is_loopback_url


class LiveNetworkBlocked(BaseException):
    """A test tried to send a request to a non-loopback host.

    Derives from BaseException deliberately: provider code treats any
    ``Exception`` as "offline, fall back to defaults", so an ordinary
    exception here would be swallowed and the test would pass while quietly
    depending on the internet — which is how a 6s timeout showed up as an
    unrelated "widget never mounted" flake. BaseException gets through those
    handlers and fails the test at the point of the violation.
    """


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    """Unit tests may reach this machine and mocked transports — never the internet.

    Guarding ``send`` rather than the constructor keeps provider construction
    (which builds a real client object eagerly, but sends nothing) working,
    while every actual request is checked. ``httpx.MockTransport`` stays
    available, and so do the localhost suites (gateway, mock server) and the
    built-in ``mock://`` provider: what is forbidden is an outbound connection
    to somewhere else on the network.
    """
    httpx = pytest.importorskip("httpx")

    def check(client, request) -> None:
        # the product's own notion of "this machine": loopback hosts and the
        # built-in mock:// provider scheme (oaset.network.is_loopback_url)
        if is_loopback_url(str(request.url)):
            return
        try:
            transport = client._transport_for_url(request)
        except Exception:
            transport = None
        if isinstance(transport, httpx.MockTransport):
            return
        raise LiveNetworkBlocked(
            f"{type(client).__name__} would talk to {request.url!r}: tests must "
            "inject a MockTransport or stub the caller, never go live")

    real_send = httpx.Client.send
    real_async_send = httpx.AsyncClient.send

    def guarded_send(self, request, *args, **kwargs):
        check(self, request)
        return real_send(self, request, *args, **kwargs)

    async def guarded_async_send(self, request, *args, **kwargs):
        check(self, request)
        return await real_async_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", guarded_send)
    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_async_send)


@pytest.fixture(autouse=True)
def default_language():
    """Pin the ambient i18n language per test.

    zh is the product default (config default ui_language). Without this,
    a TUI test that sets English leaks into later unit tests whose
    assertions legitimately expect the default-language rendering."""
    set_language("zh")
    yield
    set_language("zh")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point OASET_HOME at a temp dir so tests never touch the real ~/.oaset."""
    home = tmp_path / "oaset-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OASET_HOME", str(home))
    return home


@pytest.fixture
def cfg():
    return default_config()


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def pytest_addoption(parser):
    parser.addoption("--runreal", action="store_true", default=False,
                     help="run real-input Windows GUI tests (cursor/keyboard synthesis)")


def pytest_collection_modifyitems(config, items):
    """Real-input tests are excluded from the default run: they synthesize
    global mouse/keyboard events and must run in a dedicated invocation
    (`pytest -m real_input`) on an unlocked interactive desktop."""
    import pytest as _pytest

    if config.getoption("--runreal"):
        return
    skip_real = _pytest.mark.skip(reason="real-input suite: run with --runreal")
    for item in items:
        if "real_input" in item.keywords:
            item.add_marker(skip_real)


async def wait_for_inline_input(pilot, app, timeout: float = 10.0):
    """Wait until exactly one *visible* InlineInput is mounted with its field.

    Textual mounts a widget and its composed children in separate cycles, so
    querying ``#inline-input-field`` right after the parent appears can raise
    NoMatches on a slower runner (seen on the windows-3.11 CI job). Requiring
    exactly one prompt also avoids typing into a previous prompt that is still
    being torn down — a prompt that has already resolved (``display = False``)
    is skipped rather than counted, since it no longer shows the user anything.

    The timeout is a backstop, not a budget: a step that needs seconds of wall
    clock (a live request, a probe) belongs stubbed in the test, not waited on
    here. A failure reports what was actually mounted so the next reader can
    tell "the app stalled" from "the field never composed".
    """
    import time as _time

    deadline = _time.monotonic() + timeout
    seen: list[str] = []
    while _time.monotonic() < deadline:
        prompts = [p for p in app.query("InlineInput") if p.display]
        seen = [f"{type(p).__name__}(display={p.display})"
                for p in app.query("InlinePromptBase")]
        if len(prompts) == 1:
            try:
                field = prompts[0].query_one("#inline-input-field")
            except Exception:
                field = None
            if field is not None:
                return field
        await pilot.pause(0.05)
    raise AssertionError(
        f"no visible inline input became ready within {timeout:g}s; "
        f"prompts mounted: {seen or 'none'}")


@pytest.fixture(scope="session")
def host_shell_unavailable_reason():
    """Non-empty when this HOST cannot run a trivial shell command.

    CI's Windows runner has been observed to start the configured shell but
    have it die instantly with an unsigned -1 and empty output — an
    environment property, not a product state. Tests whose subject IS the
    shell (timeout kill, sandbox) skip with this reason instead of failing
    on infrastructure; the same tests run for real on a dev machine.
    """
    import subprocess
    import sys

    if sys.platform != "win32":
        return ""
    try:
        from oaset.tools.shell import shell_command_line

        r = subprocess.run(shell_command_line("echo oaset-probe"),
                           capture_output=True, timeout=60)
    except Exception as exc:  # pragma: no cover - host-dependent
        return f"shell probe crashed: {exc}"
    if r.returncode != 0 or b"oaset-probe" not in r.stdout:
        return (f"host shell cannot run a trivial command (rc={r.returncode}, "
                f"out={r.stdout[:80]!r}, err={r.stderr[:80]!r})")
    return ""


@pytest.fixture(scope="session")
def restricted_spawn_unavailable_reason():
    """Non-empty when the restricted-token shim cannot spawn here.

    The deny-only-SID sandbox needs token privileges the hosted runner does
    not grant (fail-closed shim exits non-zero with empty streams). Sandbox
    tests skip with this reason; they are verified on real Windows.
    """
    import subprocess
    import sys

    if sys.platform != "win32":
        return ""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "oaset.sandboxexec", "--", "cmd", "/c", "exit 0"],
            capture_output=True, timeout=60)
    except Exception as exc:  # pragma: no cover - host-dependent
        return f"restricted-token probe crashed: {exc}"
    if r.returncode != 0:
        return (f"restricted-token spawn unavailable here (rc={r.returncode}, "
                f"err={r.stderr[:120]!r})")
    return ""
