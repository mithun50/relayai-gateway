# RelayAI API reference

RelayAI serves an **OpenAI-compatible API** and an **Anthropic-compatible API** at the same time, on the same port, with the same key. Behind both is opencode: every model opencode can reach is available through either API, with streaming and tool calling.

This page documents every endpoint, field, event and error. For installing and the `relayai` command, see the package READMEs ([Python](../python/README.md), [Node](../node/README.md)).

## Contents

1. [Quick facts](#1-quick-facts)
2. [Start the server and get your URL and key](#2-start-the-server-and-get-your-url-and-key)
3. [Base URLs and authentication](#3-base-urls-and-authentication)
4. [Endpoint index](#4-endpoint-index)
5. [GET /health](#5-get-health)
6. [Models](#6-models)
7. [POST /v1/chat/completions (OpenAI)](#7-post-v1chatcompletions-openai)
8. [POST /v1/messages (Anthropic)](#8-post-v1messages-anthropic)
9. [Tool calling](#9-tool-calling)
10. [Errors](#10-errors)
11. [Behaviour and limits](#11-behaviour-and-limits)
12. [Client integrations](#12-client-integrations)
13. [Network access, HTTPS and running on boot](#13-network-access-https-and-running-on-boot)
14. [What is not supported](#14-what-is-not-supported)

---

## 1. Quick facts

| | OpenAI compatible | Anthropic compatible |
|---|---|---|
| Base URL | `http://127.0.0.1:8000/v1` | `http://127.0.0.1:8000` |
| Main endpoint | `POST /v1/chat/completions` | `POST /v1/messages` |
| Auth header | `Authorization: Bearer <key>` | `x-api-key: <key>` (Bearer works too) |
| Models | `GET /v1/models` | same list |
| Streaming | `"stream": true` (SSE `data:` chunks, ends with `[DONE]`) | `"stream": true` (SSE named events) |
| Tools | `tools`, `tool_choice`, `tool_calls`, `role: "tool"` | `tools`, `tool_choice`, `tool_use`, `tool_result` |
| Error shape | `{"error": {"message", "type", "param", "code"}}` | `{"type": "error", "error": {"type", "message"}}` |

Port `8000` and host `127.0.0.1` are the defaults; both can be changed (section 2).

## 2. Start the server and get your URL and key

Any of these gives you both APIs.

**CLI, in the background** (recommended):

```bash
relayai start                         # add -m opencode/big-pickle to choose the default model
relayai status                        # shows the URLs and the key again
relayai stop
```

**CLI, in the foreground** (shows one log line per request; Ctrl+C stops it):

```bash
relayai serve --port 8000
```

Output of either:

```
OpenAI base URL    : http://127.0.0.1:8000/v1
Anthropic base URL : http://127.0.0.1:8000
API key            : rai-...
```

**From code** (no separate process):

```python
from relayai import RelayAI
with RelayAI() as ai:
    print(ai.base_url, ai.anthropic_base_url, ai.api_key)
```

```js
const { RelayAI } = require("relayai-gateway");
const ai = await RelayAI.start();
console.log(ai.baseUrl, ai.anthropicBaseUrl, ai.apiKey);
```

**The key.** If you do not choose one (`--api-key` or `RELAYAI_API_KEY`), relayai creates a random key on first use, stores it in `~/.relayai/api_key`, and reuses it every time, so your clients keep working across restarts. To change it, delete that file (or set your own key) and restart. The SDK's private gateways use a fresh random key each time unless you pass `api_key` / `apiKey`.

**Print the variables for your shell**, so tools that read `OPENAI_*` / `ANTHROPIC_*` pick them up:

```bash
eval "$(relayai env)"                         # bash, zsh
relayai env --shell fish | source             # fish
relayai env | Invoke-Expression               # PowerShell
relayai env --shell cmd                       # cmd.exe: prints set commands
```

It prints `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_MODEL`.

## 3. Base URLs and authentication

**Base URLs.** OpenAI clients append paths like `/chat/completions` to their base URL, so their base URL ends in `/v1`. Anthropic clients append `/v1/messages` themselves, so their base URL is the bare server address. RelayAI also accepts paths without the `/v1` prefix (`/chat/completions`, `/models`, `/messages`), so a base URL configured either way works.

**Authentication.** Every endpoint except `/health` needs the key, in any of these headers:

| Header | Used by |
|---|---|
| `Authorization: Bearer <key>` | OpenAI clients, Claude Code with `ANTHROPIC_AUTH_TOKEN` |
| `x-api-key: <key>` | Anthropic clients |
| `api-key: <key>` | Azure-style OpenAI clients |

A missing or wrong key returns **401** (`invalid_api_key` on OpenAI routes, `authentication_error` on Anthropic routes). The comparison is constant time.

**CORS.** Every response carries `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Headers: *` and `Access-Control-Allow-Methods: GET, POST, OPTIONS`, and `OPTIONS` preflights return 204. Browser apps can call the API directly, which also means any web page you open could try to, so keep the key out of public pages.

## 4. Endpoint index

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /health` (also `GET /`) | no | gateway and opencode status |
| `GET /v1/models` | yes | models opencode can use right now |
| `GET /v1/models/{id}` | yes | one model (accepts full or short ids) |
| `POST /v1/chat/completions` | yes | OpenAI chat completions, streaming and tools |
| `POST /v1/messages` | yes | Anthropic messages, streaming and tools |
| `POST /v1/messages/count_tokens` | yes | rough token count of a request |
| `OPTIONS *` | no | CORS preflight (204) |
| `HEAD *` | no | connection warm-up probes (200, empty) |

Anything else returns 404 `Unknown endpoint`.

## 5. GET /health

No key needed. Use it for monitoring and readiness checks.

```bash
curl http://127.0.0.1:8000/health
```

```json
{"status":"ok","service":"RelayAI","version":"2026.11.11","opencode":{"url":"http://127.0.0.1:4096","reachable":true,"version":"2.0.31"}}
```

| Field | Meaning |
|---|---|
| `status` | `ok` when opencode answers, `degraded` when it does not |
| `version` | RelayAI version (date based, `YYYY.M.D`) |
| `opencode.url` / `.version` | the opencode serve behind this gateway |

HTTP status is **200** when `ok` and **503** when `degraded` (the body is the same shape), so load balancers can use the status code alone.

## 6. Models

### GET /v1/models

Lists every model opencode can use at this moment, read live from opencode (cached for 30 seconds, `RELAYAI_MODEL_CACHE`). Nothing is hardcoded: connect a provider in opencode and its models appear here.

```bash
curl http://127.0.0.1:8000/v1/models -H "Authorization: Bearer $KEY"
```

```json
{"object":"list","data":[{"id":"opencode/big-pickle","object":"model","created":0,"owned_by":"opencode"}, ...]}
```

Ids have the form `provider/model`. `owned_by` is the provider part. `created` is always 0 (opencode does not report it).

### GET /v1/models/{id}

Looks up one model. Accepts the full id (`/v1/models/opencode/big-pickle`) or a short one (`/v1/models/big-pickle`) and returns the full entry. Unknown ids return 404 `model_not_found`.

### How the `model` field is resolved

The same rules apply to `/v1/chat/completions` and `/v1/messages`, except where noted.

| You send | Result |
|---|---|
| `"default"`, `"auto"`, `""` or no model | the gateway's default model (`--model` / `RELAYAI_MODEL`); if none is set, opencode's own default |
| a full id, e.g. `"opencode/big-pickle"` | that model |
| a short id, e.g. `"big-pickle"` | the model with that name, if exactly one provider has it |
| a short id that several providers have | 400 `model_ambiguous`, listing the full ids |
| an unknown name, OpenAI endpoint | 404 `model_not_found` (with `--no-strict-models`: the default model instead) |
| an unknown name, Anthropic endpoint | always the default model. Claude Code and Anthropic SDKs send names like `claude-sonnet-4-5`, so the Anthropic endpoint never rejects a name |

When a name is not found, the model list is refreshed once before giving up, so a model added in opencode a moment ago is found.

**What the response reports.** The OpenAI endpoint returns the model that actually answered in `model` (or `"default"` when opencode chose it). The Anthropic endpoint echoes the name you sent, as Anthropic clients expect.

## 7. POST /v1/chat/completions (OpenAI)

### Request

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{
        "model": "opencode/big-pickle",
        "messages": [
          {"role": "system", "content": "You are terse."},
          {"role": "user", "content": "What is DNS?"}
        ]
      }'
```

| Field | Supported | Notes |
|---|---|---|
| `model` | yes | see [model resolution](#how-the-model-field-is-resolved) |
| `messages` | yes, required | non-empty array; at least one user message with text |
| `stream` | yes | see [streaming](#streaming-1) |
| `stream_options.include_usage` | yes | adds a final usage chunk to the stream |
| `tools` | yes | `type: "function"` tools with JSON Schema `parameters` |
| `tool_choice` | yes | `"auto"` (default), `"none"`, `"required"`, `{"type": "function", "function": {"name": ...}}` |
| `functions` | yes | legacy field, treated like `tools`; calls still come back as `tool_calls` |
| `temperature`, `top_p`, `max_tokens`, `max_completion_tokens`, `stop`, `presence_penalty`, `frequency_penalty`, `seed`, `n`, `logprobs`, `response_format`, `parallel_tool_calls`, `user`, `metadata` | accepted, **ignored** | opencode does not take these per request. Requests that send them still work. `n` is always 1. |

**Message roles**

| Role | Handling |
|---|---|
| `system`, `developer` | joined (in order) into the system prompt |
| `user` | a user turn; empty ones are skipped |
| `assistant` | an assistant turn; its `tool_calls` are kept so the model sees what it called |
| `tool` (and legacy `function`) | a tool result, matched to its call by `tool_call_id` |

**Content** may be a string or an array of parts. Parts of type `text`, `input_text` and `output_text` are used. Parts of type `image_url`, `image`, `input_image` and `document` are replaced by the text `[attachment omitted: this endpoint is text only]`.

### Response

```json
{
  "id": "chatcmpl-7acfc4398fad4b1ba0661a80a7fab808",
  "object": "chat.completion",
  "created": 1794416291,
  "model": "opencode/big-pickle",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "DNS maps names to IP addresses."},
      "logprobs": null,
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": 33, "completion_tokens": 8, "total_tokens": 41}
}
```

- `finish_reason` is `"stop"` or `"tool_calls"`. It is never `"length"`, because no token limit is applied.
- With tool calls, `message.content` is `null` (or any text the model wrote around the calls) and `message.tool_calls` is set; see [section 9](#9-tool-calling).
- `usage`: with opencode 2.x these are **estimates** (about 4 characters per token, including RelayAI's own instructions); opencode 1.x reports real counts.

### Streaming

With `"stream": true` the response is `text/event-stream`. Real captured output for a request with a tool call and `stream_options.include_usage`:

```
data: {"id":"chatcmpl-2f00...","object":"chat.completion.chunk","created":1794416297,"model":"default","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null,"logprobs":null}]}

data: {"id":"chatcmpl-2f00...","object":"chat.completion.chunk","created":1794416297,"model":"default","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_82d8...","type":"function","function":{"name":"get_weather","arguments":"{\"city\":\"Paris\"}"}}]},"finish_reason":null,"logprobs":null}]}

data: {"id":"chatcmpl-2f00...","object":"chat.completion.chunk","created":1794416297,"model":"default","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls","logprobs":null}]}

data: {"id":"chatcmpl-2f00...","object":"chat.completion.chunk","created":1794416297,"model":"default","choices":[],"usage":{"prompt_tokens":281,"completion_tokens":20,"total_tokens":301}}

data: [DONE]
```

Order of chunks:

1. a role chunk (`delta: {"role": "assistant", "content": ""}`), sent immediately;
2. `: keepalive` comment lines every 10 seconds (`RELAYAI_KEEPALIVE`) while the model works; SSE clients ignore them;
3. the answer text in `content` deltas of up to 48 characters;
4. one chunk per tool call, each with its own `index` and the complete `arguments` string;
5. a chunk with `finish_reason`;
6. with `include_usage`: a chunk with `choices: []` and `usage`;
7. `data: [DONE]`.

The answer arrives **in one burst when it is ready**, not token by token, because opencode returns finished answers. The early role chunk and the keepalives keep connections and proxies from timing out meanwhile.

If the request fails after the stream started, the error is sent as `data: {"error": {...}}` followed by `data: [DONE]` (the HTTP status is already 200 by then).

If the client disconnects mid-stream, RelayAI cancels the opencode run.

## 8. POST /v1/messages (Anthropic)

### Request

```bash
curl http://127.0.0.1:8000/v1/messages \
  -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d '{
        "model": "default",
        "max_tokens": 1024,
        "system": "You are terse.",
        "messages": [{"role": "user", "content": "What is DNS?"}]
      }'
```

The `anthropic-version` header that Anthropic SDKs send is accepted and not needed.

| Field | Supported | Notes |
|---|---|---|
| `model` | yes | unknown names fall back to the default model; see [model resolution](#how-the-model-field-is-resolved) |
| `messages` | yes, required | `user` and `assistant` turns; string or content blocks |
| `system` | yes | a string or an array of text blocks |
| `stream` | yes | see [streaming](#streaming-2) |
| `tools` | yes | `name`, `description`, `input_schema` |
| `tool_choice` | yes | `{"type": "auto"}` (default), `{"type": "any"}`, `{"type": "tool", "name": ...}`, `{"type": "none"}` |
| `max_tokens` | accepted, **not enforced** | send it anyway, Anthropic SDKs require it |
| `temperature`, `top_p`, `top_k`, `stop_sequences`, `metadata`, `thinking`, `disable_parallel_tool_use`, `service_tier` | accepted, **ignored** | |

**Content blocks**

| Block | Handling |
|---|---|
| `text` | used |
| `tool_use` (in assistant turns) | kept, so the model sees what it called |
| `tool_result` (in user turns) | given to the model as the result of the matching `tool_use_id`; `is_error: true` is marked as an error; `content` may be a string or text blocks |
| `image`, `document` | replaced by `[attachment omitted: this endpoint is text only]` |
| `thinking`, `redacted_thinking`, others | dropped |

Messages with `role: "system"` inside `messages` (Claude Code sends these) are kept as system notes at that point in the conversation.

### Response

```json
{
  "id": "msg_c4620cd5e3da4510a54663a8",
  "type": "message",
  "role": "assistant",
  "model": "claude-sonnet-4-5",
  "content": [{"type": "text", "text": "DNS maps names to IP addresses."}],
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {"input_tokens": 33, "output_tokens": 8}
}
```

- `model` echoes what you sent.
- `stop_reason` is `"end_turn"` or `"tool_use"`; never `"max_tokens"`.
- `content` holds a `text` block (if the model wrote text) followed by one `tool_use` block per tool call.
- `usage` is estimated on opencode 2.x, as for OpenAI.

### Streaming

With `"stream": true` the response uses Anthropic's named SSE events. Real captured output for a tool call:

```
event: message_start
data: {"type":"message_start","message":{"id":"msg_261c...","type":"message","role":"assistant","model":"default","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":0,"output_tokens":0}}}

event: ping
data: {"type":"ping"}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_8bf1...","name":"get_weather","input":{}}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\"city\":\"Paris\"}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"input_tokens":281,"output_tokens":20}}

event: message_stop
data: {"type":"message_stop"}
```

- `message_start` and a `ping` are sent immediately; more `ping` events follow every 10 seconds while the model works.
- Text blocks stream as `text_delta` pieces of up to 64 characters. Tool input arrives as one `input_json_delta` holding the complete JSON.
- Usage is final in `message_delta` (it is 0 in `message_start`).
- A failure after the stream started is sent as `event: error` with the Anthropic error body.

### POST /v1/messages/count_tokens

```bash
curl http://127.0.0.1:8000/v1/messages/count_tokens -H "x-api-key: $KEY" \
  -H "Content-Type: application/json" -d '{"model":"default","messages":[{"role":"user","content":"Hello there"}]}'
```

```json
{"input_tokens": 16}
```

A rough estimate (request size / 4), there so clients that call it (Claude Code does) keep working. Do not use it for billing.

## 9. Tool calling

Both endpoints support tools with the standard request and response shapes, so frameworks and agent harnesses work unchanged. Your code runs the tools; opencode's own tools (shell, file editing) are never used.

### OpenAI round trip

```python
from openai import OpenAI
import json

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key=KEY)
weather = {"type": "function", "function": {
    "name": "get_weather",
    "description": "Current weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
}}

messages = [{"role": "user", "content": "Weather in Paris?"}]
r = client.chat.completions.create(model="opencode/big-pickle", messages=messages, tools=[weather])
msg = r.choices[0].message
if msg.tool_calls:                                     # finish_reason == "tool_calls"
    messages.append(msg.model_dump(exclude_none=True))
    for call in msg.tool_calls:
        args = json.loads(call.function.arguments)     # {"city": "Paris"}
        messages.append({"role": "tool", "tool_call_id": call.id, "content": "18C, sunny"})
    r = client.chat.completions.create(model="opencode/big-pickle", messages=messages, tools=[weather])
print(r.choices[0].message.content)
```

### Anthropic round trip

```python
import anthropic

client = anthropic.Anthropic(base_url="http://127.0.0.1:8000", api_key=KEY)
tools = [{"name": "get_weather", "description": "Current weather for a city",
          "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]

messages = [{"role": "user", "content": "Weather in Paris?"}]
r = client.messages.create(model="opencode/big-pickle", max_tokens=1024, tools=tools, messages=messages)
if r.stop_reason == "tool_use":
    use = next(b for b in r.content if b.type == "tool_use")
    messages += [{"role": "assistant", "content": r.content},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": use.id, "content": "18C, sunny"}]}]
    r = client.messages.create(model="opencode/big-pickle", max_tokens=1024, tools=tools, messages=messages)
print(r.content[0].text)
```

### How it works

opencode does not pass a caller's tools to the model, so RelayAI emulates tool calling:

1. Your tools (names, descriptions, JSON Schemas) are written into the system prompt, with instructions to call them as:

   ```
   <tool_call>
   {"name": "get_weather", "arguments": {"city": "Paris"}}
   </tool_call>
   ```

2. RelayAI finds those blocks in the model's answer, checks the name against your tools, parses the arguments, and returns them as OpenAI `tool_calls` / Anthropic `tool_use` with fresh ids. Text around the blocks stays as normal content.
3. Your tool results go back to the model as `<tool_result>` blocks in the conversation.

Details:

- Several blocks in one answer become several tool calls (parallel calls).
- A block naming a tool you did not offer, or with arguments that are not a JSON object, is left in the text instead of becoming a call.
- A missing closing `</tool_call>` at the end of the answer is tolerated, as are raw newlines inside JSON strings and arguments given as a JSON string.
- `tool_choice` `"required"` / `{"type": "any"}` and a named tool are passed to the model as an instruction ("you must call ..."). The model is asked, not forced, so check the response.
- `tool_choice: "none"` removes the tools from the request entirely.

### Reliability

Emulated tool calling depends on the model following the format. Capable models do it reliably. With opencode's free models, tested 2026-11-11 on opencode 2.0.31:

| Works on both endpoints | Partly | Not reliable |
|---|---|---|
| `opencode/big-pickle`, `opencode/longcat-2.5-free`, `opencode/minimax-m3.1-flash-free` | `deepseek-v4.1-flash-free`, `inkling-2-small-free` | `glm-5.3-flash-free`, `flint-40b-free`, `iceman-free`, `beam-free`, `nemotron-3.5-lightning-free`, `mimo-v2.6-flash-free` |

Models in the last column try to use opencode's built-in tool mechanism instead of writing text, and the request fails with `The model returned no text` (HTTP 502, type `upstream_empty`). Choose a working model for agents, and test yours before relying on it: free model line-ups change.

## 10. Errors

### Shapes

OpenAI routes (`/v1/chat/completions`, `/v1/models`, `/health` is never an error):

```json
{"error":{"message":"The model 'gpt-4o' is not available in opencode. Call GET /v1/models for the list, or use 'default'.","type":"invalid_request_error","param":null,"code":"model_not_found"}}
```

Anthropic routes (`/v1/messages`, `/v1/messages/count_tokens`):

```json
{"type":"error","error":{"type":"authentication_error","message":"Incorrect API key provided."}}
```

The official SDKs turn these into their usual exception classes (`openai.NotFoundError`, `anthropic.AuthenticationError`, ...). RelayAI's own SDKs raise `ApiError` with `.status`, `.message`, `.kind` and `.code`.

### All errors

| HTTP | OpenAI `type` / `code` | Anthropic `type` | When | What to do |
|---|---|---|---|---|
| 400 | `invalid_request_error` | `invalid_request_error` | body is not JSON or not an object, `messages` missing or empty, no user message with text, `model_ambiguous` | fix the request |
| 401 | `invalid_request_error` / `invalid_api_key` | `authentication_error` | missing or wrong key | check `relayai status` |
| 404 | `invalid_request_error` / `model_not_found` | `not_found_error` | unknown model (OpenAI only), unknown endpoint | `GET /v1/models` |
| 413 | `invalid_request_error` | `request_too_large` | body over 50 MB | send less |
| 429 | `rate_limit_error` / `server_busy` | `rate_limit_error` | all generation slots busy for 30 s (`RELAYAI_MAX_CONCURRENCY`, `RELAYAI_QUEUE_WAIT`) | retry with backoff, or raise the limit |
| 500 | `server_error` | `api_error` | a bug in RelayAI | report it with `~/.relayai/gateway.log` |
| 502 | `upstream_empty` | `api_error` | the model returned no text (see [reliability](#reliability)) | use another model |
| 502 | `upstream_auth` | `api_error` | opencode rejected RelayAI's password | with `--opencode-url`, pass the matching `--opencode-password` |
| 502 | `upstream_unreachable` | `api_error` | opencode is not running or not reachable | `relayai doctor` |
| 502 | `upstream_wrong_path`, `upstream_error` | `api_error` | opencode answered unexpectedly (other opencode version, provider error) | the message has opencode's text |
| 503 | `upstream_unavailable` | `overloaded_error` | opencode or its provider is unavailable, e.g. `Free usage limit reached` | retry later; connect your own provider in opencode |
| 504 | `upstream_timeout` | `api_error` | no answer within `RELAYAI_TIMEOUT` (600 s) | raise the timeout or use a faster model |

429 and 5xx errors are safe to retry. The official OpenAI and Anthropic SDKs retry 429, 502, 503 and 504 by themselves (twice by default).

## 11. Behaviour and limits

**Every request is independent.** Like the real OpenAI and Anthropic APIs, the server keeps no conversation state; send the whole conversation each time. Internally each request is a short-lived opencode session that is deleted afterwards, and the conversation is folded into a single prompt for the model.

**Answers arrive in one piece.** Streaming keeps the connection busy with an early first chunk and keepalives, then sends the finished answer. There is no token-by-token output.

**Timeouts.**

| Setting | Default | Meaning |
|---|---|---|
| `RELAYAI_TIMEOUT` / `--timeout` | 600 s | longest wait for one answer, then 504 |
| `RELAYAI_KEEPALIVE` | 10 s | interval of keepalive comments / ping events while streaming |
| idle connection | 120 s | a client that connects and sends nothing is dropped |

Set your client's timeout above `RELAYAI_TIMEOUT` for non-streaming requests.

**Concurrency.** At most `RELAYAI_MAX_CONCURRENCY` (4) answers are generated at once. Further requests wait up to `RELAYAI_QUEUE_WAIT` (30 s) for a slot, then get 429. Model listing and `/health` are never queued. opencode's free models have their own rate limit; many parallel requests trigger `Free usage limit reached` (503) for a while.

**Request size.** Up to 50 MB per request body.

**Text only.** Images, audio and documents are replaced by a placeholder.

**Token usage** is estimated on opencode 2.x (about 4 characters per token). It counts RelayAI's added instructions, so prompts with tools look larger than what you sent.

**Ignored generation parameters.** `temperature`, `max_tokens`, `stop` and the like are accepted so existing code keeps working, but have no effect. The model and its settings come from opencode.

## 12. Client integrations

Replace `KEY` with the key from `relayai status`. Every example below was run against both the Python and the Node gateway on 2026-11-11, with these client versions: `openai` 2.8.0 and `anthropic` 3.49.0 (Python), LangChain (`langchain-openai`, `langchain-anthropic`), and `openai` 7.55, `@anthropic-ai/sdk` 1.8, `ai` 7.0 with `@ai-sdk/openai-compatible` 3.0 (Node).

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key=KEY)

r = client.chat.completions.create(model="default", messages=[{"role": "user", "content": "Hi"}])
print(r.choices[0].message.content)

for chunk in client.chat.completions.create(model="default", messages=[{"role": "user", "content": "Hi"}], stream=True):
    if chunk.choices:
        print(chunk.choices[0].delta.content or "", end="")

print([m.id for m in client.models.list()])
```

With `OPENAI_BASE_URL` and `OPENAI_API_KEY` set (`relayai env`), `OpenAI()` needs no arguments.

### OpenAI Node SDK

```js
import OpenAI from "openai";

const client = new OpenAI({ baseURL: "http://127.0.0.1:8000/v1", apiKey: KEY });
const r = await client.chat.completions.create({ model: "default", messages: [{ role: "user", content: "Hi" }] });
console.log(r.choices[0].message.content);

const stream = await client.chat.completions.create({ model: "default", messages: [{ role: "user", content: "Hi" }], stream: true });
for await (const chunk of stream) process.stdout.write(chunk.choices[0]?.delta?.content ?? "");
```

### Anthropic Python SDK

```python
import anthropic

client = anthropic.Anthropic(base_url="http://127.0.0.1:8000", api_key=KEY)

r = client.messages.create(model="default", max_tokens=1024, messages=[{"role": "user", "content": "Hi"}])
print(r.content[0].text)

with client.messages.stream(model="default", max_tokens=1024, messages=[{"role": "user", "content": "Hi"}]) as s:
    for text in s.text_stream:
        print(text, end="")
```

### Anthropic Node SDK

```js
import Anthropic from "@anthropic-ai/sdk";

const client = new Anthropic({ baseURL: "http://127.0.0.1:8000", apiKey: KEY });
const r = await client.messages.create({ model: "default", max_tokens: 1024, messages: [{ role: "user", content: "Hi" }] });
console.log(r.content[0].text);

const stream = client.messages.stream({ model: "default", max_tokens: 1024, messages: [{ role: "user", content: "Hi" }] });
stream.on("text", (t) => process.stdout.write(t));
await stream.finalMessage();
```

### LangChain (Python)

```python
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic

llm = ChatOpenAI(base_url="http://127.0.0.1:8000/v1", api_key=KEY, model="opencode/big-pickle")
print(llm.invoke("Hi").content)

with_tools = llm.bind_tools([weather])            # an OpenAI-style tool dict, a @tool function, or a pydantic model
print(with_tools.invoke("Weather in Paris?").tool_calls)

claude_style = ChatAnthropic(base_url="http://127.0.0.1:8000", api_key=KEY, model="opencode/big-pickle")
print(claude_style.invoke("Hi").content)
```

### Vercel AI SDK

Use the OpenAI-compatible provider (it talks to `/chat/completions`). Install it together with `zod`, which the AI SDK needs but npm does not add by itself:

```bash
npm install ai @ai-sdk/openai-compatible zod
```

```js
import { generateText, streamText, tool, jsonSchema } from "ai";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";

const relay = createOpenAICompatible({ name: "relayai", baseURL: "http://127.0.0.1:8000/v1", apiKey: KEY });

const { text } = await generateText({ model: relay("opencode/big-pickle"), prompt: "Hi" });

const result = streamText({ model: relay("opencode/big-pickle"), prompt: "Hi" });
for await (const piece of result.textStream) process.stdout.write(piece);

const withTools = await generateText({
  model: relay("opencode/big-pickle"),
  prompt: "Weather in Paris?",
  tools: {
    get_weather: tool({
      description: "Current weather for a city",
      inputSchema: jsonSchema({ type: "object", properties: { city: { type: "string" } }, required: ["city"] }),
      execute: async ({ city }) => `18C in ${city}`,
    }),
  },
});
```

### Claude Code

```bash
relayai claude -m opencode/big-pickle
```

That sets everything for that one Claude Code process. To configure it by hand instead (for example in a shell profile):

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_AUTH_TOKEN=KEY
export ANTHROPIC_MODEL=opencode/big-pickle
export ANTHROPIC_DEFAULT_HAIKU_MODEL=opencode/big-pickle   # Claude Code's background model
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
claude
```

Unset `ANTHROPIC_API_KEY` in that shell so your real Anthropic key is not mixed in (`relayai claude` does this for you). Claude Code relies on tool calling, so use a model from the [reliability table](#reliability). Claude Code requests are large (around 16,000 tokens of instructions and tool definitions), which some free models handle slowly.

### Open WebUI and other chat front ends

Add an **OpenAI API** connection (in Open WebUI: Admin Settings, Connections) with URL `http://127.0.0.1:8000/v1` and your key. The model list fills from `/v1/models`.

If the front end runs in Docker, `127.0.0.1` means the container itself. Use `http://host.docker.internal:8000/v1` and start the gateway with `--host 0.0.0.0` (see section 13), since a server bound to `127.0.0.1` is usually not reachable from containers.

### Any other tool

Anything with an "OpenAI compatible" or "custom base URL" option works with base URL `http://127.0.0.1:8000/v1`, your key, and a model id from `/v1/models` (or `default`). Tools that only offer an Anthropic setting take `http://127.0.0.1:8000`.

## 13. Network access, HTTPS and running on boot

### Use it from other machines

```bash
relayai start --host 0.0.0.0 --api-key "$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

Clients then use `http://<this-machine's-ip>:8000/v1` (OpenAI) and `http://<this-machine's-ip>:8000` (Anthropic). Allow the port in the firewall if needed. Everyone with the key can use your opencode models and your providers' quota, so use a long key and share it carefully. The key travels in plain text over `http`, so on networks you do not trust put HTTPS in front (next section).

### HTTPS with a reverse proxy

Keep RelayAI on `127.0.0.1` and let a reverse proxy handle TLS.

**Caddy** (gets certificates automatically and streams SSE without extra settings):

```
api.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

**nginx**:

```nginx
server {
    listen 443 ssl;
    server_name api.example.com;
    # ssl_certificate ... ; ssl_certificate_key ... ;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_buffering off;            # stream SSE as it comes
        proxy_read_timeout 660s;        # longer than RELAYAI_TIMEOUT
        client_max_body_size 50m;
    }
}
```

RelayAI also sends `X-Accel-Buffering: no` on streams, which tells nginx not to buffer them.

**Quick public URL with a tunnel** (for demos): `cloudflared tunnel --url http://127.0.0.1:8000` prints an `https://...trycloudflare.com` address. Anyone who finds it can reach your gateway, so the key is the only protection. Cloudflare closes requests that send nothing for about 100 seconds; streaming requests are safe thanks to the keepalives, but long non-streaming answers can be cut off, so prefer `stream: true` behind it.

### Run it on boot

**Linux (systemd user service)**, `~/.config/systemd/user/relayai.service`:

```ini
[Unit]
Description=RelayAI gateway

[Service]
ExecStart=/full/path/to/relayai serve
Environment=RELAYAI_MODEL=opencode/big-pickle
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
which relayai                               # the path for ExecStart
systemctl --user daemon-reload
systemctl --user enable --now relayai
loginctl enable-linger "$USER"              # keep it running when you are logged out
journalctl --user -u relayai -f             # logs
```

`relayai serve` stops the opencode serve it started when systemd stops it.

**Windows (Task Scheduler, at logon):**

```powershell
(Get-Command relayai).Source               # the path to use below
schtasks /Create /SC ONLOGON /TN RelayAI /TR "\"C:\full\path\to\relayai.exe\" start"
```

The pip install gives `relayai.exe`, the npm install gives `relayai.cmd`; use whichever path the first command prints.

**macOS**: run `relayai start` from a login item, or create a launchd agent that runs `relayai serve`.

### Logs and files

| File (in `~/.relayai`) | Contents |
|---|---|
| `gateway.log` | output of `relayai start` (one line per request: model, tools, status, duration) |
| `opencode.log` | output of the opencode serve RelayAI manages |
| `api_key` | the persistent key |
| `gateway.json`, `opencode.json` | what is running (pids, URLs, the opencode password) |

## 14. What is not supported

| Not available | Note |
|---|---|
| OpenAI Responses API (`/v1/responses`), Assistants, Batches, Files | use chat completions |
| Embeddings, images, audio, moderation endpoints | opencode serves chat models only |
| Image, audio and document inputs | replaced by a placeholder |
| Token-by-token streaming | answers stream in one burst when ready |
| `n` > 1, `logprobs`, JSON mode / `response_format`, seeds | accepted and ignored |
| Exact token counts on opencode 2.x | estimates |
| Prompt caching, extended thinking output | not passed through |
