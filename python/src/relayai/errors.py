"""Error types shared by the gateway, the SDK and the CLI."""


class RelayAIError(Exception):
    """Base class for every error raised by relayai."""


class ApiError(RelayAIError):
    """
    An error with an HTTP status, rendered in OpenAI shape on OpenAI routes
    and in Anthropic shape on /v1/messages.
    """

    def __init__(self, status, message, kind="invalid_request_error", code=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.kind = kind
        self.code = code

    def body(self):
        return {"error": {"message": self.message, "type": self.kind, "param": None, "code": self.code}}

    def anthropic_body(self):
        kinds = {400: "invalid_request_error", 401: "authentication_error", 404: "not_found_error",
                 413: "request_too_large", 429: "rate_limit_error", 503: "overloaded_error"}
        return {"type": "error", "error": {"type": kinds.get(self.status, "api_error"), "message": self.message}}


class OpencodeNotInstalled(RelayAIError):
    """opencode could not be found on this machine."""

    def __init__(self, message=None):
        super().__init__(message or (
            "opencode is not installed (or not on PATH). Install it with "
            "`relayai install-opencode` or `npm install -g @opencode/cli`, "
            "or point relayai at a running server with --opencode-url."
        ))
