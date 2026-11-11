"use strict";
/**
 * Talking to opencode, and running it.
 *
 * OpencodeClient  speaks opencode's own HTTP API (1.x and 2.x, detected automatically).
 * OpencodeManager finds or installs the opencode executable and runs `opencode serve`
 *                 in the background with a generated password.
 */
const crypto = require("node:crypto");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const state = require("./state");
const { request } = require("./http");
const { ApiError, OpencodeNotInstalled } = require("./errors");

// opencode 1.x only: its tools are switched off per request. The full list is
// read from opencode, this is the fallback.
const FALLBACK_TOOLS_OFF = { bash: false, edit: false, write: false, patch: false };

const EMPTY_REPLY =
  "The model returned no text. Some models answer a tool request with opencode's own " +
  "native tool call instead of text; try another model (see `relayai models`).";

const { log } = require("./log");

class OpencodeClient {
  constructor(url, { username = "opencode", password = "", timeout = 600 } = {}) {
    this.url = url.replace(/\/+$/, "");
    this.username = username || "opencode";
    this.password = password || "";
    this.timeout = timeout;
    this._version = null;
    this._toolsOff = { at: 0, map: null };
  }

  // ------------------------------------------------------------ transport
  /**
   * One HTTP call to opencode, resolving with parsed JSON (or null for an empty
   * body). `retries` only applies when no connection could be made at all, so a
   * request is never sent twice.
   */
  async request(method, urlPath, body, { timeout = 30, retries = 0 } = {}) {
    const headers = { "Content-Type": "application/json" };
    if (this.password) {
      headers.Authorization = "Basic " + Buffer.from(`${this.username}:${this.password}`).toString("base64");
    }
    for (let attempt = 0; ; attempt++) {
      let res;
      try {
        res = await request(method, this.url + urlPath, { body, headers, timeout: timeout * 1000 });
      } catch (err) {
        if (err.code === "ETIMEOUT") {
          throw new ApiError(504, `opencode did not answer within ${timeout} seconds.`, "upstream_timeout");
        }
        if (err.code === "ECONNREFUSED" && attempt < retries) {
          await state.sleep(500 * (attempt + 1));
          continue;
        }
        throw new ApiError(
          502,
          `Cannot reach opencode at ${this.url} (${err.code || err.message}). Is \`opencode serve\` running?`,
          "upstream_unreachable"
        );
      }
      const text = res.text;
      if (res.status === 401) {
        throw new ApiError(
          502,
          "opencode rejected the login. Set OPENCODE_SERVER_PASSWORD (and OPENCODE_SERVER_USERNAME " +
            "if changed) to match the opencode server.",
          "upstream_auth"
        );
      }
      if (res.status >= 400) {
        let parsed = null;
        try {
          parsed = JSON.parse(text);
        } catch {
          /* not JSON */
        }
        if (parsed && typeof parsed === "object" && parsed.message) {
          const tag = parsed._tag || "Error";
          if (tag === "ServiceUnavailableError") {
            // a provider quota, rate limit or outage: worth retrying later
            throw new ApiError(503, `opencode: ${parsed.message}`, "upstream_unavailable");
          }
          throw new ApiError(502, `opencode ${tag} for ${method} ${urlPath}: ${parsed.message}`, "upstream_error");
        }
        throw new ApiError(
          502,
          `opencode returned HTTP ${res.status} for ${method} ${urlPath}: ${text.slice(0, 600)}`,
          "upstream_error"
        );
      }
      const html = String(res.headers["content-type"] || "").includes("text/html");
      if (html || text.trimStart().slice(0, 15).toLowerCase().startsWith("<!doctype")) {
        // opencode 2.x serves its web UI for every path it does not know
        throw new ApiError(
          502,
          `opencode has no API at ${method} ${urlPath} (it sent a web page)`,
          "upstream_wrong_path"
        );
      }
      if (!text.trim()) return null;
      try {
        return JSON.parse(text);
      } catch {
        throw new ApiError(502, `opencode sent a reply that is not JSON: ${text.slice(0, 200)}`, "upstream_error");
      }
    }
  }

