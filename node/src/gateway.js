"use strict";
/**
 * The HTTP gateway: OpenAI and Anthropic compatible endpoints in front of opencode.
 *
 *   GET  /v1/models                   models opencode can use right now
 *   GET  /v1/models/{id}              one model
 *   POST /v1/chat/completions         OpenAI format, stream true or false, tools
 *   POST /v1/messages                 Anthropic format, stream true or false, tools
 *   POST /v1/messages/count_tokens    rough estimate
 *   GET  /health                      gateway and opencode status (no key needed)
 */
const crypto = require("node:crypto");
const http = require("node:http");

const P = require("./protocol");
const { version } = require("./version");
const { configFromEnv } = require("./config");
const { ApiError } = require("./errors");
const { OpencodeClient, OpencodeManager } = require("./opencode");

const { log } = require("./log");

const MAX_BODY_BYTES = 50 * 1024 * 1024;

/** A counting semaphore whose acquire() gives up after a timeout. */
class Semaphore {
  constructor(n) {
    this.free = n;
    this.queue = [];
  }

  acquire(ms) {
    if (this.free > 0) {
      this.free--;
      return Promise.resolve(true);
    }
    return new Promise((resolve) => {
      const entry = { resolve };
      entry.timer = setTimeout(() => {
        this.queue.splice(this.queue.indexOf(entry), 1);
        resolve(false);
      }, ms);
      this.queue.push(entry);
    });
  }

  release() {
    const next = this.queue.shift();
    if (next) {
      clearTimeout(next.timer);
      next.resolve(true);
    } else this.free++;
  }
}

/** The list of usable models, read from opencode and cached briefly. */
class ModelCatalog {
  constructor(client, ttl) {
    this.client = client;
    this.ttl = ttl;
    this.list = [];
    this.fetched = 0;
  }

  async ids(force = false) {
    if (this.list.length && !force && Date.now() - this.fetched < this.ttl * 1000) return [...this.list];
    try {
      this.list = await this.client.modelIds();
      this.fetched = Date.now();
    } catch (err) {
      if (!this.list.length) throw err;
      log.warn("could not refresh the model list from opencode, using the last known list");
    }
    return [...this.list];
  }
}

function split(id) {
  const i = id.indexOf("/");
  return { providerID: id.slice(0, i), modelID: id.slice(i + 1) };
}

/**
 * The gateway server.
 *
 *   const gw = await Gateway.create({ apiKey: "secret", port: 0 });
 *   console.log(gw.baseUrl);   // http://127.0.0.1:<port>/v1
 *   await gw.close();
 *
 * Without opencodeUrl, opencode serve is started (or reused) through
 * OpencodeManager. close() stops it only if this gateway started it and
 * stopOpencode is true.
 */
class Gateway {
  /**
   * @param {object} settings  Config overrides (see configFromEnv), or a full config.
   * @param {{opencode?: OpencodeClient, stopOpencode?: boolean, quiet?: boolean}} options
   */
  static async create(settings = {}, options = {}) {
    const gw = new Gateway();
    gw.config = configFromEnv(settings);
    if (!gw.config.apiKey) throw new TypeError("apiKey must be set");
    if (options.quiet) log.quiet = true;
    gw.manager = null;
    gw.startedOpencode = false;
    gw.stopOpencode = options.stopOpencode !== false;
    if (options.opencode) gw.opencode = options.opencode;
    else if (gw.config.opencodeUrl) {
      gw.opencode = new OpencodeClient(gw.config.opencodeUrl, {
        username: gw.config.opencodeUsername,
        password: gw.config.opencodePassword,
      });
    } else {
      gw.manager = new OpencodeManager({
        port: gw.config.opencodePort,
        binary: gw.config.opencodeBin,
        username: gw.config.opencodeUsername,
      });
      const { client, started } = await gw.manager.ensure();
      gw.opencode = client;
      gw.startedOpencode = started;
    }
    gw.opencode.timeout = gw.config.timeout;
    gw.catalog = new ModelCatalog(gw.opencode, gw.config.modelCacheTtl);
    gw.slots = new Semaphore(Math.max(1, gw.config.maxConcurrency));
    gw.server = http.createServer((req, res) => gw._handle(req, res));
    gw.server.requestTimeout = 0; // answers can take minutes
    gw.server.headersTimeout = 120000;
    gw.closed = false;
    try {
      await new Promise((resolve, reject) => {
        gw.server.once("error", reject);
        gw.server.listen({ port: gw.config.port, host: gw.config.host, exclusive: true }, resolve);
      });
    } catch (err) {
      if (gw.manager && gw.startedOpencode && gw.stopOpencode) gw.manager.stop();
      throw err;
    }
    return gw;
  }

  get port() {
    return this.server.address().port;
  }

