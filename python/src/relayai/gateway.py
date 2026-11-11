"""
The HTTP gateway: OpenAI and Anthropic compatible endpoints in front of opencode.

    GET  /v1/models                   models opencode can use right now
    GET  /v1/models/{id}              one model
    POST /v1/chat/completions         OpenAI format, stream true or false, tools
    POST /v1/messages                 Anthropic format, stream true or false, tools
    POST /v1/messages/count_tokens    rough estimate
    GET  /health                      gateway and opencode status (no key needed)
"""
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import protocol as P
from ._version import __version__
from .config import Config
from .errors import ApiError
from .opencode import OpencodeClient, OpencodeManager

log = logging.getLogger("relayai")
MAX_BODY_BYTES = 50 * 1024 * 1024


class ModelCatalog:
    """The list of usable models, read from opencode and cached briefly."""

    def __init__(self, client, ttl):
        self.client = client
        self.ttl = ttl
        self._lock = threading.Lock()
        self._ids = []
        self._fetched = 0.0

    def ids(self, force=False):
        with self._lock:
            if self._ids and not force and time.time() - self._fetched < self.ttl:
                return list(self._ids)
            try:
                self._ids = self.client.model_ids()
                self._fetched = time.time()
            except ApiError:
                if not self._ids:
                    raise
                log.warning("could not refresh the model list from opencode, using the last known list")
            return list(self._ids)


class Gateway:
    """
    The gateway server.

        gw = Gateway(Config.from_env(api_key="secret", port=0))
        gw.start()               # background thread; gw.serve_forever() blocks instead
        print(gw.base_url)       # http://127.0.0.1:<port>/v1
        gw.close()

    Without config.opencode_url, opencode serve is started (or reused) through
    OpencodeManager. close() stops it only if this gateway started it and
    stop_opencode is True.
    """

    def __init__(self, config=None, opencode=None, stop_opencode=True):
        self.config = config or Config.from_env()
        if not self.config.api_key:
            raise ValueError("Config.api_key must be set")
        self.manager = None
        self.started_opencode = False
        self.stop_opencode = stop_opencode
        if opencode is not None:
            self.opencode = opencode
        elif self.config.opencode_url:
            self.opencode = OpencodeClient(self.config.opencode_url, self.config.opencode_username,
                                           self.config.opencode_password)
        else:
            self.manager = OpencodeManager(self.config.opencode_port, self.config.opencode_bin,
                                           self.config.opencode_username)
            self.opencode, self.started_opencode = self.manager.ensure()
        self.opencode.timeout = self.config.timeout
        self.catalog = ModelCatalog(self.opencode, self.config.model_cache_ttl)
        self.slots = threading.BoundedSemaphore(max(1, self.config.max_concurrency))
        self._server = _Server((self.config.host, self.config.port), _Handler)
        self._server.gateway = self
        self._thread = None
        self._closed = False

    # ---------------------------------------------------------------- lifecycle
    @property
    def port(self):
        return self._server.server_address[1]

    @property
    def url(self):
        host = self.config.host
        host = "127.0.0.1" if host in ("0.0.0.0", "", "::") else host
        return f"http://{host}:{self.port}"

    @property
    def base_url(self):
        """OpenAI base URL (ends in /v1)."""
        return self.url + "/v1"

    def start(self):
        """Serve in a background thread and return self."""
        if not self._thread:
            self._thread = threading.Thread(target=self._server.serve_forever, name="relayai-gateway", daemon=True)
            self._thread.start()
        return self

    def serve_forever(self):
        try:
            self._server.serve_forever()
        finally:
            self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._server.shutdown() if self._thread else None
        except Exception:
            pass
        self._server.server_close()
        if self.manager and self.started_opencode and self.stop_opencode:
            self.manager.stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ models
    def resolve_model(self, requested, strict=None):
        """
        Client model name -> (opencode model dict or None, name to report).
        None means: let opencode use its own default model.
        """
        strict = self.config.strict_models if strict is None else strict
        default = self.config.model
        name = (requested or "").strip()
        if name.lower() in P.DEFAULT_NAMES:
            name = default
            if not name:
                return None, "default"

        ids = self.catalog.ids()
        if name not in ids and strict:
            ids = self.catalog.ids(force=True)  # it may have been added just now
        if name in ids:
            return _split(name), name
        bare = [i for i in ids if i.split("/", 1)[1] == name]
        if len(bare) == 1:
            return _split(bare[0]), bare[0]
        if len(bare) > 1:
            raise ApiError(400, f"Model '{name}' exists under several providers. Use one of: {', '.join(bare)}",
                           code="model_ambiguous")
        if not strict:
            if default in ids:
                return _split(default), default
            return None, "default"
        raise ApiError(404, f"The model '{name}' is not available in opencode. "
                            "Call GET /v1/models for the list, or use 'default'.", code="model_not_found")

    def generate(self, system, turns, tools, force, model, box):
        system, prompt = P.fold(system, turns, tools, force)
        text, usage, used = self.opencode.generate(system, prompt, model, box)
        text, calls = P.finish(text, tools)
        return {"text": text, "calls": calls, "usage": usage, "used": used}


