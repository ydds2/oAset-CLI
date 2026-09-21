"""Minimal MCP server over stdio for tests: tools `echo` and `add`.

Optional argv[1]: a path to dump the client's response to our
sampling/createMessage request (id 9001), sent right after
notifications/initialized. Without argv the sampling probe is skipped,
keeping older tests untouched.
"""

from __future__ import annotations

import json
import sys

SAMPLING_ID = 9001
ELICIT_ID = 9002
ROOTS_ID = 9003


def reply(message_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()


def main() -> None:
    dump_path = sys.argv[1] if len(sys.argv) > 1 else None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method", "")
        request_id = message.get("id")
        if method == "notifications/initialized":
            if dump_path:
                sys.stdout.write(json.dumps({
                    "jsonrpc": "2.0", "id": SAMPLING_ID,
                    "method": "sampling/createMessage",
                    "params": {"messages": [{"role": "user",
                                             "content": {"type": "text",
                                                         "text": "Say OK"}}],
                               "maxTokens": 64},
                }) + "\n")
                sys.stdout.write(json.dumps({
                    "jsonrpc": "2.0", "id": ELICIT_ID,
                    "method": "elicitation/create",
                    "params": {"message": "confirm install",
                               "requestedSchema": {
                                   "type": "object",
                                   "properties": {"answer": {
                                       "type": "string",
                                       "title": "Confirm"}},
                                   "required": ["answer"]}},
                }) + "\n")
                sys.stdout.write(json.dumps({
                    "jsonrpc": "2.0", "id": ROOTS_ID,
                    "method": "roots/list", "params": {},
                }) + "\n")
                sys.stdout.flush()
            continue
        if request_id is None:
            continue  # other notifications
        if not method:
            # response to one of our server→client probes
            kind = {SAMPLING_ID: "sampling", ELICIT_ID: "elicitation",
                    ROOTS_ID: "roots"}.get(request_id)
            if dump_path and kind:
                with open(f"{dump_path}.{kind}.json", "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(message))
            continue
        if method == "initialize":
            reply(request_id, {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1.0"},
            })
        elif method == "tools/list":
            reply(request_id, {"tools": [
                {
                    "name": "echo",
                    "description": "Echo the given text back",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string", "description": "Text to echo"}},
                        "required": ["text"],
                    },
                },
                {
                    "name": "add",
                    "description": "Add two numbers",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "a": {"type": "number"},
                            "b": {"type": "number"},
                        },
                        "required": ["a", "b"],
                    },
                },
            ]})
        elif method == "tools/call":
            params = message.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "echo":
                text = str(args.get("text", ""))
                reply(request_id, {
                    "content": [{"type": "text", "text": f"echo: {text}"}],
                    "isError": text == "fail",
                })
            elif name == "add":
                reply(request_id, {
                    "content": [{"type": "text", "text": str(float(args.get("a", 0)) + float(args.get("b", 0)))}],
                    "isError": False,
                })
            else:
                reply(request_id, {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True})
        elif method == "ping":
            reply(request_id, {})
        else:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
