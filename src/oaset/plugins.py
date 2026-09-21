"""Plugin system: drop Python plugins into ~/.oaset/plugins/ or ./.oaset/plugins/.

Each plugin module defines `register(api)`:

    def register(api):
        api.add_command("hello", "Say hello", lambda app, args: app.chat.add_notice("hi!"))
        api.add_tool(MyTool())          # any oaset Tool instance

Trust model: plugins under the USER directory (~/.oaset/plugins) are managed
by the user directly and load by default. Plugins inside a PROJECT directory
(<cwd>/.oaset/plugins) execute with full user privileges but come from the
repo you happen to be in — they are skipped until explicitly trusted:

    oaset trust-plugin <name>

The trust record pins the file's sha256; a modified plugin is skipped again
until re-trusted. `oaset update apply` installs into the USER directory and
is therefore unaffected.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oaset.i18n import t
from oaset.utils import oaset_home

MAX_PLUGINS = 32


@dataclass
class PluginCommand:
    name: str
    description: str
    handler: Callable[[Any, str], Any]  # async or sync (app, args)


@dataclass
class LoadedPlugin:
    """What one plugin contributed — the rollback record for reload."""

    name: str
    module_name: str
    tools: list[str] = field(default_factory=list)      # registry tool names
    commands: list[str] = field(default_factory=list)   # slash command names
    manifest: dict[str, Any] = field(default_factory=dict)  # validated OASET_MANIFEST


MANIFEST_KEYS = ("version", "description", "requires_oaset", "permissions", "api")


def _validate_manifest(plugin: str, raw: Any) -> dict[str, Any]:
    """OASET_MANIFEST schema: dict with known keys, sane types. Unknown keys
    are kept but reported; wrong types are rejected (plugin still loads —
    a bad manifest must not brick the host, but it IS visible)."""
    if not isinstance(raw, dict):
        raise ValueError("OASET_MANIFEST must be a dict")
    out: dict[str, Any] = {}
    unknown = []
    for key, value in raw.items():
        if key not in MANIFEST_KEYS:
            unknown.append(key)
            out[key] = value
            continue
        if key in ("version", "description", "requires_oaset", "api") and value is not None \
                and not isinstance(value, str):
            raise ValueError(f"manifest '{key}' must be a string")
        if key == "permissions" and value is not None and not isinstance(value, (list, tuple)):
            raise ValueError("manifest 'permissions' must be a list")
        out[key] = value
    if unknown:
        out["_unknown_keys"] = unknown
    return out


def __oaset_version__() -> str:
    from oaset import __version__

    return str(__version__)


def trust_path() -> Path:
    return oaset_home() / "plugin_trust.json"


def plugin_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_trust() -> dict[str, str]:
    path = trust_path()
    if not path.exists():
        return {}
    with contextlib.suppress(ValueError, OSError):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    return {}


def is_trusted(name: str, path: Path) -> bool:
    """A plugin is trusted when the trust record exists AND matches the
    current file hash — edits invalidate trust."""
    record = _load_trust().get(name)
    return record is not None and record == plugin_fingerprint(path)


def is_valid_plugin_name(name: str) -> bool:
    """Only a bare stem is a valid target: no slashes, traversal or emptiness."""
    return bool(name) and Path(name).name == name and "/" not in name and "\\" not in name and ".." not in name


def trust_plugin_file(path: Path) -> str:
    """Pin a plugin file's hash into the trust record. Returns the hash."""
    name = path.stem
    digest = plugin_fingerprint(path)
    record = _load_trust()
    record[name] = digest
    from oaset.utils import atomic_write_text, file_lock

    # read-modify-write under one lock: two surfaces trusting plugins at once
    # used to swap tmp files and pair a name with the WRONG hash
    with file_lock(trust_path()):
        trust_path().parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(trust_path(), json.dumps(record, ensure_ascii=False, indent=2))
    return digest


