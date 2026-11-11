"""
relayai: OpenAI and Anthropic compatible API for the models in opencode,
with emulated tool calling, usable as a CLI, a server, or a Python SDK.

    from relayai import RelayAI

    with RelayAI() as ai:
        print(ai.chat("Hello"))
"""
from ._version import __version__
from .client import Client
from .config import Config
from .errors import ApiError, OpencodeNotInstalled, RelayAIError
from .gateway import Gateway
from .opencode import OpencodeClient, OpencodeManager
from .protocol import parse_tool_calls
from .sdk import RelayAI, running_gateway

__all__ = [
    "__version__",
    "RelayAI",
    "Client",
    "Gateway",
    "Config",
    "OpencodeClient",
    "OpencodeManager",
    "ApiError",
    "OpencodeNotInstalled",
    "RelayAIError",
    "running_gateway",
    "parse_tool_calls",
]
