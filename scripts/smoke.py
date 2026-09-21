"""Production smoke gate — run before any release:

    python scripts/smoke.py

Checks that require no network and no API keys:
1. ruff gate (pyproject ruleset) is clean
2. full test suite passes
3. every CLI subcommand answers --help
4. `oaset mcp-serve` completes a JSON-RPC handshake over stdio
5. tool registry + slash-command registry are internally consistent
6. config roundtrip (save -> load -> same default model)
Exit code 0 = shippable.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")

failures: list[str] = []


def run(cmd: list[str], cwd: Path = ROOT, timeout: int = 120, input_text: str | None = None,
        env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                          input=input_text, env=env)


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


# 1) lint gate
r = run([PY, "-m", "ruff", "check", "src/", "tests/"])
check("ruff gate", r.returncode == 0, r.stdout[-400:])

# 2) tests
r = run([PY, "-m", "pytest", "-q", "--tb=no"], timeout=600)
check("test suite", r.returncode == 0 and "failed" not in r.stdout, r.stdout[-200:])

# 3) every CLI subcommand --help
r = run([PY, "-m", "ruff", "--version"])  # sanity that PY works at all
help_out = run([PY, "-m", "oaset", "--help"])
check("cli --help", help_out.returncode == 0)
for sub in ["login", "logout", "mcp-serve", "batch", "config", "setup", "gateway",
            "cron", "models", "sessions", "doctor"]:
    r = run([PY, "-m", "oaset", sub, "--help"])
    check(f"cli {sub} --help", r.returncode == 0, r.stderr[-120:])

# 4) mcp-serve handshake
init_msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"clientInfo": {"name": "smoke"}}})
r = run([PY, "-m", "oaset", "mcp-serve"], input_text=init_msg + "\n", timeout=60)
try:
    reply = json.loads(r.stdout.strip().splitlines()[-1])
    ok = reply.get("result", {}).get("serverInfo", {}).get("name") == "oaset"
except Exception:
    ok = False
check("mcp-serve handshake", ok, r.stdout[-200:] + r.stderr[-200:])

# 5) registry consistency (in-process)
code = (
    "from oaset.tools import default_tools;"
    "from oaset.tui.commands import COMMANDS;"
    "from oaset.tui.app import OasetApp;"
    "tools = {t.name for t in default_tools()};"
    "missing = [c.name for c in COMMANDS if not hasattr(OasetApp, 'cmd_' + c.name.replace('-', '_'))];"
    "print('tools', len(tools), 'commands', len(COMMANDS), 'missing', missing)"
)
r = run([PY, "-c", code])
ok = r.returncode == 0 and "missing []" in r.stdout
check("registry consistency", ok, r.stdout[-200:] + r.stderr[-200:])

# 6) config roundtrip (isolated home)
import os
import tempfile

with tempfile.TemporaryDirectory() as home:
    env = {**os.environ, "OASET_HOME": home}
    r = run([PY, "-c", (
        "from oaset.config import load_config, save_config, default_config;"
        "cfg = default_config(); cfg.default_model = 'mock/mock-echo';"
        "save_config(cfg);"
        "assert load_config().default_model == 'mock/mock-echo'; print('roundtrip ok')"
    )], env=env)
    check("config roundtrip", "roundtrip ok" in r.stdout, r.stderr[-200:])
    r = run([PY, "-m", "oaset", "doctor"], env=env)
    check("cli doctor", r.returncode == 0, r.stderr[-200:])

print()
if failures:
    print(f"SMOKE FAILED ({len(failures)}):", ", ".join(failures))
    sys.exit(1)
print("SMOKE PASSED — shippable.")