  // ------------------------------------------------------------ discovery
  /** 'v2' for opencode 2.x (routes under /api), else 'v1'. Remembered once known. */
  async apiVersion() {
    if (this._version) return this._version;
    let version = "v1";
    try {
      const info = await this.request("GET", "/api/info", undefined, { timeout: 10, retries: 2 });
      if (info && typeof info === "object" && info.version) version = "v2";
    } catch (err) {
      if (["upstream_unreachable", "upstream_timeout", "upstream_auth"].includes(err.kind)) throw err;
    }
    this._version = version;
    return version;
  }

  /** { reachable, version }. Throws only for a wrong password. */
  async health() {
    try {
      const p = (await this.apiVersion()) === "v2" ? "/api/info" : "/global/health";
      const info = await this.request("GET", p, undefined, { timeout: 5 });
      return { reachable: true, version: (info && info.version) || null };
    } catch (err) {
      if (err.kind === "upstream_auth") throw err;
      if (["upstream_unreachable", "upstream_timeout"].includes(err.kind)) return { reachable: false, version: null };
      return { reachable: true, version: null };
    }
  }

  /** Every usable model as provider/model, read live from opencode. */
  async modelIds() {
    const ids = new Set();
    if ((await this.apiVersion()) === "v2") {
      const listing = (await this.request("GET", "/api/model", undefined, { timeout: 15, retries: 2 })) || {};
      for (const m of listing.data || []) {
        if (m && m.providerID && m.modelID && m.enabled !== false) ids.add(`${m.providerID}/${m.modelID}`);
      }
    } else {
      const config = (await this.request("GET", "/config/providers", undefined, { timeout: 15, retries: 2 })) || {};
      for (const provider of config.providers || []) {
        const models = provider.models || {};
        const keys = Array.isArray(models) ? models.map((m) => m && m.id) : Object.keys(models);
        for (const k of keys) if (provider.id && k) ids.add(`${provider.id}/${k}`);
      }
    }
    return [...ids].sort();
  }

  // ------------------------------------------------------------ generation
  /**
   * One answer from opencode. `model` is { providerID, modelID } or null for
   * opencode's default. `box` receives the session id so the caller can cancel.
   * Resolves with { text, usage, used }.
   */
  async generate(system, prompt, model = null, box = {}) {
    if ((await this.apiVersion()) === "v2") return this._generateV2(system, prompt, model, box);
    return this._generateV1(system, prompt, model, box);
  }

  async cancel(sessionId) {
    const v2 = (await this.apiVersion()) === "v2";
    try {
      await this.request(
        "POST",
        v2 ? `/api/session/${sessionId}/interrupt` : `/session/${sessionId}/abort`,
        v2 ? {} : undefined,
        { timeout: 10 }
      );
    } catch {
      /* best effort */
    }
  }

  /**
   * opencode 2.x: a throwaway session answered with POST /api/session/:id/generate,
   * which does not run opencode's agent loop, so opencode's tools never run.
   *
   * Measured on opencode 2.0.31 with its free models:
   *   - POST /api/experimental/generate (stateless) is refused with
   *     "Free usage limit reached" even when sessions work, so it is not used.
   *   - A session created with a `permissions` list is refused the same way.
   * The call takes one prompt string, so the system text goes on top. It
   * reports no token counts, so usage is estimated at ~4 characters per token.
   */
  async _generateV2(system, prompt, model, box) {
    const create = { title: "RelayAI" };
    if (model) create.model = { providerID: model.providerID, id: model.modelID };
    const session = await this.request("POST", "/api/session", create, { retries: 2 });
    const sessionId = session && session.data && session.data.id;
    if (!sessionId) throw new ApiError(502, "opencode did not return a session id", "upstream_error");
    box.session = sessionId;
    const full = `# Instructions\n\n${system}\n\n# Request\n\n${prompt}`;
    let result;
    try {
      result = await this.request("POST", `/api/session/${sessionId}/generate`, { prompt: full }, { timeout: this.timeout });
    } finally {
      try {
        await this.request("DELETE", `/api/session/${sessionId}`, undefined, { timeout: 15 });
      } catch (err) {
        log.warn(`could not delete opencode session ${sessionId}: ${err.message}`);
      }
    }
    if (box.cancelled) throw new ApiError(499, "Client closed the request", "cancelled");
    const text = String((result && result.data && result.data.text) || "").trim();
    if (!text) throw new ApiError(502, EMPTY_REPLY, "upstream_empty");
    const promptTokens = Math.max(1, Math.floor(full.length / 4));
    const completionTokens = Math.max(1, Math.floor(text.length / 4));
    return {
      text,
      usage: { prompt_tokens: promptTokens, completion_tokens: completionTokens, total_tokens: promptTokens + completionTokens },
      used: model ? `${model.providerID}/${model.modelID}` : null,
    };
  }

