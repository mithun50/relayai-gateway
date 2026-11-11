"use strict";
// SDK tests against the fake opencode in ../../conformance. Run: npm test
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { after, before, describe, test } = require("node:test");

process.env.RELAYAI_HOME = fs.mkdtempSync(path.join(os.tmpdir(), "relayai-node-test-"));
const { ApiError, Client, Gateway, RelayAI, parseToolCalls } = require("../src/index.js");
const state = require("../src/state");

const FAKE = path.join(__dirname, "..", "..", "conformance", "fake_opencode.py");
const PYTHON = process.platform === "win32" ? "python" : "python3";

describe("parseToolCalls", () => {
  test("parses calls and keeps text", () => {
    const r = parseToolCalls('Sure.\n<tool_call>\n{"name": "f", "arguments": {"a": 1}}\n</tool_call>', ["f"]);
    assert.equal(r.text, "Sure.");
    assert.deepEqual(r.calls, [{ name: "f", input: { a: 1 } }]);
  });
  test("unknown tool is left as text", () => {
    const r = parseToolCalls('<tool_call>{"name": "x", "arguments": {}}</tool_call>', ["f"]);
    assert.deepEqual(r.calls, []);
    assert.match(r.text, /tool_call/);
  });
  test("missing closing tag and string arguments", () => {
    const r = parseToolCalls('<tool_call>{"name": "f", "arguments": "{\\"a\\": 2}"}', ["f"]);
    assert.deepEqual(r.calls, [{ name: "f", input: { a: 2 } }]);
  });
  test("raw newlines inside strings are accepted", () => {
    const r = parseToolCalls('<tool_call>{"name": "f", "arguments": {"code": "line1\nline2"}}</tool_call>', ["f"]);
    assert.deepEqual(r.calls, [{ name: "f", input: { code: "line1\nline2" } }]);
  });
});

describe("SDK against a fake opencode", () => {
  let fake;
  let url;

  before(async () => {
    const port = await state.freePort(0);
    url = `http://127.0.0.1:${port}`;
    fake = spawn(PYTHON, [FAKE, "serve", "--port", String(port)], {
      env: { ...process.env, OPENCODE_SERVER_PASSWORD: "testpw" },
      stdio: "ignore",
    });
    for (let i = 0; i < 100; i++) {
      if ((await new Client(url).health()).status !== "down") break;
      await state.sleep(100);
    }
  });

  after(() => fake.kill());

  const start = (extra = {}) => RelayAI.start({ reuse: false, opencodeUrl: url, opencodePassword: "testpw", ...extra });

  test("chat, stream, models, health", async () => {
    const ai = await start();
    try {
      assert.equal(await ai.chat("Say PONG"), "PONG");
      let streamed = "";
      for await (const piece of ai.stream("Say PONG")) streamed += piece;
      assert.equal(streamed, "PONG");
      assert.deepEqual(await ai.models(), ["fake/echo", "fake/toolbot"]);
      assert.ok(ai.baseUrl.endsWith("/v1"));
      assert.equal((await ai.health()).status, "ok");
    } finally {
      await ai.close();
    }
  });

  test("tool round trip with complete()", async () => {
    const tool = {
      type: "function",
      function: {
        name: "get_weather",
        parameters: { type: "object", properties: { city: { type: "string" } }, required: ["city"] },
      },
    };
    const ai = await start({ model: "fake/toolbot" });
    try {
      const reply = await ai.complete({ messages: [{ role: "user", content: "Weather?" }], tools: [tool] });
      const call = reply.choices[0].message.tool_calls[0];
      assert.equal(call.function.name, "get_weather");
      const followup = [
        { role: "user", content: "Weather?" },
        reply.choices[0].message,
        { role: "tool", tool_call_id: call.id, content: "sunny" },
      ];
      assert.equal(await ai.chat(followup, { tools: [tool] }), "Tool said: sunny");
    } finally {
      await ai.close();
    }
  });

  test("Anthropic messages()", async () => {
    const ai = await start();
    try {
      const reply = await ai.messages({ messages: [{ role: "user", content: "hi" }] });
      assert.equal(reply.content[0].text, "PONG");
    } finally {
      await ai.close();
    }
  });

  test("errors are ApiError with the HTTP status", async () => {
    const ai = await start();
    try {
      await assert.rejects(ai.chat("hi", { model: "nope/nope" }), (e) => e instanceof ApiError && e.status === 404);
      await assert.rejects(ai.chat("FAIL_QUOTA"), (e) => e instanceof ApiError && e.status === 503);
      await assert.rejects(new Client(ai.url, "wrong").models(), (e) => e instanceof ApiError && e.status === 401);
    } finally {
      await ai.close();
    }
  });

  test("embedded Gateway", async () => {
    const gw = await Gateway.create({ apiKey: "k", port: 0, opencodeUrl: url, opencodePassword: "testpw" }, { quiet: true });
    try {
      assert.equal(await new Client(gw.baseUrl, "k").chat("hi"), "PONG");
    } finally {
      await gw.close();
    }
  });

  test("official openai package, if installed", async (t) => {
    let OpenAI;
    try {
      OpenAI = require("openai");
    } catch {
      t.skip("openai package not installed");
      return;
    }
    const ai = await start();
    try {
      const client = new (OpenAI.OpenAI || OpenAI)({ baseURL: ai.baseUrl, apiKey: ai.apiKey });
      const r = await client.chat.completions.create({ model: "default", messages: [{ role: "user", content: "hi" }] });
      assert.equal(r.choices[0].message.content, "PONG");
    } finally {
      await ai.close();
    }
  });
});
