"""Offline OpenAI-compatible mock server for oAset demos and end-to-end tests.

Run:  python examples/mock_server.py [port]        (default 8399)
Then: oaset --model mock/mock-echo                 (provider `mock` points here)

Behavior (scripted):
  turn 1 — streams a short text, then requests read_file on ./demo.txt
  turn 2 — (after the tool result arrives) streams a completion summary
Everything after echoes the user text. No network access required.
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "mock-echo"
TOOL_FILE = "demo.txt"


def sse_chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> bytes:
    payload = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage is not None:
        payload["usage"] = usage
        payload["choices"] = []
    return f"data: {json.dumps(payload)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # quiet
        pass

    def do_GET(self) -> None:  # health probe
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self) -> None:
        if not self.path.rstrip().endswith("/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages", [])
        saw_tool_result = any(m.get("role") == "tool" for m in messages)
        last_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        question = str(last_user.get("content", ""))

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()

        def send(chunk: bytes) -> None:
            self.wfile.write(chunk)
            self.wfile.flush()

        if not saw_tool_result:
            for piece in ("Let me check ", TOOL_FILE, " first.\n"):
                send(sse_chunk({"content": piece}))
            args = json.dumps({"path": TOOL_FILE})
            send(sse_chunk({"tool_calls": [{"index": 0, "id": "call_mock_1", "type": "function",
                                            "function": {"name": "read_file", "arguments": args}}]}))
            send(sse_chunk({}, finish="tool_calls"))
        else:
            for piece in ("Done. ", "I read ", TOOL_FILE, " and the answer to “",
                          question[:60], "” is: the mock pipeline works end to end. ✅"):
                send(sse_chunk({"content": piece}))
            send(sse_chunk({}, finish="stop"))
        send(sse_chunk({}, finish=None, usage={"prompt_tokens": 42, "completion_tokens": 24, "total_tokens": 66}))
        send(b"data: [DONE]\n\n")


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8399
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"oAset mock server on http://127.0.0.1:{port}/v1  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
