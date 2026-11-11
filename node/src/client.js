"use strict";
/** A small client for a running RelayAI gateway (or any OpenAI-compatible server). */
const { open, readAll, request } = require("./http");
const { ApiError } = require("./errors");

class Client {
  /**
   * @param {string} baseUrl   gateway URL, with or without /v1
   * @param {string} apiKey
   * @param {{model?: string, timeout?: number}} options  timeout in seconds
   */
  constructor(baseUrl = "http://127.0.0.1:8000", apiKey = "", { model = "default", timeout = 600 } = {}) {
    this.url = baseUrl.replace(/\/+$/, "").replace(/\/v1$/, "");
    this.apiKey = apiKey;
    this.model = model || "default";
    this.timeout = timeout;
  }

  /** OpenAI base URL, for the openai SDK and similar clients. */
  get baseUrl() {
    return this.url + "/v1";
  }

  /** Anthropic base URL, for the @anthropic-ai/sdk and Claude Code. */
  get anthropicBaseUrl() {
    return this.url;
  }

  _headers() {
    return { Authorization: `Bearer ${this.apiKey}`, "Content-Type": "application/json" };
  }

  async _json(method, path, body) {
    let res;
    try {
      res = await request(method, this.url + path, { body, headers: this._headers(), timeout: this.timeout * 1000 });
    } catch (err) {
      throw new ApiError(502, `Cannot reach the gateway at ${this.url} (${err.code || err.message})`, "connection_error");
    }
    let data = null;
    try {
      data = res.text ? JSON.parse(res.text) : null;
    } catch {
      /* not JSON */
    }
    if (res.status >= 400) throw toError(res.status, data, res.text);
    return data;
  }

  /** Gateway and opencode status. Never throws. */
  async health() {
    try {
      const res = await request("GET", this.url + "/health", { timeout: 10000 });
      return JSON.parse(res.text || "{}");
    } catch (err) {
      return { status: "down", error: err.message };
    }
  }

  /** Model ids opencode can use right now, as provider/model. */
  async models() {
    const data = await this._json("GET", "/v1/models");
    return data.data.map((m) => m.id);
  }

  /** Raw POST /v1/chat/completions. Resolves with the OpenAI response object. */
  async complete(body = {}) {
    return this._json("POST", "/v1/chat/completions", { model: this.model, ...body });
  }

  /** Raw POST /v1/messages (Anthropic format). Resolves with the Anthropic response object. */
  async messages(body = {}) {
    return this._json("POST", "/v1/messages", { model: this.model, max_tokens: 4096, ...body });
  }

  /**
   * Ask something and get the answer text. `prompt` is a string or an array of
   * OpenAI-style messages. Extra options go into the request body.
   */
  async chat(prompt, { model, system, ...extra } = {}) {
    const reply = await this.complete({ ...extra, model: model || this.model, messages: toMessages(prompt, system) });
    return reply.choices[0].message.content || "";
  }

  /** Like chat(), but yields the answer in pieces as they arrive. */
  async *stream(prompt, { model, system, ...extra } = {}) {
    const body = { ...extra, model: model || this.model, messages: toMessages(prompt, system), stream: true };
    let res;
    try {
      res = await open("POST", this.url + "/v1/chat/completions", {
        body,
        headers: this._headers(),
        timeout: this.timeout * 1000,
      });
    } catch (err) {
      throw new ApiError(502, `Cannot reach the gateway at ${this.url} (${err.code || err.message})`, "connection_error");
    }
    if (res.statusCode >= 400) {
      const text = await readAll(res);
      let data = null;
      try {
        data = JSON.parse(text);
      } catch {
        /* not JSON */
      }
      throw toError(res.statusCode, data, text);
    }
    res.setEncoding("utf8");
    let buffer = "";
    for await (const chunk of res) {
      buffer += chunk;
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (!line.startsWith("data: ")) continue;
        const data = line.slice(6);
        if (data === "[DONE]") return;
        const event = JSON.parse(data);
        if (event.error) throw new ApiError(502, event.error.message || "stream failed", event.error.type || "api_error");
        for (const choice of event.choices || []) {
          const piece = (choice.delta || {}).content;
          if (piece) yield piece;
        }
      }
    }
  }
}

function toError(status, data, text) {
  const err = (data && data.error) || {};
  const message = (typeof err === "object" ? err.message : String(err)) || String(text || "").slice(0, 300) || `HTTP ${status}`;
  return new ApiError(status, message, (typeof err === "object" && err.type) || "api_error", (typeof err === "object" && err.code) || null);
}

function toMessages(prompt, system) {
  const messages = Array.isArray(prompt) ? [...prompt] : [{ role: "user", content: String(prompt) }];
  if (system) messages.unshift({ role: "system", content: system });
  return messages;
}

module.exports = { Client };
