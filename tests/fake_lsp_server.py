"""Fake LSP server over stdio for tests: Content-Length framed JSON-RPC.

Behavior: answers `initialize`, accepts didOpen/didChange, and pushes
publishDiagnostics with one error on the opened document.
"""

from __future__ import annotations

import json
import sys


def send(message: dict) -> None:
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        key, _, value = line.partition(b":")
        headers[key.strip().lower().decode()] = value.strip().decode()
    length = int(headers.get("content-length", 0))
    if not length:
        return None
    return json.loads(sys.stdin.buffer.read(length))


def main() -> None:
    while True:
        message = read_message()
        if message is None:
            break
        method = message.get("method", "")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {}}})
            _initialized = True
        elif method == "initialized":
            pass
        elif method in ("textDocument/didOpen", "textDocument/didChange"):
            doc = message["params"]["textDocument"]
            uri = doc["uri"] if "uri" in doc else message["params"]["textDocument"]["uri"]
            send({
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": uri,
                    "diagnostics": [{
                        "range": {"start": {"line": 0, "character": 0},
                                  "end": {"line": 0, "character": 5}},
                        "severity": 1,
                        "message": "fake undefined name 'x'",
                    }],
                },
            })
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": message["id"], "result": None})
            break


if __name__ == "__main__":
    main()
