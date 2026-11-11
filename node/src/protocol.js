"use strict";
/**
 * Translation between the OpenAI / Anthropic wire formats and one opencode
 * prompt, plus emulated tool calling.
 *
 * opencode does not pass a caller's tools to the model. So the tools are
 * described in the prompt, the model writes calls as <tool_call> JSON blocks,
 * and those blocks are turned into OpenAI tool_calls or Anthropic tool_use.
 * The caller runs the tools, never opencode.
 */
const crypto = require("node:crypto");
const { ApiError } = require("./errors");

const DEFAULT_SYSTEM =
  "You are a helpful, concise assistant. " + "Answer clearly and directly. If you do not know something, say so.";
const DEFAULT_NAMES = ["", "default", "auto"];

const TOOL_RULES = `# Tools

You can call the tools listed below. They are the only tools you have, and the
caller runs them for you. Ignore any other tool you may have been told about.

To call a tool, write a block in exactly this form:

<tool_call>
{"name": "tool_name", "arguments": {"parameter": "value"}}
</tool_call>

Rules:
- The block content must be one valid JSON object with "name" and "arguments".
- "arguments" must follow the tool's JSON Schema. Escape newlines inside strings as \\n.
- You may write several <tool_call> blocks to call several tools.
- After writing tool calls, stop. Do not guess or invent their results.
- Results come back to you inside <tool_result> blocks.
- If no tool is needed, just answer normally with no <tool_call> block.

Available tools:
`;

const CALL_RE = /<tool_call>\s*(\{[\s\S]*?\})\s*<\/tool_call>/g;
const OPEN_CALL_RE = /<tool_call>\s*(\{[\s\S]*\})\s*$/;
const LABEL_RE = /^\s*\[(Assistant|assistant)\]\s*/;
const ATTACHMENT = "[attachment omitted: this endpoint is text only]";

const hex = (n) => crypto.randomUUID().replace(/-/g, "").slice(0, n);

/**
 * JSON.parse that also accepts raw control characters inside strings, which
 * models often write (Python's json.loads(strict=False) does the same).
 */
function lenientParse(text) {
  try {
    return JSON.parse(text);
  } catch {
    /* repair below */
  }
  let out = "";
  let inString = false;
  let escaped = false;
  for (const ch of text) {
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
      else if (ch === "\n") {
        out += "\\n";
        continue;
      } else if (ch === "\r") {
        out += "\\r";
        continue;
      } else if (ch === "\t") {
        out += "\\t";
        continue;
      } else if (ch < " ") {
        out += "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0");
        continue;
      }
    } else if (ch === '"') inString = true;
    out += ch;
  }
  return JSON.parse(out);
}

// ------------------------------------------------------------------ tool calling

function toolsPrompt(tools) {
  const parts = [TOOL_RULES];
  for (const t of tools) {
    const schema = JSON.stringify(t.schema || { type: "object", properties: {} });
    parts.push(`## ${t.name}\n${String(t.description || "").trim()}\nJSON Schema for arguments: ${schema}\n`);
  }
  return parts.join("\n");
}

function renderCall(name, args) {
  return "<tool_call>\n" + JSON.stringify({ name, arguments: args }) + "\n</tool_call>";
}

function renderResult(name, callId, content) {
  return `<tool_result tool="${name || "unknown"}" id="${callId || ""}">\n${content}\n</tool_result>`;
}

/** Pull <tool_call> blocks out of the model's text. Returns { text, calls }. */
function parseToolCalls(text, toolNames) {
  const names = toolNames instanceof Set ? toolNames : new Set(toolNames);
  const calls = [];
  const take = (whole, json) => {
    let obj;
    try {
      obj = lenientParse(json);
    } catch {
      return whole;
    }
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) return whole;
    let args = "arguments" in obj ? obj.arguments : "input" in obj ? obj.input : "parameters" in obj ? obj.parameters : {};
    if (typeof args === "string") {
      try {
        args = lenientParse(args);
      } catch {
        return whole;
      }
    }
    if (!names.has(obj.name) || !args || typeof args !== "object" || Array.isArray(args)) return whole;
    calls.push({ name: obj.name, input: args });
    return "";
  };
  let remaining = text.replace(CALL_RE, take);
  remaining = remaining.replace(OPEN_CALL_RE, take); // the model forgot the closing tag
  return { text: remaining.trim(), calls };
}

