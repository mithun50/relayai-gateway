# RelayAI

**OpenAI and Anthropic compatible API, with tool calling, for the models in [opencode](https://opencode.ai).**

RelayAI finds opencode, runs `opencode serve` in the background, and puts `/v1/chat/completions` and `/v1/messages` in front of it, so any OpenAI or Anthropic client (including Claude Code) can use opencode's models. It ships as a CLI, a server and an SDK, for both Python and Node, with no dependencies.

| | Install | Docs |
|---|---|---|
| Python | `pip install relayai-gateway` | [python/README.md](python/README.md) |
| Node | `npm install -g relayai-gateway` | [node/README.md](node/README.md) |
| API reference | both | [docs/API.md](docs/API.md) |
| Changes | both | [CHANGELOG.md](CHANGELOG.md) |

```bash
relayai start                         # opencode serve + gateway in the background
relayai chat "Explain DNS in one line"
relayai claude                        # Claude Code on opencode's models
relayai stop
```

Both packages offer the same `relayai` command and behave the same. They share `~/.relayai`, so either can see and stop what the other started.

## Repository layout

```
python/          PyPI package "relayai-gateway"   (src/relayai, tests/)
node/            npm package  "relayai-gateway"   (src/, bin/, index.d.ts, test/)
docs/API.md      full API reference (both endpoints, streaming, errors, integrations)
scripts/         release_version.py (date-based version for both packages)
conformance/     shared test suite and a fake opencode, run against both implementations
.github/         CI: every suite on Linux, macOS and Windows
```

Each implementation has the same modules:

| Module | Role |
|---|---|
| `config` | settings from flags, `RELAYAI_*` / `OPENCODE_*` variables and defaults |
| `state` | `~/.relayai` files, process helpers (spawn detached, pid checks, stop) |
| `opencode` | `OpencodeClient` (opencode's API, 1.x and 2.x) and `OpencodeManager` (find, install, run `opencode serve`) |
| `protocol` | OpenAI / Anthropic translation and emulated tool calling |
| `gateway` | the HTTP server |
| `client`, `sdk` | `Client` for a running gateway, `RelayAI` that starts everything |
| `cli` | the `relayai` command |

## Development

```bash
# Python
cd python && python -m unittest discover -s tests

# Node
cd node && npm test

# Both, end to end (CLI, API, background lifecycle, doctor), against a fake opencode
python conformance/run.py --impl python
python conformance/run.py --impl node
python conformance/run.py --impl node --with-claude    # also drives real Claude Code
```

Any change to behaviour goes into both implementations and is covered by `conformance/run.py`.

Contributions are welcome: read [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md).

## Versions

Versions are date based, `YYYY.M.D` (the release day, for example `2026.11.11`), and the same for both packages. What changed in each release is in [CHANGELOG.md](CHANGELOG.md). Releasing: [RELEASING.md](RELEASING.md).

## License

MIT, by [Mithun Gowda B](https://github.com/mithun50).
