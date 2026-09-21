"""Architecture tests (docs/plans/tui.zh-CN.md Task 6 Step 1): the seams of
the split must hold without anyone re-checking them by hand.

- command registry entries all resolve to real handlers on OasetApp
- controllers do not import each other and never import the app module
  (they receive the host through inheritance, not a reverse dependency)
- tui/turn_view.py receives the app as a parameter and imports nothing
  from it (same rule for the render dispatch)

(The app.py line budget was retired by owner decision on 2026-09-14;
structure is guarded by the rules above instead of a line count.)
"""

from __future__ import annotations

import ast
from pathlib import Path

CONTROLLERS = Path("src/oaset/tui/controllers")
TURN_VIEW = Path("src/oaset/tui/turn_view.py")
APP = Path("src/oaset/tui/app.py")


def test_every_registered_command_has_a_handler():
    from oaset.tui.app import OasetApp
    from oaset.tui.commands import COMMANDS

    missing = [cmd.name for cmd in COMMANDS
               if not callable(getattr(OasetApp, cmd.handler_name, None))]
    assert not missing, f"commands without handlers: {missing}"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_controllers_do_not_import_each_other():
    """A controller importing a sibling couples domains that were split apart;
    shared helpers belong in utils or the app itself."""
    files = sorted(CONTROLLERS.glob("*.py"))
    assert len(files) >= 6, "the controller split went missing?"
    for path in files:
        if path.name == "__init__.py":
            continue
        siblings = _imports(path) & {
            f"oaset.tui.controllers.{f.stem}" for f in files
            if f.stem != path.stem and f.name != "__init__.py"}
        assert not siblings, f"{path.name} imports sibling controllers: {siblings}"


def test_controllers_and_turn_view_never_import_the_app():
    """The host flows DOWN through inheritance / parameters; importing
    oaset.tui.app from below would be a circular dependency."""
    for path in [*CONTROLLERS.glob("*.py"), TURN_VIEW]:
        mods = _imports(path)
        assert "oaset.tui.app" not in mods, (
            f"{path.name} must not import oaset.tui.app (reverse dependency)")


def test_command_registry_is_the_single_alias_source():
    """No module may keep its own alias table — the registry is the source."""
    for path in [*CONTROLLERS.glob("*.py"), APP, TURN_VIEW]:
        src = path.read_text(encoding="utf-8")
        assert "ALIASES =" not in src and "alias_table" not in src, (
            f"{path.name} defines its own alias table; use tui/commands.py")
