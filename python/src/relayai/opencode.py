"""
Talking to opencode, and running it.

OpencodeClient  speaks opencode's own HTTP API (1.x and 2.x, detected automatically).
OpencodeManager finds or installs the opencode executable and runs `opencode serve`
                in the background with a generated password.
"""
import base64
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import state
from .errors import ApiError, OpencodeNotInstalled

log = logging.getLogger("relayai")

# opencode 1.x only: its tools are switched off per request. The full list is
# read from opencode, this is the fallback.
FALLBACK_TOOLS_OFF = {"bash": False, "edit": False, "write": False, "patch": False}


class OpencodeClient:
    """A small client for one opencode server."""

    def __init__(self, url, username="opencode", password="", timeout=600):
        self.url = url.rstrip("/")
        self.username = username or "opencode"
        self.password = password or ""
        self.timeout = timeout
        self._version = None
        self._tools_off = {"at": 0.0, "map": None}
        self._lock = threading.Lock()

    def __repr__(self):
        return f"OpencodeClient({self.url!r})"

    # ------------------------------------------------------------ transport
    def request(self, method, path, body=None, timeout=30, retries=0):
        """
        One HTTP call to opencode, returning parsed JSON (or None for an empty body).
        `retries` only applies when no connection could be made at all, so a
        request is never sent twice.
        """
        headers = {"Content-Type": "application/json"}
        if self.password:
            token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
            headers["Authorization"] = "Basic " + token
        data = json.dumps(body).encode() if body is not None else None

        attempt = 0
        while True:
            req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as res:
                    raw = res.read().decode(errors="replace")
                    html = "text/html" in (res.headers.get("Content-Type") or "")
                if html or raw.lstrip()[:15].lower().startswith("<!doctype"):
                    # opencode 2.x serves its web UI for every path it does not know
                    raise ApiError(502, f"opencode has no API at {method} {path} (it sent a web page)",
                                   "upstream_wrong_path")
                if not raw.strip():
                    return None
                try:
                    return json.loads(raw)
                except ValueError:
                    raise ApiError(502, f"opencode sent a reply that is not JSON: {raw[:200]}", "upstream_error")
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:600]
                if e.code == 401:
                    raise ApiError(
                        502,
                        "opencode rejected the login. Set OPENCODE_SERVER_PASSWORD (and "
                        "OPENCODE_SERVER_USERNAME if changed) to match the opencode server.",
                        "upstream_auth",
                    )
                try:
                    parsed = json.loads(detail)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("message"):  # 2.x error shape: {_tag, message}
                    tag = parsed.get("_tag") or "Error"
                    if tag == "ServiceUnavailableError":
                        # a provider quota, rate limit or outage: worth retrying later
                        raise ApiError(503, f"opencode: {parsed['message']}", "upstream_unavailable")
                    raise ApiError(502, f"opencode {tag} for {method} {path}: {parsed['message']}", "upstream_error")
                raise ApiError(502, f"opencode returned HTTP {e.code} for {method} {path}: {detail}", "upstream_error")
            except (socket.timeout, TimeoutError):
                raise ApiError(504, f"opencode did not answer within {timeout} seconds.", "upstream_timeout")
            except (urllib.error.URLError, OSError) as e:
                reason = getattr(e, "reason", e)
                if isinstance(reason, (socket.timeout, TimeoutError)):
                    raise ApiError(504, f"opencode did not answer within {timeout} seconds.", "upstream_timeout")
                if isinstance(reason, ConnectionRefusedError) and attempt < retries:
                    attempt += 1
                    time.sleep(0.5 * attempt)
                    continue
                raise ApiError(502, f"Cannot reach opencode at {self.url} ({reason}). Is `opencode serve` running?",
                               "upstream_unreachable")

    # ------------------------------------------------------------ discovery
    def api_version(self):
        """'v2' for opencode 2.x (routes under /api), else 'v1'. Remembered once known."""
        with self._lock:
            if self._version:
                return self._version
        version = "v1"
        try:
            info = self.request("GET", "/api/info", timeout=10, retries=2)
            if isinstance(info, dict) and info.get("version"):
                version = "v2"
        except ApiError as e:
            if e.kind in ("upstream_unreachable", "upstream_timeout", "upstream_auth"):
                raise
        with self._lock:
            self._version = version
        return version

    def health(self):
        """(reachable, version). Raises only for a wrong password."""
        try:
            path = "/api/info" if self.api_version() == "v2" else "/global/health"
            info = self.request("GET", path, timeout=5)
            return True, info.get("version") if isinstance(info, dict) else None
        except ApiError as e:
            if e.kind == "upstream_auth":
                raise
            if e.kind in ("upstream_unreachable", "upstream_timeout"):
                return False, None
            return True, None

    def model_ids(self):
        """Every usable model as provider/model, read live from opencode."""
        if self.api_version() == "v2":
            listing = self.request("GET", "/api/model", timeout=15, retries=2) or {}
            return sorted({
                f"{m['providerID']}/{m['modelID']}"
                for m in listing.get("data") or []
                if isinstance(m, dict) and m.get("providerID") and m.get("modelID") and m.get("enabled", True)
            })
        config = self.request("GET", "/config/providers", timeout=15, retries=2) or {}
        ids = set()
        for provider in config.get("providers") or []:
            provider_id = provider.get("id")
            models = provider.get("models") or {}
            keys = list(models.keys()) if isinstance(models, dict) else [m.get("id") for m in models if isinstance(m, dict)]
            ids.update(f"{provider_id}/{k}" for k in keys if provider_id and k)
        return sorted(ids)

    # ------------------------------------------------------------ generation
    def generate(self, system, prompt, model=None, state_box=None):
        """
        One answer from opencode. `model` is {"providerID", "modelID"} or None for
        opencode's default. `state_box` (a dict) receives the session id so the
        caller can cancel. Returns (text, usage, model_id_used_or_None).
        """
        box = state_box if state_box is not None else {}
        if self.api_version() == "v2":
            return self._generate_v2(system, prompt, model, box)
        return self._generate_v1(system, prompt, model, box)

    def cancel(self, session_id):
        if self.api_version() == "v2":
            path, body = f"/api/session/{session_id}/interrupt", {}
        else:
            path, body = f"/session/{session_id}/abort", None
        try:
            self.request("POST", path, body, timeout=10)
        except ApiError:
            pass

    def _generate_v2(self, system, prompt, model, box):
        """
        opencode 2.x: a throwaway session answered with POST /api/session/:id/generate,
        which does not run opencode's agent loop, so opencode's tools never run.

        Measured on opencode 2.0.31 with its free models:
          - POST /api/experimental/generate (stateless) is refused with
            "Free usage limit reached" even when sessions work, so it is not used.
          - A session created with a `permissions` list is refused the same way,
            so none is sent.
        The call takes one prompt string, so the system text goes on top. It
        reports no token counts, so usage is estimated at ~4 characters per token.
        """
        create = {"title": "RelayAI"}
        if model:
            create["model"] = {"providerID": model["providerID"], "id": model["modelID"]}
        session = self.request("POST", "/api/session", create, retries=2)
        session_id = ((session or {}).get("data") or {}).get("id")
        if not session_id:
            raise ApiError(502, "opencode did not return a session id", "upstream_error")
        box["session"] = session_id
        full = f"# Instructions\n\n{system}\n\n# Request\n\n{prompt}"
        try:
            result = self.request("POST", f"/api/session/{session_id}/generate", {"prompt": full}, timeout=self.timeout)
        finally:
            try:
                self.request("DELETE", f"/api/session/{session_id}", timeout=15)
            except ApiError as e:
                log.warning("could not delete opencode session %s: %s", session_id, e.message)
        if box.get("cancelled"):
            raise ApiError(499, "Client closed the request", "cancelled")
        text = (((result or {}).get("data") or {}).get("text") or "").strip()
        if not text:
            raise ApiError(502, EMPTY_REPLY, "upstream_empty")
        prompt_tokens = max(1, len(full) // 4)
        completion_tokens = max(1, len(text) // 4)
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                 "total_tokens": prompt_tokens + completion_tokens}
        used = f"{model['providerID']}/{model['modelID']}" if model else None
        return text, usage, used

    def _tools_off_map(self):
        cache = self._tools_off
        if cache["map"] is None or time.time() - cache["at"] > 300:
            try:
                ids = self.request("GET", "/experimental/tool/ids", timeout=10)
                if isinstance(ids, list) and ids:
                    merged = dict(FALLBACK_TOOLS_OFF)
                    merged.update({str(i): False for i in ids})
                    cache["map"], cache["at"] = merged, time.time()
            except ApiError:
                pass
        return cache["map"] or FALLBACK_TOOLS_OFF

    def _generate_v1(self, system, prompt, model, box):
        """opencode 1.x: a throwaway session with all of opencode's tools switched off."""
        session = self.request("POST", "/session", {"title": "RelayAI"}, retries=2)
        session_id = (session or {}).get("id")
        if not session_id:
            raise ApiError(502, "opencode did not return a session id", "upstream_error")
        box["session"] = session_id
        finished = False
        try:
            body = {"parts": [{"type": "text", "text": prompt}], "system": system, "tools": self._tools_off_map()}
            if model:
                body["model"] = model
            result = self.request("POST", f"/session/{session_id}/message", body, timeout=self.timeout)
            finished = True
            if box.get("cancelled"):
                raise ApiError(499, "Client closed the request", "cancelled")
            if not isinstance(result, dict):
                raise ApiError(502, "opencode returned an empty response. Check that a provider and model "
                                    "are set up in opencode.", "upstream_error")
            info = result.get("info") or {}
            if info.get("error"):
                raise ApiError(502, "opencode error: " + _error_text(info["error"]), "upstream_error")
            texts = [p.get("text", "") for p in result.get("parts") or []
                     if isinstance(p, dict) and p.get("type") == "text"]
            text = "\n".join(t for t in texts if t).strip()
            if not text:
                raise ApiError(502, EMPTY_REPLY, "upstream_empty")
            tokens = info.get("tokens") or {}
            prompt_tokens = int(tokens.get("input") or 0)
            completion_tokens = int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
            usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                     "total_tokens": prompt_tokens + completion_tokens}
            used = f"{info['providerID']}/{info['modelID']}" if info.get("providerID") and info.get("modelID") else None
            return text, usage, used
        finally:
            if not finished:
                self.cancel(session_id)
            try:
                self.request("DELETE", f"/session/{session_id}", timeout=15)
            except ApiError as e:
                log.warning("could not delete opencode session %s: %s", session_id, e.message)