/**
 * Both APIs are stateless: every request carries the whole conversation,
 * while opencode takes one prompt. So the history is folded into one prompt.
 * turns: [speaker, text][]. Returns { system, prompt }.
 */
function fold(system, turns, tools, force = null) {
  if (!turns.some(([who]) => who === "User")) {
    throw new ApiError(400, "'messages' needs at least one user message with text");
  }
  let sys = system || DEFAULT_SYSTEM;
  let reminder = "";
  if (tools.length) {
    sys = sys + "\n\n" + toolsPrompt(tools);
    reminder = "\n\nTo use a tool, reply with <tool_call> blocks exactly as specified in your instructions.";
    if (force === "any") reminder += " You must call at least one tool in this reply.";
    else if (force) reminder += ` You must call the tool '${force}' in this reply.`;
  }
  if (turns.length === 1) return { system: sys, prompt: turns[0][1] + reminder };
  const transcript = turns.map(([who, text]) => `[${who}]\n${text}`).join("\n\n");
  const prompt =
    "This is the conversation so far. Write the assistant's next reply only. " +
    "Do not repeat the transcript or add speaker labels.\n\n" +
    transcript +
    reminder;
  return { system: sys, prompt };
}

/** Clean the model's text and pull out tool calls. Returns { text, calls }. */
function finish(text, tools) {
  const cleaned = text.replace(LABEL_RE, "");
  if (!tools.length) return { text: cleaned, calls: [] };
  return parseToolCalls(cleaned, new Set(tools.map((t) => t.name)));
}

// ----------------------------------------------------------------------- OpenAI

/** Message content is a string or a list of parts. Only text is kept. */
function contentText(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    const out = [];
    for (const p of content) {
      if (typeof p === "string") out.push(p);
      else if (p && typeof p === "object") {
        if (["text", "input_text", "output_text"].includes(p.type)) out.push(String(p.text || ""));
        else if (["image", "image_url", "input_image", "document"].includes(p.type)) out.push(ATTACHMENT);
      }
    }
    return out.join("\n");
  }
  return content === null || content === undefined ? "" : String(content);
}

/** OpenAI chat request -> { system, turns, tools, force }. */
function openaiToCore(req) {
  const messages = req.messages;
  if (!Array.isArray(messages) || !messages.length) throw new ApiError(400, "'messages' must be a non-empty array");
  const systemParts = [];
  const turns = [];
  const callNames = {};
  for (const m of messages) {
    if (!m || typeof m !== "object" || Array.isArray(m)) throw new ApiError(400, "Each message must be an object");
    const role = m.role;
    const text = contentText(m.content).trim();
    if (role === "system" || role === "developer") {
      if (text) systemParts.push(text);
    } else if (role === "user") {
      if (text) turns.push(["User", text]);
    } else if (role === "assistant") {
      const pieces = text ? [text] : [];
      for (const call of m.tool_calls || []) {
        const fn = (call && call.function) || {};
        let args;
        try {
          args = JSON.parse(fn.arguments || "{}");
        } catch {
          args = { _raw: fn.arguments };
        }
        callNames[call.id] = fn.name;
        pieces.push(renderCall(fn.name, args));
      }
      if (pieces.length) turns.push(["Assistant", pieces.join("\n")]);
    } else if (role === "tool" || role === "function") {
      const callId = m.tool_call_id;
      turns.push(["Tool result", renderResult(callNames[callId] || m.name, callId, text)]);
    }
  }
  let tools = [];
  for (const t of req.tools || []) {
    const fn = t && typeof t === "object" ? t.function : null;
    if (fn && fn.name) tools.push({ name: fn.name, description: fn.description, schema: fn.parameters });
  }
  for (const fn of req.functions || []) {
    // legacy field
    if (fn && fn.name) tools.push({ name: fn.name, description: fn.description, schema: fn.parameters });
  }
  let force = null;
  const choice = req.tool_choice;
  if (choice === "none") tools = [];
  else if (choice === "required") force = "any";
  else if (choice && typeof choice === "object") force = (choice.function || {}).name || null;
  return { system: systemParts.join("\n\n"), turns, tools, force };
}