def _split(model_id):
    provider, model = model_id.split("/", 1)
    return {"providerID": provider, "modelID": model}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    # Reuse lets a restart bind right away on Linux/macOS; on Windows it would
    # let two servers share one port, so it stays off there.
    allow_reuse_address = os.name != "nt"


class _Handler(BaseHTTPRequestHandler):
    server_version = f"RelayAI/{__version__}"
    timeout = 120  # a client that connects and then says nothing is dropped
    _anthropic = False
    _status = 0

    @property
    def gw(self):
        return self.server.gateway

    # ------------------------------------------------------------------ plumbing
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, status, obj):
        self._status = status
        payload = json.dumps(obj).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self._cors()
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass  # the client is already gone

    def _fail(self, err):
        if err.status >= 500:
            log.warning("%s %s -> %s %s", self.command, self.path, err.status, err.message)
        self._json(err.status, err.anthropic_body() if self._anthropic else err.body())

    def _sse(self, obj, event=None):
        data = obj if isinstance(obj, str) else json.dumps(obj)
        prefix = f"event: {event}\n" if event else ""
        self.wfile.write(f"{prefix}data: {data}\n\n".encode())
        self.wfile.flush()

    def _route(self):
        path = urllib.parse.unquote(self.path.split("?", 1)[0]).rstrip("/") or "/"
        if path not in ("/", "/health") and not path.startswith("/v1"):
            path = "/v1" + path  # tolerate base URLs given without /v1
        self._anthropic = path.startswith("/v1/messages")
        return path

    def _check_key(self):
        auth = self.headers.get("Authorization", "")
        given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not given:
            given = self.headers.get("x-api-key", "") or self.headers.get("api-key", "")
        if not secrets.compare_digest(given.encode(), self.gw.config.api_key.encode()):
            raise ApiError(401, "Incorrect API key provided.", "invalid_request_error", "invalid_api_key")

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Invalid Content-Length header")
        if length <= 0:
            raise ApiError(400, "Request body is empty")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, f"Request body is larger than {MAX_BODY_BYTES // (1024 * 1024)} MB")
        try:
            req = json.loads(self.rfile.read(length))
        except (ValueError, OSError):
            raise ApiError(400, "Request body is not valid JSON")
        if not isinstance(req, dict):
            raise ApiError(400, "Request body must be a JSON object")
        return req

    def _take_slot(self):
        if not self.gw.slots.acquire(timeout=self.gw.config.queue_wait):
            raise ApiError(429, f"RelayAI is busy ({self.gw.config.max_concurrency} requests running). "
                                "Try again shortly.", "rate_limit_error", "server_busy")

    # -------------------------------------------------------------------- routes
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_HEAD(self):  # connection warm-up probes
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        try:
            path = self._route()
            if path in ("/", "/health"):
                try:
                    reachable, version = self.gw.opencode.health()
                except ApiError:
                    reachable, version = False, None
                body = {"status": "ok" if reachable else "degraded", "service": "RelayAI", "version": __version__,
                        "opencode": {"url": self.gw.opencode.url, "reachable": reachable, "version": version}}
                return self._json(200 if reachable else 503, body)
            self._check_key()
            if path == "/v1/models":
                return self._json(200, {"object": "list", "data": [P.model_json(m) for m in self.gw.catalog.ids()]})
            if path.startswith("/v1/models/"):
                _, name = self.gw.resolve_model(path[len("/v1/models/"):], strict=True)
                return self._json(200, P.model_json(name))
            raise ApiError(404, f"Unknown endpoint: GET {path}")
        except ApiError as e:
            self._fail(e)
        except Exception as e:
            log.exception("unexpected error")
            self._fail(ApiError(500, f"RelayAI error: {e}", "server_error"))

    def do_POST(self):
        started = time.time()
        self._status = 0
        note = "?"
        try:
            path = self._route()
            self._check_key()
            if path == "/v1/chat/completions":
                note = self._openai_chat(self._read_json())
            elif path == "/v1/messages":
                note = self._anthropic_messages(self._read_json())
            elif path == "/v1/messages/count_tokens":
                req = self._read_json()
                self._json(200, {"input_tokens": max(1, len(json.dumps(req)) // 4)})
                note = "count_tokens"
            else:
                raise ApiError(404, f"Unknown endpoint: POST {path}")
        except ApiError as e:
            self._fail(e)
        except Exception as e:  # never leak a stack trace to the client
            log.exception("unexpected error")
            self._fail(ApiError(500, f"RelayAI error: {e}", "server_error"))
        finally:
            if note != "count_tokens":
                log.info("POST %s %s status=%s %.1fs", self.path.split("?")[0], note, self._status,
                         time.time() - started)

    # ----------------------------------------------------------- OpenAI chat
    def _openai_chat(self, req):
        system, turns, tools, force = P.openai_to_core(req)
        P.fold(system, turns, tools, force)  # validate before taking a slot
        model, model_name = self.gw.resolve_model(req.get("model"))
        completion_id = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        note = f"model={model_name} tools={len(tools)}"
        self._take_slot()

        def job(box):
            return self.gw.generate(system, turns, tools, force, model, box)

        if not req.get("stream"):
            try:
                result = job({})
            finally:
                self.gw.slots.release()
            message = {"role": "assistant", "content": result["text"] or None}
            if result["calls"]:
                message["tool_calls"] = P.openai_tool_calls(result["calls"])
            self._json(200, {
                "id": completion_id, "object": "chat.completion", "created": created,
                "model": result["used"] or model_name,
                "choices": [{"index": 0, "message": message, "logprobs": None,
                             "finish_reason": "tool_calls" if result["calls"] else "stop"}],
                "usage": result["usage"],
            })
            return note

        def on_open():
            self._sse(P.chunk_json(completion_id, created, model_name, {"role": "assistant", "content": ""}))

        def on_ping():
            self.wfile.write(b": keepalive\n\n")
            self.wfile.flush()

        def on_result(result):
            name = result["used"] or model_name
            text = result["text"]
            for i in range(0, len(text), 48):
                self._sse(P.chunk_json(completion_id, created, name, {"content": text[i:i + 48]}))
            for index, call in enumerate(P.openai_tool_calls(result["calls"])):
                call["index"] = index
                self._sse(P.chunk_json(completion_id, created, name, {"tool_calls": [call]}))
            self._sse(P.chunk_json(completion_id, created, name, {}, "tool_calls" if result["calls"] else "stop"))
            if (req.get("stream_options") or {}).get("include_usage"):
                final = P.chunk_json(completion_id, created, name, {})
                final["choices"] = []
                final["usage"] = result["usage"]
                self._sse(final)
            self._sse("[DONE]")

        def on_error(err):
            self._sse(err.body())
            self._sse("[DONE]")

        self._stream(job, on_open, on_ping, on_result, on_error)
        return note

    # ------------------------------------------- Anthropic messages (Claude Code)
    def _anthropic_messages(self, req):
        system, turns, tools, force = P.anthropic_to_core(req)
        P.fold(system, turns, tools, force)  # validate before taking a slot
        requested = str(req.get("model") or "default")
        model, model_name = self.gw.resolve_model(requested, strict=False)
        message_id = "msg_" + uuid.uuid4().hex[:24]
        note = f"model={model_name} (asked {requested}) tools={len(tools)}"
        self._take_slot()

        def job(box):
            return self.gw.generate(system, turns, tools, force, model, box)

        def envelope(content, stop_reason, usage):
            return {"id": message_id, "type": "message", "role": "assistant", "model": requested,
                    "content": content, "stop_reason": stop_reason, "stop_sequence": None, "usage": usage}

        if not req.get("stream"):
            try:
                result = job({})
            finally:
                self.gw.slots.release()
            stop = "tool_use" if result["calls"] else "end_turn"
            self._json(200, envelope(P.anthropic_content(result), stop, P.anthropic_usage(result["usage"])))
            return note

        def on_open():
            start = envelope([], None, {"input_tokens": 0, "output_tokens": 0})
            self._sse({"type": "message_start", "message": start}, "message_start")
            self._sse({"type": "ping"}, "ping")

        def on_ping():
            self._sse({"type": "ping"}, "ping")

        def on_result(result):
            for index, block in enumerate(P.anthropic_content(result)):
                if block["type"] == "text":
                    opening = {"type": "text", "text": ""}
                    deltas = [{"type": "text_delta", "text": block["text"][i:i + 64]}
                              for i in range(0, len(block["text"]), 64)]
                else:
                    opening = dict(block, input={})
                    deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
                self._sse({"type": "content_block_start", "index": index, "content_block": opening},
                          "content_block_start")
                for delta in deltas:
                    self._sse({"type": "content_block_delta", "index": index, "delta": delta}, "content_block_delta")
                self._sse({"type": "content_block_stop", "index": index}, "content_block_stop")
            stop = "tool_use" if result["calls"] else "end_turn"
            self._sse({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                       "usage": P.anthropic_usage(result["usage"])}, "message_delta")
            self._sse({"type": "message_stop"}, "message_stop")

        def on_error(err):
            self._sse(err.anthropic_body(), "error")

        self._stream(job, on_open, on_ping, on_result, on_error)
        return note

    # ------------------------------------------------------ shared streaming loop
    def _stream(self, job, on_open, on_ping, on_result, on_error):
        """
        opencode answers in one piece, so the reply is sent once it is ready,
        not token by token. While the model works, pings keep the connection
        open and reveal a client that hung up, so the opencode run can be
        cancelled. The worker thread releases the concurrency slot.
        """
        box, out = {}, {}

        def work():
            try:
                out["ok"] = job(box)
            except ApiError as e:
                out["err"] = e
            except Exception as e:
                log.exception("unexpected error")
                out["err"] = ApiError(500, f"RelayAI error: {e}", "server_error")
            finally:
                self.gw.slots.release()

        worker = threading.Thread(target=work, daemon=True)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self._cors()
            self.end_headers()
            self._status = 200
        except OSError:
            self.gw.slots.release()
            return
        worker.start()
        try:
            on_open()
            while worker.is_alive():
                worker.join(self.gw.config.keepalive)
                if worker.is_alive():
                    on_ping()
            if "err" in out:
                self._status = out["err"].status
                if out["err"].status >= 500:
                    log.warning("stream failed: %s", out["err"].message)
                on_error(out["err"])
            else:
                on_result(out["ok"])
        except OSError:
            # the client hung up: stop the model run instead of paying for it
            self._status = 499
            box["cancelled"] = True
            if box.get("session") and worker.is_alive():
                log.info("client disconnected, cancelling opencode session %s", box["session"])
                self.gw.opencode.cancel(box["session"])

    def log_message(self, fmt, *args):
        pass