class PluginAPI:
    """Surface handed to each plugin's register(). Records contributions
    so a later reload can retract exactly this plugin's additions.

    Capability isolation (SUPPLY-01): when the plugin's OASET_MANIFEST
    declares `permissions`, that list is an ALLOWLIST — `add_tool` requires
    "tools", `add_command` requires "commands"; anything undeclared raises
    PermissionError instead of silently running with host rights. A plugin
    without a manifest keeps the legacy unrestricted surface but is visible
    as unrestricted in /plugins."""

    def __init__(self, name: str, registry, extra_commands: dict[str, PluginCommand], log,
                 record: LoadedPlugin | None = None,
                 permissions: tuple[str, ...] | None = None):
        self.name = name
        self._registry = registry
        self._extra_commands = extra_commands
        self._log = log
        self._record = record
        self.permissions: tuple[str, ...] | None = (
            tuple(permissions) if permissions is not None else None)

    def _requires(self, capability: str) -> None:
        if self.permissions is None:
            return  # no manifest: legacy surface (visible as unrestricted)
        if capability not in self.permissions:
            self._log(f"plugin {self.name}: " + t("plug_cap_denied", name=self.name, capability=capability, declared=', '.join(self.permissions) or t("plug_none")))
            raise PermissionError(
                f"plugin {self.name!r} lacks manifest permission {capability!r}")

    def add_tool(self, tool) -> None:
        self._requires("tools")
        try:
            from oaset.tools import default_tools

            if tool.name in {t.name for t in default_tools()}:
                # a plugin shadowing read_file/shell replaces the trust
                # boundary core; refused outright
                self._log(t("plug_reserved_tool", name=self.name, tool=tool.name))
                raise PermissionError(
                    f"plugin {self.name!r} cannot override built-in tool {tool.name!r}")
        except ImportError:
            pass
        self._registry.add_tool(tool)
        if self._record is not None:
            self._record.tools.append(tool.name)
        self._log(f"plugin {self.name}: tool '{tool.name}' registered")

    def add_command(self, name: str, description: str, handler) -> None:
        self._requires("commands")
        name = name.lower()
        try:
            from oaset.tui.commands import COMMANDS, alias_map

            reserved = {c.name for c in COMMANDS} | set(alias_map())
        except Exception:
            reserved = set()
        if name in reserved:
            # A plugin /exit would run on the typing path while the palette
            # showed the builtin, and the builtin's danger confirmation was
            # skipped. Reserved names stay reserved.
            self._log(t("plug_reserved_command", name=self.name, command=name))
            raise PermissionError(f"plugin {self.name!r} cannot shadow built-in command /{name}")
        self._extra_commands[name] = PluginCommand(name, description, handler)
        if self._record is not None:
            self._record.commands.append(name)
        self._log(f"plugin {self.name}: command '/{name}' registered")

    def log(self, text: str) -> None:
        self._log(f"[{self.name}] {text}")


def plugin_dirs(cwd: Path) -> list[Path]:
    return [oaset_home() / "plugins", Path(cwd) / ".oaset" / "plugins"]