  get url() {
    const host = ["0.0.0.0", "", "::"].includes(this.config.host) ? "127.0.0.1" : this.config.host;
    return `http://${host}:${this.port}`;
  }

  /** OpenAI base URL (ends in /v1). */
  get baseUrl() {
    return this.url + "/v1";
  }

  async close() {
    if (this.closed) return;
    this.closed = true;
    await new Promise((resolve) => {
      this.server.close(() => resolve());
      if (this.server.closeAllConnections) this.server.closeAllConnections();
    });
    if (this.manager && this.startedOpencode && this.stopOpencode) this.manager.stop();
  }

  // ------------------------------------------------------------------ models
  /**
   * Client model name -> { model, name }. model null means: let opencode use
   * its own default model.
   */
  async resolveModel(requested, strict = this.config.strictModels) {
    const fallback = this.config.model;
    let name = String(requested || "").trim();
    if (P.DEFAULT_NAMES.includes(name.toLowerCase())) {
      name = fallback;
      if (!name) return { model: null, name: "default" };
    }
    let ids = await this.catalog.ids();
    if (!ids.includes(name) && strict) ids = await this.catalog.ids(true); // it may have been added just now
    if (ids.includes(name)) return { model: split(name), name };
    const bare = ids.filter((i) => i.slice(i.indexOf("/") + 1) === name);
    if (bare.length === 1) return { model: split(bare[0]), name: bare[0] };
    if (bare.length > 1) {
      throw new ApiError(
        400,
        `Model '${name}' exists under several providers. Use one of: ${bare.join(", ")}`,
        "invalid_request_error",
        "model_ambiguous"
      );
    }
    if (!strict) {
      if (ids.includes(fallback)) return { model: split(fallback), name: fallback };
      return { model: null, name: "default" };
    }
    throw new ApiError(
      404,
      `The model '${name}' is not available in opencode. Call GET /v1/models for the list, or use 'default'.`,
      "invalid_request_error",
      "model_not_found"
    );
  }

  async generate(core, model, box) {
    const { system, prompt } = P.fold(core.system, core.turns, core.tools, core.force);
    const out = await this.opencode.generate(system, prompt, model, box);
    const { text, calls } = P.finish(out.text, core.tools);
    return { text, calls, usage: out.usage, used: out.used };
  }

  // -------------------------------------------------------------- HTTP layer
  async _handle(req, res) {
    const started = Date.now();
    const ctx = { req, res, anthropic: false, status: 0 };
    let note = "?";
    try {
      let route = decodeURIComponent((req.url || "/").split("?")[0]).replace(/\/+$/, "") || "/";
      if (!["/", "/health"].includes(route) && !route.startsWith("/v1")) route = "/v1" + route;
      ctx.anthropic = route.startsWith("/v1/messages");

      if (req.method === "OPTIONS") {
        res.writeHead(204, cors());
        return res.end();
      }
      if (req.method === "HEAD") {
        res.writeHead(200, { "Content-Length": "0" });
        return res.end();
      }
      if (req.method === "GET") {
        note = "";
        if (route === "/" || route === "/health") {
          let health = { reachable: false, version: null };
          try {
            health = await this.opencode.health();
          } catch {
            /* wrong password: report as unreachable */
          }
          return sendJson(ctx, health.reachable ? 200 : 503, {
            status: health.reachable ? "ok" : "degraded",
            service: "RelayAI",
            version,
            opencode: { url: this.opencode.url, reachable: health.reachable, version: health.version },
          });
        }
        this._checkKey(req);
        if (route === "/v1/models") {
          const ids = await this.catalog.ids();
          return sendJson(ctx, 200, { object: "list", data: ids.map(P.modelJson) });
        }
        if (route.startsWith("/v1/models/")) {
          const { name } = await this.resolveModel(route.slice("/v1/models/".length), true);
          return sendJson(ctx, 200, P.modelJson(name));
        }
        throw new ApiError(404, `Unknown endpoint: GET ${route}`);
      }
      if (req.method === "POST") {
        this._checkKey(req);
        if (route === "/v1/chat/completions") note = await this._openaiChat(ctx, await readJson(req));
        else if (route === "/v1/messages") note = await this._anthropicMessages(ctx, await readJson(req));
        else if (route === "/v1/messages/count_tokens") {
          const body = await readJson(req);
          note = "";
          return sendJson(ctx, 200, { input_tokens: Math.max(1, Math.floor(JSON.stringify(body).length / 4)) });
        } else throw new ApiError(404, `Unknown endpoint: POST ${route}`);
        return undefined;
      }
      throw new ApiError(404, `Unknown endpoint: ${req.method} ${route}`);
    } catch (err) {
      const apiErr =
        err instanceof ApiError ? err : new ApiError(500, `RelayAI error: ${err && err.message}`, "server_error");
      if (!(err instanceof ApiError)) log.warn(`unexpected error: ${err && err.stack}`);
      if (apiErr.status >= 500) log.warn(`${req.method} ${req.url} -> ${apiErr.status} ${apiErr.message}`);
      if (!res.headersSent) sendJson(ctx, apiErr.status, ctx.anthropic ? apiErr.anthropicBody() : apiErr.body());
      else res.end();
    } finally {
      if (req.method === "POST" && note !== "") {
        log.info(`POST ${(req.url || "").split("?")[0]} ${note} status=${ctx.status} ${((Date.now() - started) / 1000).toFixed(1)}s`);
      }
    }
    return undefined;
  }

