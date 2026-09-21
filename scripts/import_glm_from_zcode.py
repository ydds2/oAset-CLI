"""One-off local helper: import the user's GLM endpoint/key from ZCode's local
config into oAset's credential store. Secrets are never printed."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

ZCODE_V2 = Path.home() / ".zcode" / "v2" / "config.json"


def load_entries() -> list[dict]:
    raw = json.loads(ZCODE_V2.read_text(encoding="utf-8"))

    def find(obj):
        if isinstance(obj, dict):
            if "baseURL" in obj and "apiKey" in obj:
                yield obj
            for v in obj.values():
                yield from find(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from find(v)

    return list(find(raw))


def is_glm(entry: dict) -> bool:
    url = str(entry.get("baseURL", ""))
    return ("bigmodel.cn" in url) or ("z.ai" in url)


def main() -> int:
    entries = [e for e in load_entries() if is_glm(e) and e.get("apiKey")]
    print(f"GLM-capable entries found: {len(entries)}")
    ranked = sorted(
        entries,
        key=lambda e: 0 if "open.bigmodel.cn" in str(e.get("baseURL", "")) else
        (1 if "z.ai" in str(e.get("baseURL", "")) else 2),
    )
    from oaset.credentials import save_credential

    for entry in ranked:
        base = str(entry["baseURL"]).rstrip("/")
        key = str(entry["apiKey"])
        # probe: anthropic-compatible /v1/messages with 1-token ping
        try:
            resp = httpx.post(
                f"{base}/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                json={
                    "model": "glm-5.3-flash",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "回复OK"}],
                },
                timeout=20,
            )
            status = resp.status_code
            ok = status == 200
            sample = resp.json().get("content", [{}])[0].get("text", "")[:40] if ok else resp.text[:60]
        except Exception as exc:
            status, ok, sample = -1, False, f"{type(exc).__name__}: {exc}"[:60]
        print(f"  {base}  model=glm-5.3-flash  HTTP {status}  sample={sample!r}")
        if ok:
            save_credential("glm", key)
            print("SAVED_CREDENTIAL glm")
            print(f"WORKING_BASE {base}")
            return 0
    # fallback: OpenAI-compatible bigmodel endpoint with the same keys
    for entry in ranked:
        key = str(entry["apiKey"])
        try:
            resp = httpx.post(
                "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": "glm-5.3-flash", "messages": [{"role": "user", "content": "回复OK"}],
                      "max_tokens": 16},
                timeout=20,
            )
            if resp.status_code == 200:
                save_credential("glm", key)
                print("SAVED_CREDENTIAL glm (openai-compatible bigmodel)")
                print("WORKING_BASE https://open.bigmodel.cn/api/paas/v4")
                return 0
            print(f"  openai-compatible try HTTP {resp.status_code}")
        except Exception as exc:
            print(f"  openai-compatible try failed: {exc}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
