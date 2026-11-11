"use strict";
/** RelayAI: the one-line way to use opencode's models from Node. */
const crypto = require("node:crypto");

const state = require("./state");
const { Client } = require("./client");
const { Gateway } = require("./gateway");

const GATEWAY_STATE = "gateway.json";

/** Details of the background gateway (`relayai start`) if it is up, else null. */
async function runningGateway() {
  const saved = state.readJson(GATEWAY_STATE);
  if (!saved || !state.pidAlive(saved.pid)) return null;
  const health = await new Client(saved.url, saved.api_key || "").health();
  return health.service === "RelayAI" ? saved : null;
}

/**
 * Everything in one object: finds or starts opencode serve, runs a private
 * gateway in this process, and gives you a client for it.
 *
 *   const { RelayAI } = require("relayai-gateway");
 *   const ai = await RelayAI.start();
 *   console.log(await ai.chat("Hello"));
 *   await ai.close();
 *
 * If a background gateway from `relayai start` is running it is reused (pass
 * reuse: false to always run a private one). Any setting from configFromEnv
 * can be passed, e.g. RelayAI.start({ model: "opencode/big-pickle", timeout: 120 }).
 *
 * For the openai / @anthropic-ai/sdk packages use ai.baseUrl, ai.anthropicBaseUrl and ai.apiKey.
 */
class RelayAI extends Client {
  static async start({ model, reuse = true, quiet = true, ...settings } = {}) {
    const saved = reuse && !settings.opencodeUrl ? await runningGateway() : null;
    if (saved) {
      const ai = new RelayAI(saved.url, saved.api_key || "", { model: model || "default" });
      ai.gateway = null;
      return ai;
    }
    const config = { port: 0, host: "127.0.0.1", ...settings };
    if (!config.apiKey) config.apiKey = process.env.RELAYAI_API_KEY || "rai-" + crypto.randomBytes(24).toString("base64url");
    if (model && !config.model) config.model = model;
    const gateway = await Gateway.create(config, { quiet });
    const ai = new RelayAI(gateway.url, gateway.config.apiKey, {
      model: model || "default",
      timeout: gateway.config.timeout + 30,
    });
    ai.gateway = gateway;
    return ai;
  }

  /** Stop the private gateway (and the opencode serve it started, if any). */
  async close() {
    if (this.gateway) {
      const gw = this.gateway;
      this.gateway = null;
      await gw.close();
    }
  }

}

// `await using ai = await RelayAI.start()` where the runtime supports it (Node 20.4+ / TS 5.2+)
if (Symbol.asyncDispose) {
  RelayAI.prototype[Symbol.asyncDispose] = function dispose() {
    return this.close();
  };
}

module.exports = { RelayAI, runningGateway, GATEWAY_STATE };