  async _toolsOffMap() {
    const cache = this._toolsOff;
    if (cache.map === null || Date.now() - cache.at > 300000) {
      try {
        const ids = await this.request("GET", "/experimental/tool/ids", undefined, { timeout: 10 });
        if (Array.isArray(ids) && ids.length) {
          cache.map = { ...FALLBACK_TOOLS_OFF, ...Object.fromEntries(ids.map((i) => [String(i), false])) };
          cache.at = Date.now();
        }
      } catch {
        /* use the fallback */
      }
    }
    return cache.map || FALLBACK_TOOLS_OFF;
  }

  /** opencode 1.x: a throwaway session with all of opencode's tools switched off. */
  async _generateV1(system, prompt, model, box) {
    const session = await this.request("POST", "/session", { title: "RelayAI" }, { retries: 2 });
    const sessionId = session && session.id;
    if (!sessionId) throw new ApiError(502, "opencode did not return a session id", "upstream_error");
    box.session = sessionId;
    let finished = false;
    try {
      const body = { parts: [{ type: "text", text: prompt }], system, tools: await this._toolsOffMap() };
      if (model) body.model = model;
      const result = await this.request("POST", `/session/${sessionId}/message`, body, { timeout: this.timeout });
      finished = true;
      if (box.cancelled) throw new ApiError(499, "Client closed the request", "cancelled");
      if (!result || typeof result !== "object") {
        throw new ApiError(
          502,
          "opencode returned an empty response. Check that a provider and model are set up in opencode.",
          "upstream_error"
        );
      }
      const info = result.info || {};
      if (info.error) throw new ApiError(502, "opencode error: " + errorText(info.error), "upstream_error");
      const text = (result.parts || [])
        .filter((p) => p && p.type === "text" && p.text)
        .map((p) => p.text)
        .join("\n")
        .trim();
      if (!text) throw new ApiError(502, EMPTY_REPLY, "upstream_empty");
      const tokens = info.tokens || {};
      const promptTokens = Number(tokens.input || 0);
      const completionTokens = Number(tokens.output || 0) + Number(tokens.reasoning || 0);
      return {
        text,
        usage: { prompt_tokens: promptTokens, completion_tokens: completionTokens, total_tokens: promptTokens + completionTokens },
        used: info.providerID && info.modelID ? `${info.providerID}/${info.modelID}` : null,
      };
    } finally {
      if (!finished) await this.cancel(sessionId);
      try {
        await this.request("DELETE", `/session/${sessionId}`, undefined, { timeout: 15 });
      } catch (err) {
        log.warn(`could not delete opencode session ${sessionId}: ${err.message}`);
      }
    }
  }
}

function errorText(err) {
  if (err && typeof err === "object" && err.data && err.data.message) {
    return `${err.name || "Error"}: ${err.data.message}`;
  }
  return JSON.stringify(err).slice(0, 600);
}

// ---------------------------------------------------------------------- manager

/**
 * Runs `opencode serve` for relayai.
 *
 * ensure() reuses the server relayai started earlier when it is still alive,
 * otherwise starts a new one on a free port with a fresh password, in the
 * background, inside ~/.relayai/workspace. Its details live in
 * ~/.relayai/opencode.json so other relayai processes (Node or Python) share it.
 */
class OpencodeManager {
  constructor({ port = 4096, binary = "", username = "opencode" } = {}) {
    this.port = port;
    this.binary = binary;
    this.username = username || "opencode";
  }