EMPTY_REPLY = (
    "The model returned no text. Some models answer a tool request with opencode's own "
    "native tool call instead of text; try another model (see `relayai models`)."
)


def _error_text(err):
    if isinstance(err, dict):
        data = err.get("data")
        if isinstance(data, dict) and data.get("message"):
            return f"{err.get('name', 'Error')}: {data['message']}"
    return json.dumps(err)[:600]


# ---------------------------------------------------------------------- manager

class OpencodeManager:
    """
    Runs `opencode serve` for relayai.

    ensure() reuses the server relayai started earlier when it is still alive,
    otherwise starts a new one on a free port with a fresh password, in the
    background, inside ~/.relayai/workspace. Its details live in
    ~/.relayai/opencode.json so other relayai processes (Python or Node) share it.
    """

    STATE = "opencode.json"
    INSTALL_PACKAGE = "@opencode/cli"

    def __init__(self, port=4096, binary="", username="opencode"):
        self.port = port
        self.binary = binary
        self.username = username or "opencode"

    # -------------------------------------------------------------- install
    def find_binary(self):
        if self.binary:
            return self.binary if (os.path.isfile(self.binary) or shutil.which(self.binary)) else None
        return shutil.which("opencode")

    def version(self):
        """Installed opencode version string, or None."""
        binary = self.find_binary()
        if not binary:
            return None
        try:
            out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        match = re.search(r"\d+\.\d+\.\d+\S*", (out.stdout or "") + (out.stderr or ""))
        return match.group(0) if match else None

    @classmethod
    def install_command(cls):
        npm = shutil.which("npm")
        return [npm, "install", "-g", cls.INSTALL_PACKAGE] if npm else None

    @classmethod
    def install(cls):
        """Install opencode globally with npm. Returns the process exit code."""
        cmd = cls.install_command()
        if not cmd:
            raise OpencodeNotInstalled(
                "npm was not found, so opencode cannot be installed automatically. "
                "Install Node.js from https://nodejs.org and run `npm install -g @opencode/cli`, "
                "or follow https://opencode.ai/docs."
            )
        return subprocess.call(cmd)

    # --------------------------------------------------------------- server
    def running(self):
        """The managed server's saved details if it is alive and answering, else None."""
        saved = state.read_json(self.STATE)
        if not saved or not state.pid_alive(saved.get("pid")):
            return None
        client = OpencodeClient(saved["url"], self.username, saved.get("password", ""))
        try:
            reachable, _ = client.health()
        except ApiError:
            return None
        return saved if reachable else None

    def ensure(self, timeout=90):
        """Return (OpencodeClient, started_now)."""
        saved = self.running()
        if saved:
            return OpencodeClient(saved["url"], self.username, saved.get("password", "")), False

        binary = self.find_binary()
        if not binary:
            raise OpencodeNotInstalled()
        binary = state.resolve_windows_shim(binary)
        port = state.free_port(self.port)
        password = secrets.token_urlsafe(24)
        url = f"http://127.0.0.1:{port}"
        env = dict(os.environ, OPENCODE_SERVER_PASSWORD=password, OPENCODE_SERVER_USERNAME=self.username)
        log.info("starting opencode serve on port %d", port)
        pid = state.spawn_detached([binary, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
                                   "opencode.log", env=env, cwd=str(state.workspace()))
        client = OpencodeClient(url, self.username, password)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not state.pid_alive(pid):
                raise ApiError(502, f"opencode serve exited right after starting. See {state.home() / 'opencode.log'}",
                               "upstream_unreachable")
            try:
                reachable, version = client.health()
            except ApiError:
                reachable, version = False, None
            if reachable:
                state.write_json(self.STATE, {"pid": pid, "url": url, "port": port, "password": password,
                                              "version": version, "started": int(time.time())})
                return client, True
            time.sleep(0.5)
        state.kill_tree(pid)
        raise ApiError(504, f"opencode serve did not come up within {timeout}s. See {state.home() / 'opencode.log'}",
                       "upstream_timeout")

    def stop(self):
        """Stop the managed server. Returns True if one was running."""
        saved = state.read_json(self.STATE)
        state.remove(self.STATE)
        if saved and state.pid_alive(saved.get("pid")):
            state.kill_tree(saved["pid"])
            return True
        return False