def load_plugins(cwd: Path, registry, extra_commands: dict[str, PluginCommand], log,
                 loaded: list[LoadedPlugin] | None = None,
                 failures: list[str] | None = None) -> list[str]:
    """Import every plugin and run register(). Returns loaded plugin names.

    When `loaded` is provided it is filled with per-plugin contribution
    records (tools/commands), enabling `reload_plugins` to retract cleanly.
    Project-directory plugins must be trusted first (see module docstring).
    """
    names: list[str] = []
    project_base = (Path(cwd) / ".oaset" / "plugins").resolve()
    for base in plugin_dirs(cwd):
        if not base.is_dir():
            continue
        is_project = base.resolve() == project_base
        for path in sorted(base.glob("*.py")):
            if len(names) >= MAX_PLUGINS:
                # silently dropping plugins made "plugins disappeared" a
                # mystery; say why
                log(t("plug_limit_reached", limit=MAX_PLUGINS))
                return names
            if is_project and not is_trusted(path.stem, path):
                log(t("plug_untrusted", name=path.stem))
                continue
            name = f"oaset_plugin_{path.stem}"
            record = LoadedPlugin(name=path.stem, module_name=name) if loaded is not None else None
            try:
                spec = importlib.util.spec_from_file_location(name, path)
                if spec is None or spec.loader is None:
                    raise ImportError(f"cannot build import spec for {path}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                register = getattr(module, "register", None)
                if register is None:
                    log(f"plugin {path.stem}: no register() — skipped")
                    continue
                manifest_raw = getattr(module, "OASET_MANIFEST", None)
                manifest: dict[str, Any] = {}
                if manifest_raw is not None:
                    try:
                        manifest = _validate_manifest(path.stem, manifest_raw)
                    except ValueError as exc:
                        log(t("plug_bad_manifest", name=path.stem, exc=exc))
                        manifest = {"_invalid": str(exc)}
                if record is not None:
                    record.manifest = manifest
                elif manifest:
                    unknown = manifest.get("_unknown_keys") or []
                    perms = ", ".join(manifest.get("permissions") or []) or "-"
                    log(f"plugin {path.stem}: manifest v{manifest.get('version', '?')} "
                        f"permissions=[{perms}]" + (f" unknown_keys={unknown}" if unknown else ""))
                requires = str((manifest or {}).get("requires_oaset", "") or "").strip()
                if requires:
                    from oaset.update import _version_at_least

                    try:
                        compatible = _version_at_least(__oaset_version__(), requires)
                    except Exception:
                        compatible = None  # bad constraint: report, do not guess
                    if compatible is False:
                        log(t("plug_incompatible", name=path.stem, requires=requires))
                        continue
                    if compatible is None:
                        log(t("plug_bad_constraint", name=path.stem, requires=requires))
                        continue
                # only a manifest that DECLARES permissions constrains the
                # surface; a plugin without one keeps the legacy surface
                if "permissions" in manifest:
                    manifest_permissions: tuple[str, ...] | None = tuple(
                        str(p) for p in (manifest.get("permissions") or []))
                else:
                    manifest_permissions = None
                try:
                    register(PluginAPI(path.stem, registry, extra_commands, log,
                                       record, permissions=manifest_permissions))
                except PermissionError as exc:
                    # capability boundary refused a call: the plugin keeps
                    # loading with the surface it declared — partial
                    # contributions stay tracked so a reload retracts them
                    log(t("plug_restricted", name=path.stem, exc=exc))
                names.append(path.stem)
                if loaded is not None and record is not None:
                    loaded.append(record)
            except Exception as exc:
                message = f"plugin {path.stem} failed: {type(exc).__name__}: {exc}"
                log(message)
                if failures is not None:
                    failures.append(message)
    return names


def unload_plugins(registry, extra_commands: dict[str, PluginCommand],
                   loaded: list[LoadedPlugin], log) -> None:
    """Retract exactly what the previous load added, and drop stale modules
    from sys.modules so a re-import sees fresh file content."""
    for record in loaded:
        for tool_name in record.tools:
            registry.remove_tool(tool_name)
        for cmd_name in record.commands:
            extra_commands.pop(cmd_name, None)
        with contextlib.suppress(KeyError):
            del sys.modules[record.module_name]
        log(f"plugin {record.name}: unloaded ({len(record.tools)} tools, {len(record.commands)} commands)")
    loaded.clear()


def reload_plugins(cwd: Path, registry, extra_commands: dict[str, PluginCommand], log,
                   loaded: list[LoadedPlugin],
                   failures: list[str] | None = None) -> list[str]:
    """Unload the previous set (tools/commands/modules) then load fresh."""
    unload_plugins(registry, extra_commands, loaded, log)
    return load_plugins(cwd, registry, extra_commands, log, loaded=loaded,
                        failures=failures)