function openaiToolCalls(calls) {
  return calls.map((c) => ({
    id: "call_" + hex(24),
    type: "function",
    function: { name: c.name, arguments: JSON.stringify(c.input) },
  }));
}

function modelJson(id) {
  return { id, object: "model", created: 0, owned_by: id.split("/")[0] };
}

function chunkJson(id, created, model, delta, finishReason = null) {
  return {
    id,
    object: "chat.completion.chunk",
    created,
    model,
    choices: [{ index: 0, delta, finish_reason: finishReason, logprobs: null }],
  };
}

// -------------------------------------------------------------------- Anthropic

/** Anthropic messages request -> { system, turns, tools, force }. */
function anthropicToCore(req) {
  const messages = req.messages;
  if (!Array.isArray(messages) || !messages.length) throw new ApiError(400, "'messages' must be a non-empty array");
  const system = contentText(req.system).trim();
  const turns = [];
  const callNames = {};
  for (const m of messages) {
    if (!m || typeof m !== "object" || Array.isArray(m)) throw new ApiError(400, "Each message must be an object");
    let content = m.content;
    if (typeof content === "string") content = [{ type: "text", text: content }];
    if (!Array.isArray(content)) continue;
    if (m.role === "system") {
      // Claude Code can add these mid-conversation
      const text = contentText(content).trim();
      if (text) turns.push(["System note", text]);
      continue;
    }
    const speaker = m.role === "assistant" ? "Assistant" : "User";
    let buffer = [];
    const flush = () => {
      const text = buffer.filter(Boolean).join("\n").trim();
      if (text) turns.push([speaker, text]);
      buffer = [];
    };
    for (const block of content) {
      if (!block || typeof block !== "object") continue;
      if (block.type === "text") buffer.push(String(block.text || ""));
      else if (block.type === "tool_use") {
        callNames[block.id] = block.name;
        buffer.push(renderCall(block.name, block.input || {}));
      } else if (block.type === "tool_result") {
        flush();
        let body = contentText(block.content).trim() || "(no output)";
        if (block.is_error) body = "ERROR: " + body;
        turns.push(["Tool result", renderResult(callNames[block.tool_use_id], block.tool_use_id, body)]);
      } else if (block.type === "image" || block.type === "document") buffer.push(ATTACHMENT);
      // thinking and other block types are dropped
    }
    flush();
  }
  let tools = [];
  for (const t of req.tools || []) {
    if (t && t.name && t.input_schema && typeof t.input_schema === "object") {
      tools.push({ name: t.name, description: t.description, schema: t.input_schema });
    }
  }
  let force = null;
  const choice = req.tool_choice || {};
  if (choice && typeof choice === "object") {
    if (choice.type === "none") tools = [];
    else if (choice.type === "any") force = "any";
    else if (choice.type === "tool") force = choice.name || null;
  }
  return { system, turns, tools, force };
}

function anthropicContent(result) {
  const blocks = [];
  if (result.text) blocks.push({ type: "text", text: result.text });
  for (const c of result.calls) blocks.push({ type: "tool_use", id: "toolu_" + hex(24), name: c.name, input: c.input });
  return blocks;
}

function anthropicUsage(usage) {
  return { input_tokens: usage.prompt_tokens, output_tokens: usage.completion_tokens };
}

module.exports = {
  DEFAULT_SYSTEM,
  DEFAULT_NAMES,
  TOOL_RULES,
  hex,
  lenientParse,
  toolsPrompt,
  renderCall,
  renderResult,
  parseToolCalls,
  fold,
  finish,
  contentText,
  openaiToCore,
  openaiToolCalls,
  modelJson,
  chunkJson,
  anthropicToCore,
  anthropicContent,
  anthropicUsage,
};
