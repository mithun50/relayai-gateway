"use strict";
/** The `relayai` command line tool (same commands and flags as the Python package). */
const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");
const { spawn } = require("node:child_process");

const state = require("./state");
const { version } = require("./version");
const { Client } = require("./client");
const { configFromEnv } = require("./config");
const { ApiError, OpencodeNotInstalled, RelayAIError } = require("./errors");
const { Gateway, log } = require("./gateway");
const { OpencodeClient, OpencodeManager } = require("./opencode");
const { RelayAI, runningGateway, GATEWAY_STATE } = require("./sdk");

const COMMANDS = {
  serve: "run the gateway in this terminal",
  start: "run the gateway in the background",
  stop: "stop the background gateway and the managed opencode serve",
  status: "show what is running",
  models: "list the models opencode can use",
  chat: "ask a question, or chat interactively",
  env: "print variables for OpenAI / Anthropic clients",
  claude: "run Claude Code through the gateway",
  doctor: "check the setup",
  "install-opencode": "install opencode with npm (npm install -g @opencode/cli)",
};

const HELP = `usage: relayai <command> [options]

OpenAI and Anthropic compatible API for the models in opencode, with tool calling.

commands:
${Object.entries(COMMANDS)
  .map(([c, d]) => `  ${c.padEnd(18)}${d}`)
  .join("\n")}

gateway and opencode options (also settable with RELAYAI_* variables):
  --host HOST              interface to listen on (default 127.0.0.1; 0.0.0.0 exposes it)
  --port PORT              gateway port (default 8000)
  --api-key KEY            key clients must send (default: stored in ~/.relayai/api_key)
  -m, --model MODEL        model as provider/model (default: opencode's default)
  --opencode-url URL       use an opencode serve you run yourself instead of a managed one
  --opencode-password PW   password of that opencode serve
  --opencode-port PORT     preferred port for the managed opencode serve (default 4096)
  --opencode-bin PATH      path to the opencode executable
  --timeout SECONDS        seconds to wait for one answer (default 600)
  --max-concurrency N      answers generated at once (default 4)
  --no-strict-models       unknown model names fall back to the default
  -v, --verbose            more logging

command options:
  serve   --keep-opencode       leave the managed opencode serve running on exit
  start   --wait SECONDS        seconds to wait for it to come up (default 120)
  stop    --keep-opencode       leave opencode serve running
  status  --json                machine readable output
  models  --json                machine readable output
  chat    [PROMPT...]           the question (omit for an interactive chat; stdin works too)
          -s, --system TEXT     system prompt
          --no-stream           print the answer in one piece
  env     --shell bash|powershell|cmd|fish
  claude  [-- CLAUDE ARGS]      arguments after -- go to claude
  doctor  --test                also send one real request through opencode
  install-opencode  -y, --yes   do not ask;  --force  install even if present

examples:
  relayai serve                       run the gateway in this terminal
  relayai start                       run it in the background (relayai stop to end)
  relayai chat "Explain DNS in one line"
  relayai chat                        interactive chat
  relayai models                      list the models opencode can use
  relayai env                         print the variables for OpenAI / Anthropic clients
  relayai claude                      run Claude Code through the gateway
  relayai doctor                      check the setup

docs: https://github.com/mithun50/relayai-gateway
`;

// ---------------------------------------------------------------- arguments

const VALUE_FLAGS = {
  "--host": ["host"],
  "--port": ["port", "int"],
  "--api-key": ["apiKey"],
  "-m": ["model"],
  "--model": ["model"],
  "--opencode-url": ["opencodeUrl"],
  "--opencode-password": ["opencodePassword"],
  "--opencode-port": ["opencodePort", "int"],
  "--opencode-bin": ["opencodeBin"],
  "--timeout": ["timeout", "int"],
  "--max-concurrency": ["maxConcurrency", "int"],
  "-s": ["system"],
  "--system": ["system"],
  "--shell": ["shell"],
  "--wait": ["wait", "int"],
};
const BOOL_FLAGS = {
  "--no-strict-models": "noStrictModels",
  "-v": "verbose",
  "--verbose": "verbose",
  "--json": "json",
  "--no-stream": "noStream",
  "--keep-opencode": "keepOpencode",
  "--test": "test",
  "-y": "yes",
  "--yes": "yes",
  "--force": "force",
  "-h": "help",
  "--help": "help",
};