  findBinary() {
    return this.binary ? state.which(this.binary) : state.which("opencode");
  }

  /** Installed opencode version string, or null. */
  version() {
    const binary = this.findBinary();
    if (!binary) return null;
    const c = state.commandFor(binary, ["--version"]);
    const out = spawnSync(c.file, c.args, { shell: c.shell, encoding: "utf8", timeout: 60000, windowsHide: true });
    const m = `${out.stdout || ""}${out.stderr || ""}`.match(/\d+\.\d+\.\d+\S*/);
    return m ? m[0] : null;
  }

  static installCommand() {
    const npm = state.which("npm");
    return npm ? [npm, "install", "-g", OpencodeManager.INSTALL_PACKAGE] : null;
  }

  /** Install opencode globally with npm. Returns the exit code. */
  static install() {
    const cmd = OpencodeManager.installCommand();
    if (!cmd) {
      throw new OpencodeNotInstalled(
        "npm was not found, so opencode cannot be installed automatically. Install Node.js from " +
          "https://nodejs.org and run `npm install -g @opencode/cli`, or follow https://opencode.ai/docs."
      );
    }
    const c = state.commandFor(cmd[0], cmd.slice(1));
    const out = spawnSync(c.file, c.args, { shell: c.shell, stdio: "inherit" });
    return out.status === null ? 1 : out.status;
  }

  /** The managed server's saved details if it is alive and answering, else null. */
  async running() {
    const saved = state.readJson(OpencodeManager.STATE);
    if (!saved || !state.pidAlive(saved.pid)) return null;
    const client = new OpencodeClient(saved.url, { username: this.username, password: saved.password || "" });
    try {
      const { reachable } = await client.health();
      return reachable ? saved : null;
    } catch {
      return null;
    }
  }

  /** Resolve with { client, started }. */
  async ensure(timeout = 90) {
    const saved = await this.running();
    if (saved) {
      return { client: new OpencodeClient(saved.url, { username: this.username, password: saved.password }), started: false };
    }
    let binary = this.findBinary();
    if (!binary) throw new OpencodeNotInstalled();
    binary = state.resolveWindowsShim(binary);
    const port = await state.freePort(this.port);
    const password = crypto.randomBytes(24).toString("base64url");
    const url = `http://127.0.0.1:${port}`;
    const env = { ...process.env, OPENCODE_SERVER_PASSWORD: password, OPENCODE_SERVER_USERNAME: this.username };
    const pid = state.spawnDetached(binary, ["serve", "--port", String(port), "--hostname", "127.0.0.1"], "opencode.log", {
      env,
      cwd: state.workspace(),
    });
    const client = new OpencodeClient(url, { username: this.username, password });
    const logFile = path.join(state.home(), "opencode.log");
    const deadline = Date.now() + timeout * 1000;
    while (Date.now() < deadline) {
      if (!state.pidAlive(pid)) {
        throw new ApiError(502, `opencode serve exited right after starting. See ${logFile}`, "upstream_unreachable");
      }
      let health = { reachable: false, version: null };
      try {
        health = await client.health();
      } catch {
        /* not ready */
      }
      if (health.reachable) {
        state.writeJson(OpencodeManager.STATE, {
          pid,
          url,
          port,
          password,
          version: health.version,
          started: Math.floor(Date.now() / 1000),
        });
        return { client, started: true };
      }
      await state.sleep(500);
    }
    state.killTree(pid);
    throw new ApiError(504, `opencode serve did not come up within ${timeout}s. See ${logFile}`, "upstream_timeout");
  }

  /** Stop the managed server. Returns true if one was running. */
  stop() {
    const saved = state.readJson(OpencodeManager.STATE);
    state.remove(OpencodeManager.STATE);
    if (saved && state.pidAlive(saved.pid)) {
      state.killTree(saved.pid);
      return true;
    }
    return false;
  }
}
OpencodeManager.STATE = "opencode.json";
OpencodeManager.INSTALL_PACKAGE = "@opencode/cli";

module.exports = { OpencodeClient, OpencodeManager, EMPTY_REPLY };
