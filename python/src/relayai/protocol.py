"""
Translation between the OpenAI / Anthropic wire formats and one opencode prompt,
plus emulated tool calling.

opencode does not pass a caller's tools to the model. So the tools are
described in the prompt, the model writes calls as <tool_call> JSON blocks,
and those blocks are turned into OpenAI tool_calls or Anthropic tool_use.
The caller runs the tools, never opencode.
"""
import json
import re
import uuid

from .errors import ApiError

DEFAULT_SYSTEM = (
    "You are a helpful, concise assistant. "
    "Answer clearly and directly. If you do not know something, say so."
)
DEFAULT_NAMES = ("", "default", "auto")

TOOL_RULES = """# Tools

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
"""

CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
OPEN_CALL_RE = re.compile(r"<tool_call>\s*(\{.*\})\s*$", re.S)
LABEL_RE = re.compile(r"^\s*\[(Assistant|assistant)\]\s*")
ATTACHMENT = "[attachment omitted: this endpoint is text only]"


# ------------------------------------------------------------------ tool calling

def tools_prompt(tools):
    parts = [TOOL_RULES]
    for t in tools:
        schema = json.dumps(t.get("schema") or {"type": "object", "properties": {}})
        parts.append(f"## {t['name']}\n{(t.get('description') or '').strip()}\nJSON Schema for arguments: {schema}\n")
    return "\n".join(parts)


def render_call(name, arguments):
    return "<tool_call>\n" + json.dumps({"name": name, "arguments": arguments}) + "\n</tool_call>"


def render_result(name, call_id, content):
    return f'<tool_result tool="{name or "unknown"}" id="{call_id or ""}">\n{content}\n</tool_result>'


def parse_tool_calls(text, tool_names):
    """Pull <tool_call> blocks out of the model's text. Returns (remaining_text, calls)."""
    calls = []

    def take(match):
        try:
            obj = json.loads(match.group(1), strict=False)
        except ValueError:
            return match.group(0)
        if not isinstance(obj, dict):
            return match.group(0)
        name = obj.get("name")
        args = obj.get("arguments", obj.get("input", obj.get("parameters", {})))
        if isinstance(args, str):
            try:
                args = json.loads(args, strict=False)
            except ValueError:
                return match.group(0)
        if name not in tool_names or not isinstance(args, dict):
            return match.group(0)
        calls.append({"name": name, "input": args})
        return ""

    remaining = CALL_RE.sub(take, text)
    remaining = OPEN_CALL_RE.sub(take, remaining)  # the model forgot the closing tag
    return remaining.strip(), calls


def fold(system, turns, tools, force=None):
    """
    Both APIs are stateless: every request carries the whole conversation,
    while opencode takes one prompt. So the history is folded into one prompt.
    turns: list of (speaker, text). Returns (system, prompt).
    """
    if not any(who == "User" for who, _ in turns):
        raise ApiError(400, "'messages' needs at least one user message with text")
    system = system or DEFAULT_SYSTEM
    reminder = ""
    if tools:
        system = system + "\n\n" + tools_prompt(tools)
        reminder = "\n\nTo use a tool, reply with <tool_call> blocks exactly as specified in your instructions."
        if force == "any":
            reminder += " You must call at least one tool in this reply."
        elif force:
            reminder += f" You must call the tool '{force}' in this reply."
    if len(turns) == 1:
        return system, turns[0][1] + reminder
    transcript = "\n\n".join(f"[{who}]\n{text}" for who, text in turns)
    prompt = ("This is the conversation so far. Write the assistant's next reply only. "
              "Do not repeat the transcript or add speaker labels.\n\n" + transcript + reminder)
    return system, prompt


def finish(text, tools):
    """Clean the model's text and pull out tool calls. Returns (text, calls)."""
    text = LABEL_RE.sub("", text)
    if not tools:
        return text, []
    return parse_tool_calls(text, {t["name"] for t in tools})


# ----------------------------------------------------------------------- OpenAI