class UsageError extends Error {}

function parse(argv) {
  const args = { positional: [], passthrough: [] };
  for (let i = 0; i < argv.length; i++) {
    let a = argv[i];
    if (a === "--") {
      args.passthrough = argv.slice(i + 1);
      break;
    }
    let inline = null;
    const eq = a.indexOf("=");
    if (a.startsWith("--") && eq > 0) {
      inline = a.slice(eq + 1);
      a = a.slice(0, eq);
    }
    if (VALUE_FLAGS[a]) {
      const [key, type] = VALUE_FLAGS[a];
      const raw = inline !== null ? inline : argv[++i];
      if (raw === undefined) throw new UsageError(`${a} needs a value`);
      const value = type === "int" ? Number.parseInt(raw, 10) : raw;
      if (type === "int" && Number.isNaN(value)) throw new UsageError(`${a} needs a number`);
      args[key] = value;
    } else if (BOOL_FLAGS[a]) args[BOOL_FLAGS[a]] = true;
    else if (a.startsWith("-") && a !== "-" && !args.command) throw new UsageError(`unknown option ${a}`);
    else if (!args.command) args.command = a;
    else args.positional.push(argv[i]);
  }
  return args;
}

function settings(args) {
  return {
    host: args.host,
    port: args.port,
    apiKey: args.apiKey,
    model: args.model,
    opencodeUrl: args.opencodeUrl,
    opencodePassword: args.opencodePassword,
    opencodePort: args.opencodePort,
    opencodeBin: args.opencodeBin,
    timeout: args.timeout,
    maxConcurrency: args.maxConcurrency,
    strictModels: args.noStrictModels ? false : undefined,
  };
}

function forwardFlags(args) {
  const out = [];
  const pairs = [
    ["--host", args.host],
    ["--port", args.port],
    ["--api-key", args.apiKey],
    ["--model", args.model],
    ["--opencode-url", args.opencodeUrl],
    ["--opencode-password", args.opencodePassword],
    ["--opencode-port", args.opencodePort],
    ["--opencode-bin", args.opencodeBin],
    ["--timeout", args.timeout],
    ["--max-concurrency", args.maxConcurrency],
  ];
  for (const [flag, value] of pairs) if (value !== undefined && value !== null) out.push(flag, String(value));
  if (args.noStrictModels) out.push("--no-strict-models");
  return out;
}

/**
 * Write to stdout/stderr synchronously. process.stdout.write is asynchronous
 * when stdout is a pipe on Linux, and output still in flight is lost when the
 * process is stopped (seen with `relayai serve` under CI, and it would bite
 * systemd, Docker and log collectors too). A direct fd write cannot be lost.
 */
function writeSync(fd, text) {
  const buf = Buffer.from(text);
  let offset = 0;
  while (offset < buf.length) {
    try {
      offset += fs.writeSync(fd, buf, offset);
    } catch (e) {
      if (e.code !== "EAGAIN") {
        (fd === 1 ? process.stdout : process.stderr).write(buf.subarray(offset)); // last resort
        return;
      }
    }
  }
}
const out = (s = "") => writeSync(1, s + "\n");
const err = (s) => writeSync(2, `relayai: ${s}\n`);

/** Run fn with a client for the background gateway, or a short-lived private gateway. */
async function withClient(args, fn, quiet = false) {
  const saved = args.opencodeUrl ? null : await runningGateway();
  if (saved) return fn(new Client(saved.url, saved.api_key, { model: args.model || "default" }));
  if (!quiet) {
    process.stderr.write("(starting a private gateway; `relayai start` keeps one running and makes this instant)\n");
  }
  const s = settings(args);
  delete s.port;
  delete s.host;
  const ai = await RelayAI.start({ reuse: false, ...s });
  try {
    return await fn(ai);
  } finally {
    await ai.close();
  }
}

