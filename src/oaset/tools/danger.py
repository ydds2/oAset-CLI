"""Dangerous shell command detection (compact pattern set).

Two tiers:
- BLOCKED: never approvable — disk-wiping / firmware / destructive system ops.
- DANGEROUS: requires confirmation (already the EXEC default) and disables
  the "always allow" shortcut so a prompt-injected agent cannot normalize it.
"""

from __future__ import annotations

import re

# Unconditionally blocked — no approval can override these.
BLOCKED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+/(\s|$)", re.IGNORECASE),          # rm -rf /
    re.compile(r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f?[a-zA-Z]*\s+/(usr|etc|var|bin|sbin|lib|boot|dev|home|root)\b", re.IGNORECASE),
    re.compile(r"\bmkfs(\.\w+)?\b", re.IGNORECASE),                                     # format filesystems
    re.compile(r"\bdiskpart\b", re.IGNORECASE),
    re.compile(r"\b(format|initialize)\s+[a-zA-Z]:\\\s*$", re.IGNORECASE),              # format C:\
    re.compile(r"\bcipher\s+/w\b", re.IGNORECASE),
    re.compile(r"\breg\s+delete\s+HKLM\b", re.IGNORECASE),
    re.compile(r"\bRemove-Item\s+.*-Recurse.*\s+C:\\(\s|$)", re.IGNORECASE),
    re.compile(r"\bdd\s+.*\bof=/dev/(sd|nvme|hd)", re.IGNORECASE),
    re.compile(r">\s*/dev/sd[a-z]\b", re.IGNORECASE),
    re.compile(r":\(\)\s*\{\s*:\|\s*:&\s*\};:", re.IGNORECASE),                          # fork bomb
    re.compile(r"\bshred\s+.*(/dev/sd|C:\\)", re.IGNORECASE),
]

# Dangerous: approval required, "always allow" is suppressed for the session.
DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*\s+)", re.IGNORECASE),                     # recursive delete
    re.compile(r"\bdel\s+/[sq]\b", re.IGNORECASE),
    re.compile(r"\brmdir\s+/s\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+.*--force", re.IGNORECASE),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
    re.compile(r"\bgit\s+clean\s+-[a-zA-Z]*f", re.IGNORECASE),
    re.compile(r"\bchmod\s+-R\s+777\b", re.IGNORECASE),
    re.compile(r"\bchown\s+-R\b", re.IGNORECASE),
    re.compile(r"\b(sudo|runas)\b", re.IGNORECASE),
    re.compile(r"\btaskkill\s+/f\b", re.IGNORECASE),
    re.compile(r"\bkill\s+-9\b"),
    re.compile(r"\b(pip|npm|pnpm|yarn|uv)\s+install\b.*(--force|--break-system-packages)", re.IGNORECASE),
    re.compile(r"\bcurl\b[^\n|]*\|\s*(sh|bash|powershell)\b", re.IGNORECASE),           # pipe-to-shell
    re.compile(r"\biwr\b.*\|\s*iex\b", re.IGNORECASE),
    re.compile(r"\binvoke-expression\b", re.IGNORECASE),
    re.compile(r"\bshutdown\b|\breboot\b", re.IGNORECASE),
    re.compile(r"\bschtasks\s+/create\b", re.IGNORECASE),
    re.compile(r"\breg\s+(add|delete)\b", re.IGNORECASE),
    re.compile(r"\bsetx\b", re.IGNORECASE),
    re.compile(r"\bATTRIB\s+[-+]S\b", re.IGNORECASE),
    re.compile(r"\bnet\s+(user|localgroup)\b", re.IGNORECASE),
]


def classify_command(command: str) -> str:
    """Return 'blocked' | 'dangerous' | 'normal'."""
    for pattern in BLOCKED_PATTERNS:
        if pattern.search(command):
            return "blocked"
    for pattern in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return "dangerous"
    return "normal"
