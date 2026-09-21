"""Local Signal bridge (single-machine stand-in for signal-cli REST).

Endpoints consumed by oAset's SignalTransport:
  GET  /v1/receive/{account}?timeout=N -> [{"envelope": {...}}]
  POST /v2/send                        -> {}

Run:  python examples/signal_bridge.py [port]     (default 8080)
Then: oaset gateway --telegram-like flows via SignalTransport with
      api_base=http://127.0.0.1:8080

Post an inbound message with curl:
  curl -X POST http://127.0.0.1:8080/inject -H "content-type: application/json" \
       -d '{"source": "+15550001", "message": "hello"}'
"""
from __future__ import annotations

import json
import queue
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

INBOX: "queue.Queue[dict]" = queue.Queue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/receive/"):
            timeout = 5
            try:
                item = INBOX.get(timeout=timeout)
            except queue.Empty:
                item = None
            payload = [] if item is None else [{
                "envelope": {"source": item["source"],
                             "dataMessage": {"message": item["message"]}}}]
            self._json(payload)
        else:
            self._json({})

    def do_POST(self):
        if self.path == "/inject":
            length = int(self.headers.get("content-length", 0))
            item = json.loads(self.rfile.read(length) or b"{}")
            INBOX.put({"source": item.get("source", "+15550001"),
                       "message": item.get("message", "")})
            self._json({"queued": True})
        elif self.path == "/v2/send":
            length = int(self.headers.get("content-length", 0))
            print("[bridge] SEND:", self.rfile.read(length).decode(), flush=True)
            self._json({"ok": True})
        else:
            self._json({})


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"local signal bridge on :{port} (POST /inject to feed inbound messages)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
