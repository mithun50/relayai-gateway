#!/usr/bin/env python3
"""
Conformance tests for both RelayAI implementations, against a fake opencode.

    python conformance/run.py --impl python
    python conformance/run.py --impl node
    python conformance/run.py --impl python --with-claude   # also drives real Claude Code

Every check prints PASS or FAIL; the exit code is the number of failures.
Runs in a temporary RELAYAI_HOME, so it never touches your real ~/.relayai.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "conformance" / "fake_opencode.py"
FAKE_BIN = ROOT / "conformance" / "bin" / ("opencode.cmd" if os.name == "nt" else "opencode")
KEY = "conformance-key"
results = {"pass": 0, "fail": 0}


def check(name, cond, detail=""):
    if cond:
        results["pass"] += 1
        print(f"  PASS  {name}", flush=True)
    else:
        results["fail"] += 1
        print(f"  FAIL  {name}  {str(detail)[:400]}", flush=True)
    return bool(cond)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url, seconds=60):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2).close()
            return True
        except urllib.error.HTTPError:
            return True
        except OSError:
            time.sleep(0.3)
    return False


def http(base, path, body=None, key=KEY, method=None, headers=None, raw=False):
    h = {"Content-Type": "application/json"}
    if key is not None:
        h["Authorization"] = f"Bearer {key}"
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, data = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, data = e.code, e.read().decode()
    if raw:
        return status, data
    try:
        return status, json.loads(data)
    except ValueError:
        return status, {"_raw": data}


def sse(data):
    return [json.loads(l[6:]) for l in data.splitlines() if l.startswith("data: ") and l[6:] != "[DONE]"]


WEATHER = {"type": "function", "function": {"name": "get_weather", "description": "Weather for a city",
                                            "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                                                           "required": ["city"]}}}
ATOOL = {"name": "get_weather", "description": "Weather for a city", "input_schema": WEATHER["function"]["parameters"]}


# ----------------------------------------------------------------- API checks

def api_checks(base, fake_base):
    s, b = http(base, "/health", key=None)
    check("health is public and reports opencode", s == 200 and b.get("service") == "RelayAI"
          and b["opencode"]["reachable"] is True, b)

    s, b = http(base, "/v1/models")
    ids = [m["id"] for m in b.get("data", [])]
    check("models come live from opencode, disabled ones hidden", ids == ["fake/echo", "fake/toolbot"], b)
    s, b = http(base, "/v1/models/toolbot")
    check("model lookup by short name", s == 200 and b.get("id") == "fake/toolbot", b)
    s, b = http(base, "/v1/models", key="wrong")
    check("wrong key is 401 (OpenAI shape)", s == 401 and b["error"]["code"] == "invalid_api_key", b)
    s, b = http(base, "/v1/messages", {"model": "x", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]},
                key=None, headers={"x-api-key": "wrong"})
    check("wrong key is 401 (Anthropic shape)", s == 401 and b.get("type") == "error", b)
    s, b = http(base, "/v1/nope", {"a": 1})
    check("unknown endpoint is 404", s == 404, b)
    s, b = http(base, "/v1/chat/completions", {"model": "nope/nope", "messages": [{"role": "user", "content": "hi"}]})
    check("unknown model is 404 model_not_found", s == 404 and b["error"]["code"] == "model_not_found", b)
    s, b = http(base, "/v1/chat/completions", {"model": "default", "messages": []})
    check("empty messages is 400", s == 400, b)

    s, b = http(base, "/v1/chat/completions", {"model": "default", "messages": [{"role": "user", "content": "Say PONG"}]})
    check("OpenAI plain chat", s == 200 and b["choices"][0]["message"]["content"] == "PONG"
          and b["choices"][0]["finish_reason"] == "stop" and b["usage"]["total_tokens"] > 0, b)
    s, b = http(base, "/chat/completions", {"model": "default", "messages": [{"role": "user", "content": "x"}]})
    check("base URL without /v1 is tolerated", s == 200, b)

    msgs = [{"role": "user", "content": "Weather in Paris?"}]
    s, b = http(base, "/v1/chat/completions", {"model": "fake/toolbot", "messages": msgs, "tools": [WEATHER]})
    calls = (b.get("choices") or [{}])[0].get("message", {}).get("tool_calls") or []
    check("OpenAI tool_calls", s == 200 and b["choices"][0]["finish_reason"] == "tool_calls" and len(calls) == 1
          and json.loads(calls[0]["function"]["arguments"]) == {"city": "Paris"}, b)
    check("response names the model used", b.get("model") == "fake/toolbot", b.get("model"))
    if calls:
        msgs += [b["choices"][0]["message"], {"role": "tool", "tool_call_id": calls[0]["id"], "content": "18C sunny"}]
        s, b = http(base, "/v1/chat/completions", {"model": "fake/toolbot", "messages": msgs, "tools": [WEATHER]})
        check("OpenAI tool result is used", s == 200 and b["choices"][0]["message"]["content"] == "Tool said: 18C sunny", b)

    s, data = http(base, "/v1/chat/completions", {"model": "default", "stream": True,
                                                  "stream_options": {"include_usage": True}, "tools": [WEATHER],
                                                  "messages": [{"role": "user", "content": "Weather?"}]}, raw=True)
    ev = sse(data)
    tc = [d["choices"][0]["delta"]["tool_calls"][0] for d in ev
          if d.get("choices") and "tool_calls" in d["choices"][0]["delta"]]
    fins = [d["choices"][0]["finish_reason"] for d in ev if d.get("choices") and d["choices"][0]["finish_reason"]]
    check("OpenAI streaming tool call", s == 200 and len(tc) == 1 and tc[0]["index"] == 0 and fins == ["tool_calls"]
          and data.rstrip().endswith("data: [DONE]") and any("usage" in d for d in ev), data[:300])
    s, data = http(base, "/v1/chat/completions", {"model": "default", "stream": True,
                                                  "messages": [{"role": "user", "content": "hi"}]}, raw=True)
    text = "".join(d["choices"][0]["delta"].get("content") or "" for d in sse(data) if d.get("choices"))
    check("OpenAI streaming text", s == 200 and text == "PONG", data[:300])

    s, b = http(base, "/v1/chat/completions", {"model": "default", "tool_choice": "none", "tools": [WEATHER],
                                               "messages": [{"role": "user", "content": "Weather?"}]})
    check("tool_choice none disables tools", s == 200 and b["choices"][0]["message"]["content"] == "PONG", b)

    amsgs = [{"role": "user", "content": "Weather in Paris?"}]
    s, b = http(base, "/v1/messages", {"model": "claude-sonnet-4-5", "max_tokens": 100, "messages": amsgs, "tools": [ATOOL]})
    uses = [c for c in b.get("content", []) if c.get("type") == "tool_use"]
    check("Anthropic tool_use", s == 200 and b["stop_reason"] == "tool_use" and len(uses) == 1
          and uses[0]["input"] == {"city": "Paris"} and b["model"] == "claude-sonnet-4-5", b)
    if uses:
        amsgs += [{"role": "assistant", "content": b["content"]},
                  {"role": "user", "content": [{"type": "tool_result", "tool_use_id": uses[0]["id"], "content": "18C"}]}]
        s, b = http(base, "/v1/messages", {"model": "x", "max_tokens": 100, "messages": amsgs, "tools": [ATOOL]})
        check("Anthropic tool result is used", s == 200 and b["stop_reason"] == "end_turn"
              and b["content"][0]["text"] == "Tool said: 18C", b)

    s, data = http(base, "/v1/messages", {"model": "x", "max_tokens": 100, "stream": True, "tools": [ATOOL],
                                          "messages": [{"role": "user", "content": "Weather?"}]}, raw=True)
    ev = sse(data)
    types = [e["type"] for e in ev]
    deltas = "".join(e["delta"].get("partial_json", "") for e in ev if e["type"] == "content_block_delta")
    check("Anthropic streaming tool_use", s == 200 and types[0] == "message_start" and types[-1] == "message_stop"
          and deltas and json.loads(deltas) == {"city": "Paris"}
          and [e["delta"]["stop_reason"] for e in ev if e["type"] == "message_delta"] == ["tool_use"], data[:300])

    s, b = http(base, "/v1/messages/count_tokens", {"model": "x", "messages": [{"role": "user", "content": "hello"}]})
    check("count_tokens", s == 200 and b.get("input_tokens", 0) > 0, b)

    s, b = http(base, "/v1/chat/completions", {"model": "default", "messages": [{"role": "user", "content": "FAIL_QUOTA"}]})
    check("opencode quota error -> 503 with its message (OpenAI)", s == 503 and "Free usage" in b["error"]["message"], b)
    s, b = http(base, "/v1/messages", {"model": "x", "max_tokens": 5, "messages": [{"role": "user", "content": "FAIL_QUOTA"}]})
    check("opencode quota error -> 503 (Anthropic shape)", s == 503 and b.get("type") == "error", b)

    s, b = http(fake_base, "/api/_stats", key=None, headers={"Authorization": "Basic b3BlbmNvZGU6dGVzdHB3"})
    check("every opencode session was deleted", s == 200 and b["open"] == 0 and b["created"] > 0, b)


# --------------------------------------------------------------- runner

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--impl", choices=["python", "node"], required=True)
    ap.add_argument("--with-claude", action="store_true")
    args = ap.parse_args()

    home = tempfile.mkdtemp(prefix="relayai-conf-")
    env = dict(os.environ, RELAYAI_HOME=home, PYTHONPATH=str(ROOT / "python" / "src"), PYTHONUNBUFFERED="1")
    for var in ("RELAYAI_API_KEY", "RELAYAI_PORT", "RELAYAI_MODEL", "OPENCODE_URL", "OPENCODE_SERVER_PASSWORD"):
        env.pop(var, None)
    cli = [sys.executable, "-m", "relayai"] if args.impl == "python" else ["node", str(ROOT / "node" / "bin" / "relayai.js")]

    def run(*argv, timeout=120, input_text=None):
        p = subprocess.run(cli + list(argv), env=env, capture_output=True, text=True, timeout=timeout,
                           input=input_text, cwd=home)  # neutral cwd: nothing can shadow the package
        return p.returncode, p.stdout, p.stderr

    print(f"== {args.impl}: CLI basics")
    code, out, _ = run("--version")
    py_version = re.search(r'"([^"]+)"', (ROOT / "python" / "src" / "relayai" / "_version.py").read_text()).group(1)
    node_version = json.loads((ROOT / "node" / "package.json").read_text())["version"]
    check("python and node packages have the same version", py_version == node_version, (py_version, node_version))
    check("--version", code == 0 and f"relayai {py_version} ({args.impl})" in out, out)
    code, out, _ = run("--help")
    check("--help lists commands", code == 0 and all(c in out for c in ("serve", "start", "chat", "doctor")), out)

    print(f"== {args.impl}: API against an external opencode (--opencode-url)")
    oc_port, gw_port = free_port(), free_port()
    fake = subprocess.Popen([sys.executable, str(FAKE), "serve", "--port", str(oc_port)],
                            env=dict(env, OPENCODE_SERVER_PASSWORD="testpw"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    gw = subprocess.Popen(cli + ["serve", "--port", str(gw_port), "--api-key", KEY, "--opencode-url",
                                 f"http://127.0.0.1:{oc_port}", "--opencode-password", "testpw"],
                          env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=home)
    try:
        up = wait_http(f"http://127.0.0.1:{gw_port}/health")
        check("serve comes up", up)
        if up:
            api_checks(f"http://127.0.0.1:{gw_port}", f"http://127.0.0.1:{oc_port}")
    finally:
        gw.terminate()
        try:
            out = gw.communicate(timeout=15)[0]
        except subprocess.TimeoutExpired:
            gw.kill()
            out = gw.communicate()[0]
        fake.terminate()
        fake.wait(10)
    ok = check("serve banner shows URLs and key", "OpenAI base URL" in out and KEY in out, out[:500])
    if not ok:
        print("  ---- complete gateway output ----\n" + out + "\n  ---- end ----", flush=True)

    print(f"== {args.impl}: managed opencode, background gateway (start / status / chat / stop)")
    gw_port, oc_port = free_port(), free_port()
    code, out, err = run("start", "--port", str(gw_port), "--opencode-bin", str(FAKE_BIN),
                         "--opencode-port", str(oc_port), timeout=150)
    check("start", code == 0 and f":{gw_port}" in out, out + err)
    code, out, err = run("status", "--json")
    st = json.loads(out) if out.strip().startswith("{") else {}
    gpid = (st.get("gateway") or {}).get("pid")
    opid = (st.get("opencode") or {}).get("pid")
    check("status --json shows gateway and managed opencode", code == 0 and gpid and opid
          and "password" not in json.dumps(st), out + err)
    key = (st.get("gateway") or {}).get("api_key", "")
    check("persistent api key was created", key.startswith("rai-") and Path(home, "api_key").is_file(), key)
    code, out, err = run("models", "--json")
    check("models via the background gateway", code == 0 and "fake/toolbot" in out, out + err)
    code, out, err = run("chat", "--no-stream", "Say PONG")
    check("chat one-shot", code == 0 and out.strip() == "PONG", out + err)
    code, out, err = run("chat", "Say PONG")
    check("chat streaming", code == 0 and out.strip() == "PONG", out + err)
    code, out, err = run("chat", "--no-stream", input_text="Say PONG from stdin")
    check("chat reads stdin", code == 0 and out.strip() == "PONG", out + err)
    code, out, err = run("env", "--shell", "bash")
    check("env prints exports", code == 0 and f"export OPENAI_BASE_URL=http://127.0.0.1:{gw_port}/v1" in out
          and "ANTHROPIC_AUTH_TOKEN=" + key in out, out + err)
    code, out, err = run("start", "--port", str(gw_port))
    check("second start reports already running", code == 0 and "already running" in out, out + err)
    code, out, err = run("stop")
    check("stop stops the gateway and opencode", code == 0 and "Stopped the gateway" in out
          and "Stopped opencode serve" in out, out + err)
    time.sleep(1.5)
    check("gateway process is gone", not pid_alive(gpid), gpid)
    check("managed opencode process is gone", not pid_alive(opid), opid)
    check("state files cleaned up", not Path(home, "gateway.json").exists() and not Path(home, "opencode.json").exists())

    print(f"== {args.impl}: one-shot private gateway (no background gateway)")
    code, out, err = run("chat", "--no-stream", "--opencode-bin", str(FAKE_BIN), "--opencode-port", str(free_port()),
                         "hello", timeout=150)
    check("chat starts and stops everything itself", code == 0 and out.strip() == "PONG", out + err)
    time.sleep(1.0)
    check("its opencode was stopped afterwards", not Path(home, "opencode.json").exists())

    print(f"== {args.impl}: doctor")
    code, out, err = run("doctor", "--opencode-bin", str(FAKE_BIN), "--opencode-port", str(free_port()), "--test",
                         timeout=150)
    check("doctor --test passes with a working opencode", code == 0 and "test generation" in out
          and "FAIL" not in out, out + err)
    code, out, err = run("doctor", "--opencode-bin", str(Path(home) / "missing-opencode"))
    check("doctor fails clearly without opencode", code != 0 and "not found" in out, out + err)

    if args.with_claude:
        print(f"== {args.impl}: real Claude Code through `relayai claude`")
        code, out, err = run("claude", "--opencode-bin", str(FAKE_BIN), "--opencode-port", str(free_port()), "--",
                             "-p", "Run the shell command: echo hello", "--allowedTools", "Bash",
                             "--output-format", "json", timeout=300)
        # Claude Code may append notes (e.g. "Shell cwd was reset") after the command output
        check("Claude Code ran Bash and used the result", code == 0 and '"result":"Tool said: hello' in out
              and '"is_error":false' in out,
              out[-500:] + err[-500:])

    print(f"\n{args.impl}: {results['pass']} passed, {results['fail']} failed")
    return results["fail"]


def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(main())
