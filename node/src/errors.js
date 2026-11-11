"use strict";
/** Error types shared by the gateway, the SDK and the CLI. */

class RelayAIError extends Error {}

/**
 * An error with an HTTP status, rendered in OpenAI shape on OpenAI routes and
 * in Anthropic shape on /v1/messages.
 */
class ApiError extends RelayAIError {
  constructor(status, message, kind = "invalid_request_error", code = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind;
    this.code = code;
  }

  body() {
    return { error: { message: this.message, type: this.kind, param: null, code: this.code } };
  }

  anthropicBody() {
    const kinds = {
      400: "invalid_request_error",
      401: "authentication_error",
      404: "not_found_error",
      413: "request_too_large",
      429: "rate_limit_error",
      503: "overloaded_error",
    };
    return { type: "error", error: { type: kinds[this.status] || "api_error", message: this.message } };
  }
}

class OpencodeNotInstalled extends RelayAIError {
  constructor(message) {
    super(
      message ||
        "opencode is not installed (or not on PATH). Install it with " +
          "`relayai install-opencode` or `npm install -g @opencode/cli`, " +
          "or point relayai at a running server with --opencode-url."
    );
    this.name = "OpencodeNotInstalled";
  }
}

module.exports = { RelayAIError, ApiError, OpencodeNotInstalled };
