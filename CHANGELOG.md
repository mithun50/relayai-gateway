# Changelog

All notable changes to RelayAI are recorded here. Both packages (`relayai-gateway` on PyPI and npm) are released together with the same version.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions are **date based**: `YYYY.M.D` is the day of the release (for example `2026.11.11`), with at most one release per day. A version number says when a release was made, not how big it is, so read the notes below to see whether a release changes behaviour you rely on; such changes are listed under **Changed** or **Removed**.

## [Unreleased]

## [2026.11.12] - 2026-11-12

### Fixed

- opencode 2.x could be mistaken for opencode 1.x when relayai checked it in the first moments after starting it. The gateway then failed every request with `opencode returned HTTP 405 for POST /session` or `opencode has no API at ... (it sent a web page)` until it was restarted. relayai now decides the opencode version only from a clear answer (2.x's `/api/info` or 1.x's `/global/health`) and keeps waiting while opencode is still starting; a request that arrives in that moment gets a retryable 503 instead of a broken gateway. If you hit this on 2026.11.11, restart the gateway (`relayai stop` then `relayai start`, or Ctrl+C and `relayai serve`).
- A garbled or half-open reply from opencode (possible while it starts) is now reported as "not reachable yet" instead of an unexpected error.

## [2026.11.11] - 2026-11-11

### Added

- First release of the `relayai-gateway` packages for Python (PyPI) and Node (npm), with the same features and the same `relayai` command.
- OpenAI-compatible API: `POST /v1/chat/completions` (streaming, tools, `tool_choice`, legacy `functions`), `GET /v1/models`, `GET /v1/models/{id}`.
- Anthropic-compatible API: `POST /v1/messages` (streaming, tools, `tool_choice`), `POST /v1/messages/count_tokens`.
- Emulated tool calling through `<tool_call>` blocks, turned into OpenAI `tool_calls` and Anthropic `tool_use`; tolerant of missing closing tags and raw newlines in JSON strings.
- `GET /health` for monitoring (200 when opencode is reachable, 503 when not).
- Models are read live from opencode and cached for 30 seconds; short model names, `default`, and fallback to the default model on the Anthropic endpoint.
- Support for opencode 1.x and 2.x, detected automatically, including opencode 2.x password authentication.
- opencode management: finds the `opencode` executable, starts `opencode serve` in the background with a generated password and a private working folder (`~/.relayai/workspace`), reuses it across commands, and stops it again.
- CLI commands: `serve`, `start`, `stop`, `status`, `models`, `chat` (one-shot, stdin, interactive), `env`, `claude`, `doctor`, `install-opencode`.
- Python SDK: `RelayAI`, `Client`, `Gateway`, `Config`, `OpencodeClient`, `OpencodeManager`, `ApiError`.
- Node SDK with TypeScript types: `RelayAI.start()`, `Client`, `Gateway.create()`, `configFromEnv()`, `OpencodeClient`, `OpencodeManager`, `ApiError`; CommonJS and ESM imports.
- Persistent API key in `~/.relayai/api_key`; state shared between the Python and Node packages.
- Concurrency limit with a wait queue (429 when busy), per-answer timeout (504), keepalives while streaming, cancellation of the opencode run when a streaming client disconnects.
- Errors in OpenAI shape on OpenAI routes and Anthropic shape on Anthropic routes, with opencode's messages (such as "Free usage limit reached") passed through as 503.
- Documentation: package READMEs and a full API reference in [`docs/API.md`](https://github.com/mithun50/relayai-gateway/blob/main/docs/API.md).
- A shared conformance suite run against both implementations, and CI on Linux, macOS and Windows.

### Known limitations

- Answers stream in one piece when ready, not token by token, because opencode returns finished answers.
- Text only; image and document inputs are replaced by a placeholder.
- Token usage is an estimate on opencode 2.x.
- Several of opencode's free models answer tool requests with opencode's own tool mechanism instead of text, which fails with "The model returned no text". `opencode/big-pickle`, `opencode/longcat-2.5-free` and `opencode/minimax-m3.1-flash-free` handled tools correctly in testing.
