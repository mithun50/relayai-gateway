# RelayAI

OpenAI-compatible (`/v1/chat/completions`, `/v1/models`) and Anthropic-compatible (`/v1/messages`) APIs, with emulated tool calling, in front of `opencode serve`. Shipped as two packages named `relayai-gateway` (PyPI and npm) that both provide a `relayai` CLI, a server and an SDK. Author: Mithun Gowda B (GitHub mithun50).

## Layout
- `python/src/relayai/`: Python package, standard library only (Python 3.8+). Tests in `python/tests/`.
- `node/src/`: Node package, zero dependencies (Node 18+, CommonJS, ESM named imports work). Types in `node/index.d.ts`. Tests in `node/test/`.
- `conformance/run.py`: shared end-to-end suite run against both implementations; `conformance/fake_opencode.py` (+ `bin/opencode[.cmd]`) is a fake opencode 2.x executable with a scripted model.
- `legacy/`: the old single-file scripts, superseded by the packages (kept until the user decides to delete them).
- Same module split in both: `config`, `state`, `opencode` (OpencodeClient + OpencodeManager), `protocol`, `gateway`, `client`, `sdk`, `cli`.

## Rules
- Every behaviour change goes into BOTH implementations and stays covered by `conformance/run.py`. CLI flags, output lines that tests match, error messages and `~/.relayai` file formats must stay identical across the two.
- Keep both packages dependency free.
- Models are never hardcoded: always read live from opencode (`GET /api/model` on 2.x, `GET /config/providers` on 1.x).
- opencode 1.x and 2.x both supported; version detected via `GET /api/info` (2.x only). opencode 2.x answers unknown paths with its web UI HTML and status 200; the client turns that into an `upstream_wrong_path` error.
- opencode 2.x always requires basic auth. The manager generates a password per `opencode serve` it starts and stores it in `~/.relayai/opencode.json`.
- 2.x request path: create a session (`POST /api/session`, model only), `POST /api/session/:id/generate` with one prompt string (system text on top), `DELETE` the session in `finally`. Cancel via `/interrupt`. Token usage is estimated (chars / 4).
- With opencode's free models: do NOT use `POST /api/experimental/generate` and do NOT send `permissions` on session create; both get "Free usage limit reached" while plain sessions work. Free models are burst rate limited: test real models sequentially, never many requests in parallel.
- Process handling: managed processes are spawned detached. Stop the gateway with `kill_tree(pid, tree=False)`; on Windows a detached child still counts as a child, so `/T` would take opencode with it. Never use `os.kill(pid, 0)` on Windows in Python (it terminates the process); `state.pid_alive` uses the Win32 API.
- Errors use the OpenAI error shape on OpenAI routes and the Anthropic shape on `/v1/messages`.
- Versions are date based, `YYYY.M.D` (no zero padding, one release per day), identical in `python/src/relayai/_version.py` and `node/package.json`. Set them only with `python scripts/release_version.py`. Every user-visible change gets a line under `## [Unreleased]` in CHANGELOG.md.
- Community files: CONTRIBUTING.md, CODE_OF_CONDUCT.md (Contributor Covenant 2.1, contact is the author's email), CHANGELOG.md (Keep a Changelog).
- The user prefers no em dashes or en dashes anywhere, including code comments and docs.

## Testing
- `cd python && python -m unittest discover -s tests`
- `cd node && npm test`
- `python conformance/run.py --impl python` and `--impl node` (45 checks each); add `--with-claude` to drive real Claude Code.
- Run `python -m relayai` from outside the repo root (or install the package); a stray `relayai.py` in the current folder would shadow it.
- CI (`.github/workflows/test.yml`) runs syntax checks only: Python compiled on 3.8, `node --check` on Node 18, `release_version.py --check`. The user decided the unit tests and conformance suite are local-only; run them before every release. Do not add test jobs back to CI.

## Status
- 2026-11-11: both packages pass their unit tests and 45/45 conformance checks on Windows against the fake opencode; Python and Node interoperate through `~/.relayai` (start in one, use and stop from the other).
- 2026-11-11, real opencode 2.0.31 free models (measured with the earlier single-file gateway, same request path): chat works on 11 of 12; full tool round trip on both endpoints works on big-pickle, longcat-2.5-free and minimax-m3.1-flash-free.
- Open problem: `/api/session/:id/generate` still hands opencode's native tools to the model, so many models make a native tool call instead of writing `<tool_call>` text and the reply comes back empty. A first probe with `agent: "plan"` / `"title"` on session create made glm return text, not yet a `<tool_call>` block.
- 2026.11.11 published on 2026-11-11 to PyPI and npm (with provenance) by the release workflow; GitHub release v2026.11.11. Installs from both registries verified.