// ------------------------------------------------------------------ commands

async function cmdServe(args) {
  const config = configFromEnv(settings(args));
  if (!config.apiKey) config.apiKey = state.loadOrCreateApiKey();
  let gw;
  try {
    gw = await Gateway.create(config, { stopOpencode: !args.keepOpencode });
  } catch (e) {
    if (e instanceof OpencodeNotInstalled) {
      err(e.message);
      return 1;
    }
    if (e && e.code === "EADDRINUSE") {
      err(`cannot listen on ${config.host}:${config.port} (port in use). Use --port to pick another port.`);
      return 1;
    }
    throw e;
  }
  state.writeJson(GATEWAY_STATE, {
    pid: process.pid,
    url: gw.url,
    port: gw.port,
    api_key: config.apiKey,
    version,
    started: Math.floor(Date.now() / 1000),
  });
  let health = { reachable: false, version: null };
  let count = 0;
  try {
    health = await gw.opencode.health();
    count = (await gw.catalog.ids()).length;
  } catch (e) {
    log.warn(e.message);
  }
  out(`RelayAI ${version}  (OpenAI and Anthropic compatible gateway for opencode)`);
  out(`  OpenAI base URL    : ${gw.baseUrl}`);
  out(`  Anthropic base URL : ${gw.url}`);
  out(`  API key            : ${config.apiKey}`);
  out(`  Default model      : ${config.model || "chosen by opencode"}`);
  out(
    `  opencode           : ${gw.opencode.url}` +
      (health.reachable ? ` (version ${health.version}, ${count} models` : " (not reachable yet") +
      (gw.startedOpencode ? ", started by relayai)" : ")")
  );
  out("Press Ctrl+C to stop.");

  return new Promise((resolve) => {
    let stopping = false;
    const stop = async () => {
      if (stopping) return;
      stopping = true;
      await gw.close();
      const saved = state.readJson(GATEWAY_STATE);
      if (saved && saved.pid === process.pid) state.remove(GATEWAY_STATE);
      out("\nRelayAI stopped.");
      resolve(0);
      // Everything is closed and written; do not wait for idle keep-alive sockets to time out.
      setTimeout(() => process.exit(0), 200).unref();
    };
    process.on("SIGINT", stop);
    process.on("SIGTERM", stop);
    process.on("SIGHUP", stop);
  });
}

async function cmdStart(args) {
  const saved = await runningGateway();
  if (saved) {
    out(`RelayAI is already running at ${saved.url} (pid ${saved.pid}).`);
    return 0;
  }
  if (!args.opencodeUrl && !new OpencodeManager({ binary: args.opencodeBin || "" }).findBinary()) {
    err(new OpencodeNotInstalled().message);
    return 1;
  }
  state.remove(GATEWAY_STATE);
  const script = path.join(__dirname, "..", "bin", "relayai.js");
  const pid = state.spawnDetached(process.execPath, [script, "serve", ...forwardFlags(args)], "gateway.log");
  const deadline = Date.now() + (args.wait || 120) * 1000;
  while (Date.now() < deadline) {
    const up = await runningGateway();
    if (up) {
      out(`RelayAI started in the background (pid ${up.pid}).`);
      out(`  OpenAI base URL    : ${up.url}/v1`);
      out(`  Anthropic base URL : ${up.url}`);
      out(`  API key            : ${up.api_key}`);
      out("Stop it with `relayai stop`. Logs: " + path.join(state.home(), "gateway.log"));
      return 0;
    }
    if (!state.pidAlive(pid) && !state.readJson(GATEWAY_STATE)) break;
    await state.sleep(500);
  }
  err(`the gateway did not start. See ${path.join(state.home(), "gateway.log")}`);
  return 1;
}

