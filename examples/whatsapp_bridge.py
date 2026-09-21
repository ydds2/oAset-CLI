"""Local WhatsApp bridge stand-in (single-machine, no accounts).

Contract consumed by oAset's WhatsAppTransport:
  GET  /receive?timeout=N -> [{"chat_id": "...", "text": "..."}]
  POST /send              -> {"chat_id": "...", "text": "..."}   (logged locally)

Feed inbound messages:
  curl -X POST http://127.0.0.1:8090/inject -H "content-type: application/json" \
       -d '{"chat_id": "wa-1", "text": "hello"}'
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
        if self.path.startswith("/receive"):
            try:
                item = INBOX.get(timeout=5)
            except queue.Empty:
                item = None
            self._json([] if item is None else [item])

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/inject":
            INBOX.put(body)
            self._json({"queued": True})
        elif self.path == "/send":
            print("[bridge] SEND:", body, flush=True)
            self._json({"ok": True})
        else:
            self._json({})


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    print(f"local whatsapp bridge on :{port} (POST /inject to feed inbound messages)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
