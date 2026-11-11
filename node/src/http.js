"use strict";
/**
 * Minimal HTTP client on node:http. Used instead of fetch because fetch has a
 * fixed 300 second headers timeout, and model answers can take longer.
 */
const http = require("node:http");
const https = require("node:https");

class TimeoutError extends Error {
  constructor(ms) {
    super(`timed out after ${Math.round(ms / 1000)} seconds`);
    this.code = "ETIMEOUT";
  }
}

/** Open a request and resolve with the response stream (not read yet). */
function open(method, url, { body, headers = {}, timeout = 30000 } = {}) {
  return new Promise((resolve, reject) => {
    const target = new URL(url);
    const lib = target.protocol === "https:" ? https : http;
    const data = body === undefined || body === null ? null : Buffer.from(typeof body === "string" ? body : JSON.stringify(body));
    const h = { ...headers };
    if (data) h["Content-Length"] = data.length;
    const req = lib.request(target, { method, headers: h }, (res) => resolve(res));
    req.setTimeout(timeout, () => req.destroy(new TimeoutError(timeout)));
    req.on("error", reject);
    if (data) req.write(data);
    req.end();
  });
}

function readAll(res) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    res.on("data", (c) => chunks.push(c));
    res.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    res.on("error", reject);
  });
}

/** Resolve with { status, headers, text }. */
async function request(method, url, options = {}) {
  const res = await open(method, url, options);
  const text = await readAll(res);
  return { status: res.statusCode, headers: res.headers, text };
}

module.exports = { open, readAll, request, TimeoutError };