  _checkKey(req) {
    const auth = req.headers.authorization || "";
    let given = auth.toLowerCase().startsWith("bearer ") ? auth.slice(7).trim() : "";
    if (!given) given = req.headers["x-api-key"] || req.headers["api-key"] || "";
    const a = Buffer.from(String(given));
    const b = Buffer.from(this.config.apiKey);
    if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) {
      throw new ApiError(401, "Incorrect API key provided.", "invalid_request_error", "invalid_api_key");
    }
  }

  async _takeSlot() {
    if (!(await this.slots.acquire(this.config.queueWait * 1000))) {
      throw new ApiError(
        429,
        `RelayAI is busy (${this.config.maxConcurrency} requests running). Try again shortly.`,
        "rate_limit_error",
        "server_busy"
      );
    }
  }

  // ----------------------------------------------------------- OpenAI chat
  async _openaiChat(ctx, req) {
    const core = P.openaiToCore(req);
    P.fold(core.system, core.turns, core.tools, core.force); // validate before taking a slot
    const { model, name: modelName } = await this.resolveModel(req.model);
    const completionId = "chatcmpl-" + P.hex(32);
    const created = Math.floor(Date.now() / 1000);
    const note = `model=${modelName} tools=${core.tools.length}`;
    await this._takeSlot();
    const job = (box) => this.generate(core, model, box);

    if (!req.stream) {
      let result;
      try {
        result = await job({});
      } finally {
        this.slots.release();
      }
      const message = { role: "assistant", content: result.text || null };
      if (result.calls.length) message.tool_calls = P.openaiToolCalls(result.calls);
      sendJson(ctx, 200, {
        id: completionId,
        object: "chat.completion",
        created,
        model: result.used || modelName,
        choices: [{ index: 0, message, logprobs: null, finish_reason: result.calls.length ? "tool_calls" : "stop" }],
        usage: result.usage,
      });
      return note;
    }

    const sse = (obj) => ctx.res.write(`data: ${typeof obj === "string" ? obj : JSON.stringify(obj)}\n\n`);
    await this._stream(ctx, job, {
      open: () => sse(P.chunkJson(completionId, created, modelName, { role: "assistant", content: "" })),
      ping: () => ctx.res.write(": keepalive\n\n"),
      result: (result) => {
        const name = result.used || modelName;
        for (let i = 0; i < result.text.length; i += 48) {
          sse(P.chunkJson(completionId, created, name, { content: result.text.slice(i, i + 48) }));
        }
        P.openaiToolCalls(result.calls).forEach((call, index) => {
          sse(P.chunkJson(completionId, created, name, { tool_calls: [{ index, ...call }] }));
        });
        sse(P.chunkJson(completionId, created, name, {}, result.calls.length ? "tool_calls" : "stop"));
        if ((req.stream_options || {}).include_usage) {
          const final = P.chunkJson(completionId, created, name, {});
          final.choices = [];
          final.usage = result.usage;
          sse(final);
        }
        sse("[DONE]");
      },
      error: (err) => {
        sse(err.body());
        sse("[DONE]");
      },
    });
    return note;
  }

  // ------------------------------------------- Anthropic messages (Claude Code)
  async _anthropicMessages(ctx, req) {
    const core = P.anthropicToCore(req);
    P.fold(core.system, core.turns, core.tools, core.force); // validate before taking a slot
    const requested = String(req.model || "default");
    const { model, name: modelName } = await this.resolveModel(requested, false);
    const messageId = "msg_" + P.hex(24);
    const note = `model=${modelName} (asked ${requested}) tools=${core.tools.length}`;
    await this._takeSlot();
    const job = (box) => this.generate(core, model, box);
    const envelope = (content, stopReason, usage) => ({
      id: messageId,
      type: "message",
      role: "assistant",
      model: requested,
      content,
      stop_reason: stopReason,
      stop_sequence: null,
      usage,
    });

    if (!req.stream) {
      let result;
      try {
        result = await job({});
      } finally {
        this.slots.release();
      }
      const stop = result.calls.length ? "tool_use" : "end_turn";
      sendJson(ctx, 200, envelope(P.anthropicContent(result), stop, P.anthropicUsage(result.usage)));
      return note;
    }

    const sse = (obj, event) => ctx.res.write(`event: ${event}\ndata: ${JSON.stringify(obj)}\n\n`);
    await this._stream(ctx, job, {
      open: () => {
        sse({ type: "message_start", message: envelope([], null, { input_tokens: 0, output_tokens: 0 }) }, "message_start");
        sse({ type: "ping" }, "ping");
      },
      ping: () => sse({ type: "ping" }, "ping"),
      result: (result) => {
        P.anthropicContent(result).forEach((block, index) => {
          let opening;
          let deltas;
          if (block.type === "text") {
            opening = { type: "text", text: "" };
            deltas = [];
            for (let i = 0; i < block.text.length; i += 64) deltas.push({ type: "text_delta", text: block.text.slice(i, i + 64) });
          } else {
            opening = { ...block, input: {} };
            deltas = [{ type: "input_json_delta", partial_json: JSON.stringify(block.input) }];
          }
          sse({ type: "content_block_start", index, content_block: opening }, "content_block_start");
          for (const delta of deltas) sse({ type: "content_block_delta", index, delta }, "content_block_delta");
          sse({ type: "content_block_stop", index }, "content_block_stop");
        });
        const stop = result.calls.length ? "tool_use" : "end_turn";
        sse(
          { type: "message_delta", delta: { stop_reason: stop, stop_sequence: null }, usage: P.anthropicUsage(result.usage) },
          "message_delta"
        );
        sse({ type: "message_stop" }, "message_stop");
      },
      error: (err) => sse(err.anthropicBody(), "error"),
    });
    return note;
  }

  // ------------------------------------------------------ shared streaming loop
  /**
   * opencode answers in one piece, so the reply is sent once it is ready, not
   * token by token. While the model works, pings keep the connection open. If
   * the client hangs up, the opencode run is cancelled.
   */
  async _stream(ctx, job, on) {
    const { res } = ctx;
    const box = {};
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
      Connection: "close",
      ...cors(),
    });
    ctx.status = 200;
    let gone = false;
    res.on("close", () => {
      if (!res.writableEnded) {
        gone = true;
        ctx.status = 499;
        box.cancelled = true;
        if (box.session) {
          log.info(`client disconnected, cancelling opencode session ${box.session}`);
          this.opencode.cancel(box.session);
        }
      }
    });
    on.open();
    const timer = setInterval(() => {
      if (!gone) on.ping();
    }, this.config.keepalive * 1000);
    try {
      const result = await job(box);
      if (!gone) on.result(result);
    } catch (err) {
      const apiErr = err instanceof ApiError ? err : new ApiError(500, `RelayAI error: ${err && err.message}`, "server_error");
      ctx.status = apiErr.status;
      if (apiErr.status >= 500) log.warn(`stream failed: ${apiErr.message}`);
      if (!gone) on.error(apiErr);
    } finally {
      clearInterval(timer);
      this.slots.release();
      if (!gone) res.end();
    }
  }
}

