"""Mechanical gates for the command system - the "complete TUI" contract.

Two layers:

1. a fast static contract over the registry (metadata, alias integrity, taxonomy
   health) that fails the moment a new command is added without them;
2. a behavioural sweep (scripts/tui_command_audit.py) asserting that every
   command produces user-visible feedback or a guided prompt - never silence and
   never an exception.

Layer 2 is what turns "we fixed the silent commands" into something CI keeps
true for commands that do not exist yet.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from oaset.tui.commands import (  # noqa: E402
    CATEGORY_ORDER,
    COMMANDS,
    RISK_SAFE,
    alias_map,
    arg_spec_for,
    category_label,
)

# Commands that may legitimately sit in the catch-all bucket. Kept short on
# purpose: "general" is what made the palette grouping useless.
GENERAL_ALLOWED: set[str] = set()


# ------------------------------------------------------------ static contract

def test_every_command_has_presentable_metadata():
    for cmd in COMMANDS:
        assert cmd.description.strip(), f"/{cmd.name} has no description"
        assert cmd.category, f"/{cmd.name} has no category"
        assert cmd.category in CATEGORY_ORDER, \
            f"/{cmd.name}: category '{cmd.category}' is not in CATEGORY_ORDER"


def test_risky_commands_declare_their_effect():
    """A write/danger command must tell the user what it will change."""
    for cmd in COMMANDS:
        if cmd.risk != RISK_SAFE:
            assert cmd.effect.strip(), f"/{cmd.name} (risk={cmd.risk}) declares no effect"


def test_commands_requiring_arguments_have_a_form():
    for cmd in COMMANDS:
        if cmd.requires_args:
            spec = arg_spec_for(cmd.name)
            assert spec is not None, \
                f"/{cmd.name} requires args but has no ArgSpec (dead-end prompt)"
            assert spec.title.strip(), f"/{cmd.name}: arg form has no title"


def test_aliases_resolve_to_their_own_command():
    aliases = alias_map()
    for cmd in COMMANDS:
        for alias in cmd.aliases:
            assert aliases[alias.lower()] == cmd.name, \
                f"alias /{alias} does not resolve to /{cmd.name}"


def test_no_alias_shadowing():
    """Two commands must not claim the same alias (the old table did)."""
    seen: dict[str, str] = {}
    for cmd in COMMANDS:
        for alias in cmd.aliases:
            key = alias.lower()
            assert key not in seen, f"/{alias}: claimed by /{seen[key]} and /{cmd.name}"
            seen[key] = cmd.name


def test_taxonomy_is_actually_used():
    """Grouping only helps if the groups are populated and specific."""
    general = [c.name for c in COMMANDS if c.category == "general" and
               c.name not in GENERAL_ALLOWED]
    assert not general, f"commands left in 'general': {general}"

    groups: dict[str, int] = {}
    for cmd in COMMANDS:
        groups[cmd.category] = groups.get(cmd.category, 0) + 1
    assert len(groups) >= 8, f"only {len(groups)} categories in use: {groups}"
    biggest = max(groups.values())
    assert biggest <= 10, f"one category holds {biggest} commands: {groups}"
    for key in groups:
        assert category_label(key) != category_label("general"), \
            f"category '{key}' has no label of its own"


def test_common_synonyms_exist():
    """The words users actually type must reach the command."""
    aliases = alias_map()
    for alias, target in (("?", "help"), ("h", "help"), ("quit", "exit"),
                          ("q", "exit"), ("md", "model"), ("resume", "sessions"),
                          ("cls", "clear")):
        assert aliases.get(alias) == target, f"/{alias} should reach /{target}"


# -------------------------------------------------------- behavioural sweep

async def test_every_command_gives_feedback_or_a_prompt(tmp_path):
    """SILENT and ERROR are the two outcomes a user cannot act on."""
    import tui_command_audit as audit

    results = await audit.audit_all(tmp_path, timeout=15.0)
    bad = [f"/{r['name']}: {r['outcome']} {r['detail']}"[:160]
           for r in results if r["outcome"] in ("SILENT", "ERROR")]
    assert not bad, "commands with no usable outcome:\n" + "\n".join(bad)
    assert len(results) >= len(COMMANDS), "the sweep must cover the whole registry"
