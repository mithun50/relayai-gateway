"""Gateway settings. Precedence: explicit arguments, then RELAYAI_* / OPENCODE_* variables, then defaults."""
import os
from dataclasses import dataclass, fields


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


@dataclass
class Config:
    host: str = "127.0.0.1"
    """Interface the gateway listens on. 0.0.0.0 exposes it on the network."""
    port: int = 8000
    """Gateway port. 0 picks any free port."""
    api_key: str = ""
    """Key clients must send. Empty: the CLI uses a key stored in ~/.relayai/api_key."""
    model: str = ""
    """Model used for "default", as provider/model. Empty: opencode's own default."""
    strict_models: bool = True
    """Reject unknown model names on the OpenAI endpoint (Anthropic always falls back)."""
    timeout: int = 600
    """Seconds to wait for one answer."""
    max_concurrency: int = 4
    """Answers generated at the same time."""
    queue_wait: int = 30
    """Seconds a request may wait for a free slot before a 429."""
    model_cache_ttl: int = 30
    """Seconds the model list is cached."""
    keepalive: int = 10
    """Seconds between keepalive pings on a streaming response."""
    opencode_url: str = ""
    """URL of an opencode serve you run yourself. Empty: relayai starts and manages one."""
    opencode_username: str = "opencode"
    opencode_password: str = ""
    """Password of the opencode serve at opencode_url (opencode 2.x always needs one)."""
    opencode_port: int = 4096
    """Preferred port for the opencode serve relayai manages."""
    opencode_bin: str = ""
    """Path to the opencode executable. Empty: found on PATH."""

    @classmethod
    def from_env(cls, **overrides):
        """Settings from the environment, with any non-None keyword overriding them."""
        config = cls(
            host=os.environ.get("RELAYAI_HOST", cls.host),
            port=_int("RELAYAI_PORT", cls.port),
            api_key=os.environ.get("RELAYAI_API_KEY", "").strip(),
            model=os.environ.get("RELAYAI_MODEL", "").strip(),
            strict_models=_bool("RELAYAI_STRICT_MODELS", cls.strict_models),
            timeout=_int("RELAYAI_TIMEOUT", cls.timeout),
            max_concurrency=max(1, _int("RELAYAI_MAX_CONCURRENCY", cls.max_concurrency)),
            queue_wait=_int("RELAYAI_QUEUE_WAIT", cls.queue_wait),
            model_cache_ttl=_int("RELAYAI_MODEL_CACHE", cls.model_cache_ttl),
            keepalive=_int("RELAYAI_KEEPALIVE", cls.keepalive),
            opencode_url=os.environ.get("OPENCODE_URL", "").strip().rstrip("/"),
            opencode_username=os.environ.get("OPENCODE_SERVER_USERNAME", cls.opencode_username),
            opencode_password=os.environ.get("OPENCODE_SERVER_PASSWORD", ""),
            opencode_port=_int("RELAYAI_OPENCODE_PORT", cls.opencode_port),
            opencode_bin=os.environ.get("RELAYAI_OPENCODE_BIN", "").strip(),
        )
        names = {f.name for f in fields(cls)}
        for key, value in overrides.items():
            if key not in names:
                raise TypeError(f"unknown setting: {key}")
            if value is not None:
                setattr(config, key, value)
        config.opencode_url = (config.opencode_url or "").rstrip("/")
        return config