// `await using gw = await Gateway.create()` where the runtime supports it (Node 20.4+ / TS 5.2+)
if (Symbol.asyncDispose) {
  Gateway.prototype[Symbol.asyncDispose] = function dispose() {
    return this.close();
  };
}

function cors() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  };
}

function sendJson(ctx, status, obj) {
  ctx.status = status;
  const payload = Buffer.from(JSON.stringify(obj));
  ctx.res.writeHead(status, { "Content-Type": "application/json", "Content-Length": payload.length, ...cors() });
  ctx.res.end(payload);
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    const declared = Number.parseInt(req.headers["content-length"] || "0", 10);
    if (declared > MAX_BODY_BYTES) {
      reject(new ApiError(413, `Request body is larger than ${MAX_BODY_BYTES / (1024 * 1024)} MB`));
      req.resume();
      return;
    }
    const chunks = [];
    let size = 0;
    req.on("data", (c) => {
      size += c.length;
      if (size > MAX_BODY_BYTES) {
        reject(new ApiError(413, `Request body is larger than ${MAX_BODY_BYTES / (1024 * 1024)} MB`));
        req.destroy();
      } else chunks.push(c);
    });
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      if (!raw.trim()) return reject(new ApiError(400, "Request body is empty"));
      let body;
      try {
        body = JSON.parse(raw);
      } catch {
        return reject(new ApiError(400, "Request body is not valid JSON"));
      }
      if (!body || typeof body !== "object" || Array.isArray(body)) {
        return reject(new ApiError(400, "Request body must be a JSON object"));
      }
      return resolve(body);
    });
    req.on("error", reject);
  });
}

module.exports = { Gateway, Semaphore, log };
