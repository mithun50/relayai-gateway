"use strict";
/** Gateway settings. Precedence: explicit options, then RELAYAI_* / OPENCODE_* variables, then defaults. */

const DEFAULTS = Object.freeze({
  host: "127.0.0.1",
  port: 8000,
  apiKey: "",
  model: "",
  strictModels: true,
  timeout: 600,
  maxConcurrency: 4,
  queueWait: 30,
  modelCacheTtl: 30,
  keepalive: 10,
  opencodeUrl: "",
  opencodeUsername: "opencode",
  opencodePassword: "",
  opencodePort: 4096,
  opencodeBin: "",
});

function int(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return fallback;
  const n = Number.parseInt(raw, 10);
  return Number.isNaN(n) ? fallback : n;
}

function bool(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined) return fallback;
  return !["0", "false", "no", "off", ""].includes(raw.trim().toLowerCase());
}

/**
 * Settings from the environment, with every defined key of `overrides` on top.
 * @param {Partial<typeof DEFAULTS>} overrides
 */
function configFromEnv(overrides = {}) {
  const env = process.env;
  const config = {
    host: env.RELAYAI_HOST || DEFAULTS.host,
    port: int("RELAYAI_PORT", DEFAULTS.port),
    apiKey: (env.RELAYAI_API_KEY || "").trim(),
    model: (env.RELAYAI_MODEL || "").trim(),
    strictModels: bool("RELAYAI_STRICT_MODELS", DEFAULTS.strictModels),
    timeout: int("RELAYAI_TIMEOUT", DEFAULTS.timeout),
    maxConcurrency: Math.max(1, int("RELAYAI_MAX_CONCURRENCY", DEFAULTS.maxConcurrency)),
    queueWait: int("RELAYAI_QUEUE_WAIT", DEFAULTS.queueWait),
    modelCacheTtl: int("RELAYAI_MODEL_CACHE", DEFAULTS.modelCacheTtl),
    keepalive: int("RELAYAI_KEEPALIVE", DEFAULTS.keepalive),
    opencodeUrl: (env.OPENCODE_URL || "").trim(),
    opencodeUsername: env.OPENCODE_SERVER_USERNAME || DEFAULTS.opencodeUsername,
    opencodePassword: env.OPENCODE_SERVER_PASSWORD || "",
    opencodePort: int("RELAYAI_OPENCODE_PORT", DEFAULTS.opencodePort),
    opencodeBin: (env.RELAYAI_OPENCODE_BIN || "").trim(),
  };
  for (const [key, value] of Object.entries(overrides)) {
    if (!(key in DEFAULTS)) throw new TypeError(`unknown setting: ${key}`);
    if (value !== undefined && value !== null) config[key] = value;
  }
  config.opencodeUrl = (config.opencodeUrl || "").replace(/\/+$/, "");
  return config;
}

module.exports = { DEFAULTS, configFromEnv };
