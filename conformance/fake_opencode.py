#!/usr/bin/env python3
"""
A stand-in for the `opencode` executable (2.x API) with a scripted model.
Used only by the conformance tests.

    fake_opencode.py --version
    fake_opencode.py serve --port 4096 [--hostname 127.0.0.1]

Auth: basic auth opencode:$OPENCODE_SERVER_PASSWORD (default "testpw").
Unknown paths return HTML with status 200, like real opencode 2.x.

The scripted model:
  - no tools in the prompt: answers "PONG"
  - tools offered and no <tool_result> yet: one <tool_call>
    (Bash with "echo hello" if offered, else the first tool, with every
    required argument set to "Paris")
  - a <tool_result> present: "Tool said: <last result>"
  - a prompt containing "FAIL_QUOTA": the 503 free usage error
"""
import base64
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", "testpw")
AUTH = "Basic " + base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
MODELS = [{"providerID": "fake", "modelID": "toolbot", "enabled": True},
          {"providerID": "fake", "modelID": "echo", "enabled": True},
          {"providerID": "fake", "modelID": "disabled-one", "enabled": False}]
SESSIONS = {}
LOCK = threading.Lock()
STATS = {"created": 0, "deleted": 0}


def model_reply(prompt):
    results = re.findall(r"<tool_result[^>]*>\n(.*?)\n</tool_result>", prompt, re.S)
    if results:
        return "Tool said: " + results[-1].strip()
    if "Available tools:" not in prompt:
        return "PONG"
    section = prompt.split("Available tools:", 1)[1]
    tools = re.findall(r"^## (\S+)\n(?:.*\n)*?JSON Schema for arguments: (.*)$", section, re.M)
    names = [n for n, _ in tools]
    if "Bash" in names:
        return ('I will run it.\n<tool_call>\n{"name": "Bash", "arguments": '
                '{"command": "echo hello", "description": "Print hello"}}\n</tool_call>')
    name, schema = tools[0]
    args = {k: "Paris" for k in json.loads(schema).get("required", [])}
    return "<tool_call>\n" + json.dumps({"name": name, "arguments": args}) + "\n</tool_call>"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj=None, html=False):
        body = b"<!doctype html><html></html>" if html else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html" if html else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        if self.headers.get("Authorization") != AUTH:
            self._send(401, {"_tag": "UnauthorizedError", "message": "Authentication required"})
            return False
        return True

    def do_GET(self):
        path = self.path.split("?")[0]
        if not path.startswith("/api/"):
            return self._send(200, html=True)
        if not self._auth():
            return
        if path == "/api/info":
            return self._send(200, {"version": "2.0.31-fake", "pid": os.getpid()})
        if path == "/api/model":
            return self._send(200, {"data": MODELS})
        if path == "/api/model/default":
            return self._send(200, {"data": MODELS[0]})
        if path == "/api/_stats":
            with LOCK:
                return self._send(200, dict(STATS, open=len(SESSIONS)))
        self._send(404, {"_tag": "NotFound", "message": "nope"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if not path.startswith("/api/"):
            return self._send(200, html=True)
        if not self._auth():
            return
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if path == "/api/session":
            if req.get("permissions"):
                return self._send(503, {"_tag": "ServiceUnavailableError", "message": "Free usage limit reached."})
            with LOCK:
                STATS["created"] += 1
                sid = "ses_fake%d" % STATS["created"]
                SESSIONS[sid] = req
            return self._send(200, {"data": {"id": sid}})
        match = re.match(r"^/api/session/(ses_\w+)/(generate|interrupt)$", path)
        if match:
            with LOCK:
                known = match.group(1) in SESSIONS
            if not known:
                return self._send(404, {"_tag": "SessionNotFoundError", "message": "no session"})
            if match.group(2) == "interrupt":
                return self._send(200, {"data": True})
            prompt = req.get("prompt", "")
            if "FAIL_QUOTA" in prompt:
                return self._send(503, {"_tag": "ServiceUnavailableError", "message": "Free usage limit reached."})
            return self._send(200, {"data": {"text": model_reply(prompt)}})
        if path == "/api/experimental/generate":
            return self._send(503, {"_tag": "ServiceUnavailableError", "message": "Free usage limit reached."})
        self._send(404, {"_tag": "NotFound", "message": "nope"})

    def do_DELETE(self):
        if not self._auth():
            return
        sid = self.path.split("?")[0].rsplit("/", 1)[-1]
        with LOCK:
            if SESSIONS.pop(sid, None) is not None:
                STATS["deleted"] += 1
        self._send(200, {"data": True})

    def log_message(self, *args):
        pass


def main(argv):
    if "--version" in argv or "-v" in argv:
        print("2.0.31-fake")
        return 0
    if not argv or argv[0] != "serve":
        print("usage: fake_opencode.py serve --port N | --version", file=sys.stderr)
        return 2
    port, host = 4096, "127.0.0.1"
    for i, arg in enumerate(argv):
        if arg == "--port":
            port = int(argv[i + 1])
        if arg == "--hostname":
            host = argv[i + 1]
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"server listening on http://{host}:{port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
