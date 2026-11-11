"""RelayAI: the one-line way to use opencode's models from Python."""
import secrets

from . import state
from .client import Client
from .config import Config
from .errors import ApiError
from .gateway import Gateway

GATEWAY_STATE = "gateway.json"


def running_gateway():
    """Details of the background gateway (`relayai start`) if it is up, else None."""
    saved = state.read_json(GATEWAY_STATE)
    if not saved or not state.pid_alive(saved.get("pid")):
        return None
    health = Client(saved["url"], saved.get("api_key", "")).health()
    return saved if health.get("service") == "RelayAI" else None


class RelayAI(Client):
    """
    Everything in one object: finds or starts opencode serve, runs a private
    gateway in a background thread, and gives you a client for it.

        from relayai import RelayAI

        with RelayAI() as ai:
            print(ai.chat("Hello"))
            print(ai.models())

    If a background gateway from `relayai start` is running, it is reused
    (pass reuse=False to always run a private one). Any Config field can be
    passed as a keyword, e.g. RelayAI(model="opencode/big-pickle", timeout=120).

    For the openai / anthropic SDKs use ai.base_url, ai.anthropic_base_url and ai.api_key.
    """

    def __init__(self, model=None, reuse=True, **settings):
        self.gateway = None
        explicit_upstream = settings.get("opencode_url")
        saved = running_gateway() if reuse and not explicit_upstream else None
        if saved:
            super().__init__(saved["url"], saved.get("api_key", ""), model=model or "default")
            return
        settings.setdefault("port", 0)
        settings.setdefault("host", "127.0.0.1")
        config = Config.from_env(**settings)
        if not config.api_key:
            config.api_key = "rai-" + secrets.token_urlsafe(24)
        if model and not config.model:
            config.model = model
        self.gateway = Gateway(config).start()
        super().__init__(self.gateway.url, config.api_key, model=model or "default", timeout=config.timeout + 30)

    def close(self):
        """Stop the private gateway (and the opencode serve it started, if any)."""
        if self.gateway:
            self.gateway.close()
            self.gateway = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


__all__ = ["RelayAI", "running_gateway", "ApiError"]