def content_text(content):
    """Message content is a string or a list of parts. Only text is kept."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict):
                if p.get("type") in ("text", "input_text", "output_text"):
                    out.append(str(p.get("text") or ""))
                elif p.get("type") in ("image", "image_url", "input_image", "document"):
                    out.append(ATTACHMENT)
        return "\n".join(out)
    return "" if content is None else str(content)


def openai_to_core(req):
    """OpenAI chat request -> (system, turns, tools, force)."""
    messages = req.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ApiError(400, "'messages' must be a non-empty array")

    system_parts, turns, call_names = [], [], {}
    for m in messages:
        if not isinstance(m, dict):
            raise ApiError(400, "Each message must be an object")
        role = m.get("role")
        text = content_text(m.get("content")).strip()
        if role in ("system", "developer"):
            if text:
                system_parts.append(text)
        elif role == "user":
            if text:
                turns.append(("User", text))
        elif role == "assistant":
            pieces = [text] if text else []
            for call in m.get("tool_calls") or []:
                fn = (call or {}).get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {"_raw": fn.get("arguments")}
                call_names[call.get("id")] = fn.get("name")
                pieces.append(render_call(fn.get("name"), args))
            if pieces:
                turns.append(("Assistant", "\n".join(pieces)))
        elif role in ("tool", "function"):
            call_id = m.get("tool_call_id")
            turns.append(("Tool result", render_result(call_names.get(call_id) or m.get("name"), call_id, text)))

    tools = []
    for t in req.get("tools") or []:
        fn = (t or {}).get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            tools.append({"name": fn["name"], "description": fn.get("description"), "schema": fn.get("parameters")})
    for fn in req.get("functions") or []:  # legacy field
        if isinstance(fn, dict) and fn.get("name"):
            tools.append({"name": fn["name"], "description": fn.get("description"), "schema": fn.get("parameters")})

    force = None
    choice = req.get("tool_choice")
    if choice == "none":
        tools = []
    elif choice == "required":
        force = "any"
    elif isinstance(choice, dict):
        force = (choice.get("function") or {}).get("name")
    return "\n\n".join(system_parts), turns, tools, force


def openai_tool_calls(calls):
    return [{"id": "call_" + uuid.uuid4().hex[:24], "type": "function",
             "function": {"name": c["name"], "arguments": json.dumps(c["input"])}} for c in calls]


def model_json(model_id):
    return {"id": model_id, "object": "model", "created": 0, "owned_by": model_id.split("/", 1)[0]}


def chunk_json(completion_id, created, model, delta, finish_reason=None):
    return {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason, "logprobs": None}]}


# -------------------------------------------------------------------- Anthropic

def anthropic_to_core(req):
    """Anthropic messages request -> (system, turns, tools, force)."""
    messages = req.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ApiError(400, "'messages' must be a non-empty array")

    system = content_text(req.get("system")).strip()
    turns, call_names = [], {}
    for m in messages:
        if not isinstance(m, dict):
            raise ApiError(400, "Each message must be an object")
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            continue
        if role == "system":  # Claude Code can add these mid-conversation
            text = content_text(content).strip()
            if text:
                turns.append(("System note", text))
            continue
        speaker = "Assistant" if role == "assistant" else "User"
        buffer = []

        def flush():
            text = "\n".join(b for b in buffer if b).strip()
            if text:
                turns.append((speaker, text))
            buffer.clear()

        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                buffer.append(str(block.get("text") or ""))
            elif kind == "tool_use":
                call_names[block.get("id")] = block.get("name")
                buffer.append(render_call(block.get("name"), block.get("input") or {}))
            elif kind == "tool_result":
                flush()
                body = content_text(block.get("content")).strip() or "(no output)"
                if block.get("is_error"):
                    body = "ERROR: " + body
                call_id = block.get("tool_use_id")
                turns.append(("Tool result", render_result(call_names.get(call_id), call_id, body)))
            elif kind in ("image", "document"):
                buffer.append(ATTACHMENT)
            # thinking and other block types are dropped
        flush()

    tools = []
    for t in req.get("tools") or []:
        if isinstance(t, dict) and t.get("name") and isinstance(t.get("input_schema"), dict):
            tools.append({"name": t["name"], "description": t.get("description"), "schema": t["input_schema"]})

    force = None
    choice = req.get("tool_choice") or {}
    if isinstance(choice, dict):
        if choice.get("type") == "none":
            tools = []
        elif choice.get("type") == "any":
            force = "any"
        elif choice.get("type") == "tool":
            force = choice.get("name")
    return system, turns, tools, force


def anthropic_content(result):
    blocks = []
    if result["text"]:
        blocks.append({"type": "text", "text": result["text"]})
    for c in result["calls"]:
        blocks.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:24], "name": c["name"], "input": c["input"]})
    return blocks


def anthropic_usage(usage):
    return {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"]}
