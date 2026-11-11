"""A small client for a running RelayAI gateway (or any OpenAI-compatible server)."""
import json
import urllib.error
import urllib.request

from .errors import ApiError


class Client:
    """
    Talks to a running gateway.

        client = Client("http://127.0.0.1:8000", api_key="...")
        print(client.chat("Hello"))
        for piece in client.stream("Tell me a story"):
            print(piece, end="")
    """

    def __init__(self, base_url="http://127.0.0.1:8000", api_key="", model="default", timeout=600):
        url = base_url.rstrip("/")
        if url.endswith("/v1"):
            url = url[:-3]
        self.url = url
        self.api_key = api_key
        self.model = model or "default"
        self.timeout = timeout

    @property
    def base_url(self):
        """OpenAI base URL, for the openai SDK and similar clients."""
        return self.url + "/v1"

    @property
    def anthropic_base_url(self):
        """Anthropic base URL, for the anthropic SDK and Claude Code."""
        return self.url

    # -------------------------------------------------------------- transport
    def _open(self, method, path, body=None, timeout=None):
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                err = json.loads(raw).get("error") or {}
                message = err.get("message") if isinstance(err, dict) else str(err)
                kind = err.get("type", "api_error") if isinstance(err, dict) else "api_error"
                code = err.get("code") if isinstance(err, dict) else None
            except (ValueError, AttributeError):
                message, kind, code = raw[:300] or e.reason, "api_error", None
            raise ApiError(e.code, message or f"HTTP {e.code}", kind, code) from None
        except urllib.error.URLError as e:
            raise ApiError(502, f"Cannot reach the gateway at {self.url} ({e.reason})", "connection_error") from None

    def _json(self, method, path, body=None, timeout=None):
        with self._open(method, path, body, timeout) as res:
            return json.loads(res.read() or b"null")

    # ---------------------------------------------------------------- endpoints
    def health(self):
        """Gateway and opencode status. Never raises; a degraded gateway answers 503 with the same body."""
        req = urllib.request.Request(self.url + "/health")
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read() or b"{}")
            except ValueError:
                return {"status": "down", "error": f"HTTP {e.code}"}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"status": "down", "error": str(getattr(e, "reason", e))}

    def models(self):
        """Model ids opencode can use right now, as provider/model."""
        return [m["id"] for m in self._json("GET", "/v1/models")["data"]]

    def complete(self, **body):
        """Raw POST /v1/chat/completions. Returns the OpenAI response dict."""
        body.setdefault("model", self.model)
        return self._json("POST", "/v1/chat/completions", body)

    def messages(self, **body):
        """Raw POST /v1/messages (Anthropic format). Returns the Anthropic response dict."""
        body.setdefault("model", self.model)
        body.setdefault("max_tokens", 4096)
        return self._json("POST", "/v1/messages", body)

    def chat(self, prompt, model=None, system=None, **extra):
        """
        Ask something and get the answer text. `prompt` is a string or a list of
        OpenAI-style messages. Extra keywords go into the request body.
        """
        reply = self.complete(model=model or self.model, messages=_messages(prompt, system), **extra)
        return reply["choices"][0]["message"].get("content") or ""

    def stream(self, prompt, model=None, system=None, **extra):
        """Like chat(), but yields the answer in pieces as they arrive."""
        body = dict(extra, model=model or self.model, messages=_messages(prompt, system), stream=True)
        with self._open("POST", "/v1/chat/completions", body) as res:
            for raw in res:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    return
                event = json.loads(data)
                if "error" in event:
                    err = event["error"]
                    raise ApiError(502, err.get("message", "stream failed"), err.get("type", "api_error"))
                for choice in event.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        yield piece


def _messages(prompt, system):
    messages = list(prompt) if isinstance(prompt, (list, tuple)) else [{"role": "user", "content": str(prompt)}]
    if system:
        messages.insert(0, {"role": "system", "content": system})
    return messages