async function cmdStop(args) {
  const saved = state.readJson(GATEWAY_STATE);
  let stopped = false;
  if (saved && state.pidAlive(saved.pid)) {
    state.killTree(saved.pid, false); // opencode is handled on its own below
    stopped = true;
  }
  state.remove(GATEWAY_STATE);
  const opencodeStopped = args.keepOpencode ? false : new OpencodeManager().stop();
  out((stopped ? "Stopped the gateway." : "No gateway was running.") + (opencodeStopped ? " Stopped opencode serve." : ""));
  return 0;
}

async function cmdStatus(args) {
  const gateway = await runningGateway();
  const opencode = await new OpencodeManager().running();
  if (args.json) {
    let oc = null;
    if (opencode) {
      oc = { ...opencode };
      delete oc.password;
    }
    out(JSON.stringify({ gateway, opencode: oc }, null, 2));
    return gateway ? 0 : 1;
  }
  if (gateway) {
    out(`gateway   running  ${gateway.url}  (pid ${gateway.pid})`);
    out(`          OpenAI base URL ${gateway.url}/v1, API key ${gateway.api_key}`);
  } else out("gateway   not running  (start it with `relayai start`)");
  if (opencode) out(`opencode  running  ${opencode.url}  (pid ${opencode.pid}, version ${opencode.version})`);
  else out("opencode  not running under relayai");
  return gateway ? 0 : 1;
}

async function cmdModels(args) {
  const ids = await withClient(args, (c) => c.models(), args.json);
  if (args.json) out(JSON.stringify(ids, null, 2));
  else out(ids.length ? ids.join("\n") : "(opencode lists no models; connect a provider in opencode first)");
  return 0;
}

function readStdin() {
  return new Promise((resolve) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (c) => (data += c));
    process.stdin.on("end", () => resolve(data));
  });
}

async function answer(client, messages, args) {
  try {
    if (args.noStream) {
      const text = await client.chat(messages, { model: args.model, system: args.system });
      out(text);
      return text;
    }
    let text = "";
    for await (const piece of client.stream(messages, { model: args.model, system: args.system })) {
      text += piece;
      process.stdout.write(piece);
    }
    out();
    return text;
  } catch (e) {
    if (!(e instanceof ApiError)) throw e;
    out();
    err(e.message);
    return null;
  }
}

async function cmdChat(args) {
  let prompt = args.positional.join(" ").trim();
  if (!prompt && !process.stdin.isTTY) prompt = (await readStdin()).trim();
  return withClient(args, async (client) => {
    if (prompt) {
      return (await answer(client, [{ role: "user", content: prompt }], args)) === null ? 1 : 0;
    }
    out("RelayAI chat. Empty line or Ctrl+C to quit, /reset to forget the conversation.");
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    const ask = () => new Promise((resolve) => rl.question("\nyou> ", resolve));
    rl.on("SIGINT", () => rl.close());
    let closed = false;
    rl.on("close", () => (closed = true));
    let history = [];
    while (!closed) {
      const line = (await Promise.race([ask(), new Promise((r) => rl.once("close", () => r("")))])).trim();
      if (!line) break;
      if (line === "/reset") {
        history = [];
        out("(conversation cleared)");
        continue;
      }
      history.push({ role: "user", content: line });
      process.stdout.write("ai> ");
      const reply = await answer(client, history, args);
      if (reply === null) history.pop();
      else history.push({ role: "assistant", content: reply });
    }
    rl.close();
    return 0;
  });
}

async function cmdEnv(args) {
  const saved = await runningGateway();
  if (!saved) {
    err("no gateway is running. Start one with `relayai start`.");
    return 1;
  }
  const { url, api_key: key } = saved;
  const pairs = [
    ["OPENAI_BASE_URL", url + "/v1"],
    ["OPENAI_API_KEY", key],
    ["ANTHROPIC_BASE_URL", url],
    ["ANTHROPIC_AUTH_TOKEN", key],
    ["ANTHROPIC_MODEL", args.model || "default"],
  ];
  const shell = args.shell || (state.IS_WIN ? "powershell" : "bash");
  for (const [name, value] of pairs) {
    if (shell === "powershell") out(`$env:${name} = "${value}"`);
    else if (shell === "cmd") out(`set ${name}=${value}`);
    else if (shell === "fish") out(`set -gx ${name} ${value}`);
    else out(`export ${name}=${value}`);
  }
  return 0;
}

