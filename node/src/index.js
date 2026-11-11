"use strict";
/**
 * relayai-gateway: OpenAI and Anthropic compatible API for the models in
 * opencode, with emulated tool calling, usable as a CLI, a server, or a Node SDK.
 *
 *   const { RelayAI } = require("relayai-gateway");
 *   const ai = await RelayAI.start();
 *   console.log(await ai.chat("Hello"));
 *   await ai.close();
 */
const { version } = require("./version");
const { Client } = require("./client");
const { configFromEnv, DEFAULTS } = require("./config");
const { ApiError, OpencodeNotInstalled, RelayAIError } = require("./errors");
const { Gateway } = require("./gateway");
const { OpencodeClient, OpencodeManager } = require("./opencode");
const { parseToolCalls } = require("./protocol");
const { RelayAI, runningGateway } = require("./sdk");

module.exports = {
  version,
  RelayAI,
  Client,
  Gateway,
  configFromEnv,
  DEFAULTS,
  OpencodeClient,
  OpencodeManager,
  ApiError,
  OpencodeNotInstalled,
  RelayAIError,
  runningGateway,
  parseToolCalls,
};
