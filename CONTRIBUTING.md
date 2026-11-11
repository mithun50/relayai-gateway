# Contributing to RelayAI

Thanks for helping. This guide covers how the project is built, how to test a change, and how releases work. Everyone taking part agrees to the [Code of Conduct](https://github.com/mithun50/relayai-gateway/blob/main/CODE_OF_CONDUCT.md).

## Ways to help

- **Report a bug.** Open an issue with what you ran, what you expected, what happened, and the output of `relayai doctor`. Logs from `~/.relayai/gateway.log` and `~/.relayai/opencode.log` help a lot. Remove your API key and any private prompts first.
- **Report model results.** Tool calling depends on the model. If you tested a model (see "Testing a real model" below), share which one, your opencode version, and whether plain chat, OpenAI tools and Anthropic tools worked. These results feed the table in [`docs/API.md`](https://github.com/mithun50/relayai-gateway/blob/main/docs/API.md).
- **Fix or improve something.** For anything bigger than a small fix, open an issue first to agree on the approach.
- **Improve the docs.** Corrections and clearer wording are always welcome.

## How the project is laid out

```
python/src/relayai/   Python package (PyPI: relayai-gateway), standard library only
node/src/             Node package (npm: relayai-gateway), no dependencies
node/index.d.ts       TypeScript types for the Node package
conformance/          shared end-to-end tests and a fake opencode
docs/API.md           API reference
scripts/              release helper
```

Both packages have the same modules, with the same responsibilities:

| Module | Role |
|---|---|
| `config` | settings from flags, environment variables and defaults |
| `state` | files in `~/.relayai` and process helpers |
| `opencode` | `OpencodeClient` (opencode's HTTP API) and `OpencodeManager` (runs `opencode serve`) |
| `protocol` | OpenAI and Anthropic translation, emulated tool calling |
| `gateway` | the HTTP server |
| `client`, `sdk` | the SDK classes |
| `cli` | the `relayai` command |

## Ground rules

1. **Change both implementations.** The Python and Node packages must behave the same: same endpoints and responses, same CLI commands, flags and messages, same `~/.relayai` file formats. A change that lands in only one of them will not be merged.
2. **No runtime dependencies.** Python uses only the standard library (3.8+), Node only its built-in modules (18+). Test-only tools are fine.
3. **Nothing hardcoded about models.** Model lists and defaults always come from opencode.
4. **Cover it in the conformance suite.** New behaviour gets a check in [`conformance/run.py`](https://github.com/mithun50/relayai-gateway/blob/main/conformance/run.py), which runs against both implementations.
5. **Keep errors in the right shape.** OpenAI routes return OpenAI-style errors, Anthropic routes return Anthropic-style errors.
6. **Writing style.** No em dashes or en dashes in code, comments or docs; use commas, colons, parentheses or separate sentences.

## Setting up

You need Python 3.8+, Node 18+ and git. opencode is only needed to test against real models.

```bash
git clone <your fork>
cd relayai
```

No install step is needed to run the tests: they use the source folders directly. To try the CLI from your checkout:

```bash
cd python && python -m pip install -e . && cd ..     # gives you `relayai` (Python)
cd node && npm link && cd ..                          # or the Node one
```

Install only one of them at a time, since both provide the `relayai` command.

## Running the tests

```bash
# Python unit tests
cd python && python -m unittest discover -s tests && cd ..

# Node unit tests
cd node && npm test && cd ..

# End to end, against a fake opencode (CLI, API, background start/stop, doctor)
python conformance/run.py --impl python
python conformance/run.py --impl node

# Also drive the real Claude Code through `relayai claude` (needs `claude` installed)
python conformance/run.py --impl node --with-claude
```

The suites use a temporary `RELAYAI_HOME`, so they never touch your own `~/.relayai`. They do not need opencode or network access: [`conformance/fake_opencode.py`](https://github.com/mithun50/relayai-gateway/blob/main/conformance/fake_opencode.py) plays opencode with a scripted model. CI ([`.github/workflows/test.yml`](https://github.com/mithun50/relayai-gateway/blob/main/.github/workflows/test.yml)) runs all of this on Linux, macOS and Windows, with Python 3.8 to 3.14 and Node 18 to 22.

## Testing a real model

The fake opencode proves the plumbing; only a real model shows whether a model follows the tool format.

```bash
relayai doctor --test -m opencode/big-pickle
relayai start -m opencode/big-pickle
relayai chat "Reply with exactly: OK"
relayai claude -- -p "List the files in this folder"
relayai stop
```

Send real requests one at a time. opencode's free models are rate limited, and a burst of parallel requests gets "Free usage limit reached" for a while. If that happens, check `opencode run -m opencode/big-pickle "hi"`: if opencode itself is refused too, the limit is on the account, not in RelayAI.

## Making a pull request

1. Branch from `main`.
2. Make the change in both packages, with tests.
3. Run the four test commands above.
4. Add a line to the `## [Unreleased]` section of [`CHANGELOG.md`](https://github.com/mithun50/relayai-gateway/blob/main/CHANGELOG.md) under **Added**, **Changed**, **Fixed** or **Removed**. Describe it from the user's side ("`relayai chat` reads the prompt from stdin"), not the code's.
5. Update [`docs/API.md`](https://github.com/mithun50/relayai-gateway/blob/main/docs/API.md) and the READMEs if behaviour or options changed.
6. Open the pull request with a short description of the problem, the change, and how you tested it (including any real models).

## Versions and releases

Versions are **date based**: `YYYY.M.D`, the day of the release, with no zero padding (for example `2026.11.11`, `2027.1.5`). One string works for npm (semver) and PyPI (PEP 440) and sorts correctly in both. There is **at most one release per day**; if a fix is ready on a day that already had a release, it ships the next day.

Because the number does not say how big a release is, the changelog does: anything that can break existing users goes under **Changed** or **Removed**, with what to do about it.

Releasing (maintainers): `python scripts/release_version.py`, commit, then push a tag `v<version>`. CI tests, builds and publishes both packages and creates the GitHub release. Details in [RELEASING.md](https://github.com/mithun50/relayai-gateway/blob/main/RELEASING.md).

## License

By contributing you agree that your contribution is licensed under the [MIT License](https://github.com/mithun50/relayai-gateway/blob/main/LICENSE) of this project.