async function cmdClaude(args) {
  const claude = state.which("claude");
  if (!claude) {
    err("Claude Code (`claude`) was not found on PATH. Install it from https://claude.com/claude-code");
    return 1;
  }
  const passthrough = [...args.positional, ...args.passthrough];
  return withClient(args, async (client) => {
    const model = args.model || "default";
    const env = { ...process.env };
    delete env.ANTHROPIC_API_KEY;
    Object.assign(env, {
      ANTHROPIC_BASE_URL: client.anthropicBaseUrl,
      ANTHROPIC_AUTH_TOKEN: client.apiKey,
      ANTHROPIC_MODEL: model,
      ANTHROPIC_DEFAULT_HAIKU_MODEL: model,
      ANTHROPIC_DEFAULT_SONNET_MODEL: model,
      ANTHROPIC_DEFAULT_OPUS_MODEL: model,
      CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS: "1",
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
    });
    process.stderr.write(`Claude Code -> RelayAI at ${client.anthropicBaseUrl} (model: ${model})\n`);
    // Async spawn, not spawnSync: the private gateway runs in this process and must keep serving.
    const c = state.commandFor(claude, passthrough);
    return new Promise((resolve) => {
      const child = spawn(c.file, c.args, { shell: c.shell, stdio: "inherit", env });
      child.on("exit", (code) => resolve(code === null ? 1 : code));
      child.on("error", (e) => {
        err(e.message);
        resolve(1);
      });
    });
  });
}

