# relayai-gateway

**OpenAI and Anthropic compatible API, with tool calling, for the models in [opencode](https://opencode.ai).**

Install it, and every tool that speaks the OpenAI or Anthropic API (the `openai` SDK, LangChain, Open WebUI, Claude Code, your own scripts) can use the models you have in opencode, including opencode's free models. relayai finds opencode, runs `opencode serve` in the background for you, and translates between the APIs.

- `POST /v1/chat/completions` (OpenAI) and `POST /v1/messages` (Anthropic), streaming included
- Tool calling on both, so agent harnesses like Claude Code work
- Models are read live from opencode, nothing is hardcoded
- A CLI (`relayai`), a server, and a Node SDK with TypeScript types, in one package with **no dependencies**
- The same tool exists for Python: `pip install relayai-gateway`

```bash
npm install -g relayai-gateway
relayai chat "Explain DNS in one sentence"
```

---

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Use it from any OpenAI or Anthropic client](#use-it-from-any-openai-or-anthropic-client)
- [Full API reference](#full-api-reference)
- [Claude Code](#claude-code)
- [Node SDK](#node-sdk)
- [CLI reference](#cli-reference)
- [Configuration](#configuration)
- [How it works](#how-it-works)
- [Which models work with tools](#which-models-work-with-tools)
- [Limits](#limits)
- [Security](#security)
- [Troubleshooting](#troubleshooting)

## Requirements

- Node.js 18 or newer
- **opencode** (1.x or 2.x). If you do not have it:

  ```bash
  relayai install-opencode        # runs: npm install -g @opencode/cli (asks first)
  ```

  or install it yourself as described at https://opencode.ai/docs.
- At least one model in opencode. opencode's free models work out of the box; to add your own providers (OpenRouter, Anthropic, OpenAI, Groq, ...) run `opencode auth login`.

Check everything with:

```bash
relayai doctor --test
```

## Quick start

```bash
relayai start                 # opencode serve + gateway in the background
relayai models                # what you can use
relayai chat "Hello"          # one question
relayai chat                  # interactive chat (/reset clears it)
relayai status                # URLs and API key
relayai stop                  # stops the gateway and the opencode serve it started
```

`relayai serve` does the same as `start` but in the foreground, with request logs; Ctrl+C stops everything.

The gateway listens on `http://127.0.0.1:8000`. Its API key is created once and stored in `~/.relayai/api_key`; `relayai status` shows it.

Commands like `chat` and `models` use the background gateway when it runs. Without it they start a private one for the duration of the command, which takes a few seconds longer.

## Use it from any OpenAI or Anthropic client

`relayai env` prints the variables for your shell:

```bash
eval "$(relayai env)"                     # bash / zsh
relayai env | Invoke-Expression           # PowerShell
```

| Client type | Base URL | API key | Model |
|---|---|---|---|
| OpenAI (SDKs, LangChain, Open WebUI, ...) | `http://127.0.0.1:8000/v1` | from `relayai status` | `default` or any id from `relayai models` |
| Anthropic (SDKs, Claude Code) | `http://127.0.0.1:8000` | same | same |

curl:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "default", "messages": [{"role": "user", "content": "Say hi"}]}'
```

The official `openai` package:

```js
import OpenAI from "openai";

const client = new OpenAI({ baseURL: "http://127.0.0.1:8000/v1", apiKey: "rai-..." });
const reply = await client.chat.completions.create({
  model: "opencode/big-pickle",
  messages: [{ role: "user", content: "Hello" }],
});
console.log(reply.choices[0].message.content);
```

Tools work the normal OpenAI way (`tools=[...]`, then `tool_calls` in the reply, then a `role: "tool"` message with the result).

## Full API reference

Every endpoint, request field, streaming event, error code and client integration (OpenAI and Anthropic SDKs for Python and Node, LangChain, Vercel AI SDK, Open WebUI, Claude Code), plus HTTPS, network access and running on boot, is documented in the **[API reference](https://github.com/mithun50/relayai-gateway/blob/main/docs/API.md)**.

## Claude Code

```bash
relayai claude                                   # interactive, through the gateway
relayai claude -m opencode/big-pickle            # pick the model
relayai claude -- -p "list the files here"       # anything after -- goes to claude
```

This sets `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and the model variables for that one Claude Code process only; your normal `claude` is untouched. Pick a model from the [tools table](#which-models-work-with-tools), because Claude Code depends on tool calling.

## Node SDK

Install it in your project with `npm install relayai-gateway` (CommonJS and ESM both work; types included).

```js
const { RelayAI } = require("relayai-gateway");   // or: import { RelayAI } from "relayai-gateway";

const ai = await RelayAI.start();                  // finds/starts opencode and a private gateway
try {
  console.log(await ai.chat("Hello"));             // -> string
  for await (const piece of ai.stream("Tell me a joke")) process.stdout.write(piece);
  console.log(await ai.models());                  // -> ["opencode/big-pickle", ...]
} finally {
  await ai.close();
}
```

With TypeScript 5.2+ you can write `await using ai = await RelayAI.start();` and skip the `close()`.

`RelayAI.start()` reuses the background gateway from `relayai start` when it runs (pass `reuse: false` to always run a private one). `close()` stops whatever it started.

Options: `RelayAI.start({ model: "opencode/big-pickle", timeout: 120, opencodeUrl: "http://127.0.0.1:4096", opencodePassword: "..." })`. Every field of [the configuration](#configuration) works, in camelCase.

**Conversations, system prompt, extra parameters**

```js
await ai.chat(
  [
    { role: "user", content: "My name is Asha." },
    { role: "assistant", content: "Nice to meet you, Asha." },
    { role: "user", content: "What is my name?" },
  ],
  { system: "Answer in one word.", model: "opencode/big-pickle" }
);
```

**Tool calling**

```js
const weather = {
  type: "function",
  function: {
    name: "get_weather",
    description: "Current weather for a city",
    parameters: { type: "object", properties: { city: { type: "string" } }, required: ["city"] },
  },
};

const messages = [{ role: "user", content: "Weather in Paris?" }];
const reply = await ai.complete({ messages, tools: [weather] });   // raw OpenAI response
const call = reply.choices[0].message.tool_calls[0];
messages.push(reply.choices[0].message, { role: "tool", tool_call_id: call.id, content: "18C, sunny" });
console.log(await ai.chat(messages, { tools: [weather] }));
```

**Anthropic format**: `await ai.messages({ messages: [...], tools: [...] })` resolves with the Anthropic response.

**Plug the official SDKs in**: `ai.baseUrl` (OpenAI, ends in `/v1`), `ai.anthropicBaseUrl` and `ai.apiKey`.

**Errors**: every failure rejects with `ApiError`, which has `.status` (HTTP status), `.message`, `.kind` and `.code`.

**Lower level pieces**

| Export | Use it to |
|---|---|
| `new Client(baseUrl, apiKey)` | talk to a gateway that is already running somewhere |
| `await Gateway.create(settings)` | embed the server in your own program (`.baseUrl`, `.close()`) |
| `OpencodeManager` | find, install, start and stop `opencode serve` yourself |
| `new OpencodeClient(url, { password })` | call opencode's own API (1.x and 2.x) |

```js
const { Gateway } = require("relayai-gateway");

const gw = await Gateway.create({ apiKey: "secret", port: 9000 });
console.log(gw.baseUrl);   // http://127.0.0.1:9000/v1
// ... later
await gw.close();
```

## CLI reference

| Command | What it does |
|---|---|
| `relayai serve` | run the gateway in this terminal (`--keep-opencode` leaves opencode running on exit) |
| `relayai start` | run it in the background (`--wait` seconds to wait for it) |
| `relayai stop` | stop the background gateway and the opencode serve relayai started (`--keep-opencode`) |
| `relayai status [--json]` | what is running, URLs and API key |
| `relayai models [--json]` | models opencode can use |
| `relayai chat [PROMPT]` | answer one prompt (also from stdin), or chat interactively; `-s` system prompt, `--no-stream` |
| `relayai env [--shell bash\|powershell\|cmd\|fish]` | print `OPENAI_*` and `ANTHROPIC_*` variables |
| `relayai claude [-- ARGS]` | run Claude Code through the gateway |
| `relayai doctor [--test]` | check the setup; `--test` sends one real request |
| `relayai install-opencode [-y] [--force]` | install opencode with npm |

Every command accepts the options in the next section. `relayai <command> --help` shows them.

## Configuration

Command line flags win over environment variables, which win over defaults.

| Flag | Variable | Default | Meaning |
|---|---|---|---|
| `--host` | `RELAYAI_HOST` | `127.0.0.1` | interface to listen on; `0.0.0.0` exposes it on the network |
| `--port` | `RELAYAI_PORT` | `8000` | gateway port |
| `--api-key` | `RELAYAI_API_KEY` | stored in `~/.relayai/api_key` | key clients must send |
| `-m`, `--model` | `RELAYAI_MODEL` | opencode's default | model behind `default`, as `provider/model` |
| `--no-strict-models` | `RELAYAI_STRICT_MODELS=0` | strict | unknown model names fall back to the default instead of a 404 (the Anthropic endpoint always falls back, because Claude Code sends Claude model names) |
| `--timeout` | `RELAYAI_TIMEOUT` | `600` | seconds per answer |
| `--max-concurrency` | `RELAYAI_MAX_CONCURRENCY` | `4` | answers generated at once |
| | `RELAYAI_QUEUE_WAIT` | `30` | seconds to wait for a free slot before a 429 |
| | `RELAYAI_MODEL_CACHE` | `30` | seconds the model list is cached |
| `--opencode-url` | `OPENCODE_URL` | managed by relayai | use an `opencode serve` you run yourself |
| `--opencode-password` | `OPENCODE_SERVER_PASSWORD` | | its password (opencode 2.x always has one) |
| | `OPENCODE_SERVER_USERNAME` | `opencode` | its username |
| `--opencode-port` | `RELAYAI_OPENCODE_PORT` | `4096` | preferred port for the managed opencode serve (the next free one is used if taken) |
| `--opencode-bin` | `RELAYAI_OPENCODE_BIN` | found on PATH | path to the opencode executable |
| | `RELAYAI_HOME` | `~/.relayai` | where state, logs and the key live |

In code: `configFromEnv({ port: 9000, model: "opencode/big-pickle" })`, or pass the same object to `RelayAI.start()` / `Gateway.create()`.

## How it works

**opencode management.** Unless you pass `--opencode-url`, relayai looks for the `opencode` executable, starts `opencode serve` on `127.0.0.1` with a freshly generated password, and keeps it in `~/.relayai/workspace` so it never works inside your projects. Its details go in `~/.relayai/opencode.json`, so later commands (and the Python package) reuse it. `relayai stop`, `serve` on exit and the SDK on close stop it again if they started it.

**Requests.** Every API request becomes one short-lived opencode session that is deleted afterwards. The OpenAI and Anthropic APIs are stateless, so the whole conversation is folded into one prompt. opencode's own tools (shell, file editing) never run.

**Tool calling** is emulated: your tools are described in the prompt, the model answers with `<tool_call>{"name": ..., "arguments": ...}</tool_call>` blocks, and relayai turns those into OpenAI `tool_calls` or Anthropic `tool_use`. Your program runs the tools, exactly as with a native API.

**Files in `~/.relayai`**: `api_key`, `gateway.json` and `opencode.json` (what is running), `gateway.log` and `opencode.log`, and `workspace/`.

## Which models work with tools

Plain chat works with every model opencode can reach. Emulated tool calling depends on the model following the `<tool_call>` format. Tested on 2026-11-11 with opencode 2.0.31's free models:

| Model | Chat | Tools (OpenAI and Anthropic) |
|---|---|---|
| `opencode/big-pickle` | yes | yes |
| `opencode/longcat-2.5-free` | yes | yes |
| `opencode/minimax-m3.1-flash-free` | yes | yes |
| `opencode/deepseek-v4.1-flash-free`, `opencode/inkling-2-small-free` | yes | partly (one endpoint) |
| `glm-5.3-flash-free`, `flint-40b-free`, `iceman-free`, `beam-free`, `nemotron-3.5-lightning-free`, `mimo-v2.6-flash-free` | yes | no |

The models marked "no" try to use opencode's own built-in tools instead of writing text, and the reply comes back empty ("The model returned no text"). Strong paid models generally follow the format well. Free model line-ups change often, so test yours with a small tools request before relying on it.

## Limits

- **Streaming arrives in one piece.** opencode returns the finished answer, so `stream: true` sends it in chunks once it is ready (with keepalives meanwhile), not token by token.
- **Text only.** Images and documents are replaced by a placeholder.
- **Token counts are estimates** on opencode 2.x (about 4 characters per token); opencode 1.x reports real counts.
- **Endpoints**: chat completions, messages, models, count_tokens (an estimate). No embeddings, no Responses API, no images.
- **Free models are rate limited** by opencode; bursts of parallel requests get "Free usage limit reached" for a while.

## Security

- The gateway listens on `127.0.0.1` only. `--host 0.0.0.0` exposes it to your network; anyone with the key can then spend your providers' quota, so use a long key.
- Every API request needs the key (`Authorization: Bearer ...` or `x-api-key`). `/health` is the only open endpoint.
- The key and the opencode password are stored in `~/.relayai` with owner-only permissions on Linux and macOS.

## Troubleshooting

| Message | Meaning and fix |
|---|---|
| `opencode is not installed` | `relayai install-opencode`, or `--opencode-bin PATH` |
| `opencode: Free usage limit reached` (HTTP 503) | opencode's free tier is rate limited; wait a little, send fewer parallel requests, or connect your own provider with `opencode auth login` |
| `The model returned no text` | the model tried opencode's built-in tools; pick a model from the [tools table](#which-models-work-with-tools) |
| `opencode rejected the login` | with `--opencode-url`, pass the matching `--opencode-password` |
| `cannot listen on 127.0.0.1:8000` | another program uses the port; `--port 8100` |
| `The model 'x' is not available` (404) | run `relayai models` and use one of those ids, or `default` |

Logs: `~/.relayai/gateway.log` (background gateway) and `~/.relayai/opencode.log`.

## Changelog and contributing

Versions are date based (`YYYY.M.D`, the release day). See the [changelog](https://github.com/mithun50/relayai-gateway/blob/main/CHANGELOG.md), [contributing guide](https://github.com/mithun50/relayai-gateway/blob/main/CONTRIBUTING.md) and [code of conduct](https://github.com/mithun50/relayai-gateway/blob/main/CODE_OF_CONDUCT.md).

## License

MIT, by [Mithun Gowda B](https://github.com/mithun50). Source: [github.com/mithun50/relayai-gateway](https://github.com/mithun50/relayai-gateway)
