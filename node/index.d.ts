// Type definitions for relayai-gateway

export declare const version: string;

/** Gateway settings. Every field is optional when passed to create()/start(). */
export interface Config {
  /** Interface the gateway listens on. 0.0.0.0 exposes it on the network. Default 127.0.0.1. */
  host: string;
  /** Gateway port. 0 picks any free port. Default 8000. */
  port: number;
  /** Key clients must send. */
  apiKey: string;
  /** Model used for "default", as provider/model. Empty: opencode's own default. */
  model: string;
  /** Reject unknown model names on the OpenAI endpoint (Anthropic always falls back). Default true. */
  strictModels: boolean;
  /** Seconds to wait for one answer. Default 600. */
  timeout: number;
  /** Answers generated at the same time. Default 4. */
  maxConcurrency: number;
  /** Seconds a request may wait for a free slot before a 429. Default 30. */
  queueWait: number;
  /** Seconds the model list is cached. Default 30. */
  modelCacheTtl: number;
  /** Seconds between keepalive pings on a streaming response. Default 10. */
  keepalive: number;
  /** URL of an opencode serve you run yourself. Empty: relayai starts and manages one. */
  opencodeUrl: string;
  opencodeUsername: string;
  /** Password of the opencode serve at opencodeUrl (opencode 2.x always needs one). */
  opencodePassword: string;
  /** Preferred port for the opencode serve relayai manages. Default 4096. */
  opencodePort: number;
  /** Path to the opencode executable. Empty: found on PATH. */
  opencodeBin: string;
}

export declare const DEFAULTS: Readonly<Config>;
/** Settings from RELAYAI_* / OPENCODE_* variables, with `overrides` on top. */
export declare function configFromEnv(overrides?: Partial<Config>): Config;

// ------------------------------------------------------------------ errors

export declare class RelayAIError extends Error {}

/** An error with an HTTP status. Thrown by the client for every non-2xx response. */
export declare class ApiError extends RelayAIError {
  constructor(status: number, message: string, kind?: string, code?: string | null);
  status: number;
  kind: string;
  code: string | null;
  body(): { error: { message: string; type: string; param: null; code: string | null } };
  anthropicBody(): { type: "error"; error: { type: string; message: string } };
}

export declare class OpencodeNotInstalled extends RelayAIError {}

// ------------------------------------------------------------------ messages

export interface ChatMessage {
  role: "system" | "developer" | "user" | "assistant" | "tool";
  content: string | null | Array<{ type: string; text?: string; [key: string]: unknown }>;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
  name?: string;
}

export interface ToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

export interface ToolDefinition {
  type: "function";
  function: { name: string; description?: string; parameters?: Record<string, unknown> };
}

export interface ChatCompletion {
  id: string;
  object: "chat.completion";
  created: number;
  model: string;
  choices: Array<{
    index: number;
    message: { role: "assistant"; content: string | null; tool_calls?: ToolCall[] };
    finish_reason: "stop" | "tool_calls";
    logprobs: null;
  }>;
  usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
}

export interface ChatOptions {
  model?: string;
  system?: string;
  tools?: ToolDefinition[];
  tool_choice?: "auto" | "none" | "required" | { type: "function"; function: { name: string } };
  [key: string]: unknown;
}

export interface Health {
  status: "ok" | "degraded" | "down";
  service?: "RelayAI";
  version?: string;
  opencode?: { url: string; reachable: boolean; version: string | null };
  error?: string;
}

// ------------------------------------------------------------------ client

/** Talks to a running gateway (or any OpenAI-compatible server). */
export declare class Client {
  /** @param baseUrl gateway URL, with or without /v1. timeout is in seconds. */
  constructor(baseUrl?: string, apiKey?: string, options?: { model?: string; timeout?: number });
  url: string;
  apiKey: string;
  model: string;
  timeout: number;
  /** OpenAI base URL (ends in /v1), for the openai package. */
  readonly baseUrl: string;
  /** Anthropic base URL, for @anthropic-ai/sdk and Claude Code. */
  readonly anthropicBaseUrl: string;
  health(): Promise<Health>;
  /** Model ids opencode can use right now, as provider/model. */
  models(): Promise<string[]>;
  /** Raw POST /v1/chat/completions. */
  complete(body: Record<string, unknown>): Promise<ChatCompletion>;
  /** Raw POST /v1/messages (Anthropic format). */
  messages(body: Record<string, unknown>): Promise<any>;
  /** Ask something and get the answer text. */
  chat(prompt: string | ChatMessage[], options?: ChatOptions): Promise<string>;
  /** Like chat(), but yields the answer in pieces. */
  stream(prompt: string | ChatMessage[], options?: ChatOptions): AsyncGenerator<string, void, unknown>;
}

/**
 * Everything in one object: finds or starts opencode serve, runs a private
 * gateway in this process (or reuses `relayai start`), and is a Client for it.
 */
export declare class RelayAI extends Client {
  static start(options?: Partial<Config> & { reuse?: boolean; quiet?: boolean }): Promise<RelayAI>;
  /** The in-process gateway, or null when a background gateway is reused. */
  gateway: Gateway | null;
  /** Stop the private gateway (and the opencode serve it started, if any). */
  close(): Promise<void>;
  [Symbol.asyncDispose](): Promise<void>;
}

// ------------------------------------------------------------------ server

export declare class Gateway {
  static create(
    settings?: Partial<Config>,
    options?: { opencode?: OpencodeClient; stopOpencode?: boolean; quiet?: boolean }
  ): Promise<Gateway>;
  config: Config;
  opencode: OpencodeClient;
  manager: OpencodeManager | null;
  /** True when this gateway started opencode serve itself. */
  startedOpencode: boolean;
  readonly port: number;
  readonly url: string;
  /** OpenAI base URL (ends in /v1). */
  readonly baseUrl: string;
  close(): Promise<void>;
  [Symbol.asyncDispose](): Promise<void>;
}

export interface OpencodeModel {
  providerID: string;
  modelID: string;
}

/** A client for opencode's own HTTP API (1.x and 2.x). */
export declare class OpencodeClient {
  constructor(url: string, options?: { username?: string; password?: string; timeout?: number });
  url: string;
  timeout: number;
  apiVersion(): Promise<"v1" | "v2">;
  health(): Promise<{ reachable: boolean; version: string | null }>;
  modelIds(): Promise<string[]>;
  generate(
    system: string,
    prompt: string,
    model?: OpencodeModel | null,
    box?: { session?: string; cancelled?: boolean }
  ): Promise<{ text: string; usage: ChatCompletion["usage"]; used: string | null }>;
  cancel(sessionId: string): Promise<void>;
}

/** Finds, installs, starts and stops `opencode serve`. */
export declare class OpencodeManager {
  constructor(options?: { port?: number; binary?: string; username?: string });
  static readonly STATE: string;
  static readonly INSTALL_PACKAGE: string;
  findBinary(): string | null;
  version(): string | null;
  static installCommand(): string[] | null;
  static install(): number;
  running(): Promise<{ pid: number; url: string; port: number; version: string | null } | null>;
  ensure(timeout?: number): Promise<{ client: OpencodeClient; started: boolean }>;
  stop(): boolean;
}

/** Details of the background gateway (`relayai start`) if it is up, else null. */
export declare function runningGateway(): Promise<{
  pid: number;
  url: string;
  port: number;
  api_key: string;
  version: string;
} | null>;

/** Pull <tool_call> blocks out of model text. */
export declare function parseToolCalls(
  text: string,
  toolNames: Iterable<string>
): { text: string; calls: Array<{ name: string; input: Record<string, unknown> }> };