async function cmdDoctor(args) {
  let ok = true;
  const line = (good, label, detail = "") => {
    if (good === false) ok = false;
    const mark = good === true ? "ok  " : good === false ? "FAIL" : "info";
    out(`[${mark}] ${label}` + (detail ? `: ${detail}` : ""));
  };
  line(true, "relayai", `${version} (Node ${process.versions.node})`);
  line(true, "home folder", state.home());
  const manager = new OpencodeManager({ binary: args.opencodeBin || "" });
  const binary = manager.findBinary();
  if (args.opencodeUrl) line(null, "opencode binary", "not needed, using --opencode-url");
  else if (binary) line(true, "opencode binary", `${binary} (version ${manager.version() || "unknown"})`);
  else line(false, "opencode binary", "not found. Run `relayai install-opencode`");
  line(null, "npm", state.which("npm") || "not found (only needed to install opencode)");
  line(null, "Claude Code", state.which("claude") || "not found (only needed for `relayai claude`)");

  let client = null;
  if (args.opencodeUrl) {
    client = new OpencodeClient(args.opencodeUrl, {
      password: args.opencodePassword || process.env.OPENCODE_SERVER_PASSWORD || "",
    });
  } else {
    const running = await manager.running();
    line(null, "managed opencode serve", running ? `running at ${running.url}` : "not running");
    if (running) client = new OpencodeClient(running.url, { password: running.password || "" });
  }
  const gateway = await runningGateway();
  line(null, "background gateway", gateway ? `running at ${gateway.url}` : "not running");

  if (client) {
    try {
      const health = await client.health();
      line(
        health.reachable,
        "opencode API",
        health.reachable
          ? `${await client.apiVersion()} at ${client.url}, version ${health.version}`
          : `cannot reach ${client.url}`
      );
      if (health.reachable) {
        const models = await client.modelIds();
        line(
          models.length > 0,
          "models",
          models.length ? `${models.length} available` : "none. Connect a provider in opencode (opencode auth login)"
        );
        if (args.test && models.length) {
          const picked =
            args.model && args.model.includes("/")
              ? { providerID: args.model.split("/")[0], modelID: args.model.slice(args.model.indexOf("/") + 1) }
              : null;
          const t0 = Date.now();
          const r = await client.generate("Answer with one word.", "Reply with exactly: OK", picked);
          line(true, "test generation", `${JSON.stringify(r.text.slice(0, 40))} in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
        }
      }
    } catch (e) {
      line(false, "opencode API", e.message);
    }
  } else if (args.test && binary) {
    const s = settings(args);
    delete s.port;
    delete s.host;
    let ai;
    try {
      ai = await RelayAI.start({ reuse: false, ...s });
      const t0 = Date.now();
      const text = await ai.chat("Reply with exactly: OK");
      line(true, "test generation", `${JSON.stringify(text.slice(0, 40))} in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
    } catch (e) {
      line(false, "test generation", e.message);
    } finally {
      if (ai) await ai.close();
    }
  }
  if (!args.test) line(null, "tip", "add --test to send one real request through opencode");
  return ok ? 0 : 1;
}

async function cmdInstallOpencode(args) {
  const manager = new OpencodeManager();
  const binary = manager.findBinary();
  if (binary && !args.force) {
    out(`opencode is already installed: ${binary} (version ${manager.version() || "unknown"}).`);
    out("Use --force to reinstall or upgrade.");
    return 0;
  }
  const cmd = OpencodeManager.installCommand();
  if (!cmd) {
    err(
      "npm was not found. Install Node.js from https://nodejs.org, then run this again, " +
        "or follow https://opencode.ai/docs to install opencode another way."
    );
    return 1;
  }
  out("This will run: " + ["npm", ...cmd.slice(1)].join(" "));
  if (!args.yes) {
    if (!process.stdin.isTTY) {
      err("not a terminal; add --yes to confirm");
      return 1;
    }
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    const reply = await new Promise((r) => rl.question("Continue? [y/N] ", r));
    rl.close();
    if (!["y", "yes"].includes(reply.trim().toLowerCase())) {
      out("Cancelled.");
      return 1;
    }
  }
  const code = OpencodeManager.install();
  if (code === 0) out(`Installed. opencode version: ${manager.version() || "unknown"}`);
  return code;
}

const HANDLERS = {
  serve: cmdServe,
  start: cmdStart,
  stop: cmdStop,
  status: cmdStatus,
  models: cmdModels,
  chat: cmdChat,
  env: cmdEnv,
  claude: cmdClaude,
  doctor: cmdDoctor,
  "install-opencode": cmdInstallOpencode,
};

async function main(argv) {
  if (argv[0] === "--version" || argv[0] === "-V") {
    out(`relayai ${version} (node)`);
    return 0;
  }
  // claude: anything after the command that relayai does not know goes to claude
  let args;
  try {
    if (argv[0] === "claude") {
      const known = [];
      const rest = [];
      for (let i = 1; i < argv.length; i++) {
        const a = argv[i].split("=")[0];
        if (argv[i] === "--") {
          rest.push(...argv.slice(i + 1));
          break;
        }
        if (VALUE_FLAGS[a] && !["-s", "--system", "--shell", "--wait"].includes(a)) {
          known.push(argv[i]);
          if (!argv[i].includes("=")) known.push(argv[++i]);
        } else if (["--no-strict-models", "-v", "--verbose"].includes(a)) known.push(argv[i]);
        else rest.push(argv[i]);
      }
      args = parse(["claude", ...known]);
      args.passthrough = rest;
    } else args = parse(argv);
  } catch (e) {
    if (e instanceof UsageError) {
      err(e.message);
      return 2;
    }
    throw e;
  }
  if (!args.command || (args.help && args.command !== "claude")) {
    out(HELP);
    return args.command || args.help ? 0 : 2;
  }
  const handler = HANDLERS[args.command];
  if (!handler) {
    err(`unknown command '${args.command}'. Run relayai --help.`);
    return 2;
  }
  log.quiet = args.command !== "serve" && !args.verbose;
  try {
    return await handler(args);
  } catch (e) {
    if (e instanceof OpencodeNotInstalled || e instanceof ApiError || e instanceof RelayAIError) {
      err(e.message);
      return 1;
    }
    throw e;
  }
}

module.exports = { main, parse };
