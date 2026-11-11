"use strict";
/**
 * Files under the relayai home folder (default ~/.relayai, or $RELAYAI_HOME),
 * plus process helpers. Same layout as the Python package, so either one can
 * see and stop what the other started.
 *
 *   api_key        the persistent gateway key, created on first use
 *   gateway.json   the background gateway: pid, url, port, api_key
 *   opencode.json  the opencode serve relayai started: pid, url, password
 *   gateway.log    output of the background gateway
 *   opencode.log   output of the managed opencode serve
 *   workspace/     working folder for opencode serve, so it never touches your projects
 */
const crypto = require("node:crypto");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const IS_WIN = process.platform === "win32";

function home() {
  const dir = process.env.RELAYAI_HOME || path.join(os.homedir(), ".relayai");
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function workspace() {
  const dir = path.join(home(), "workspace");
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function readJson(name) {
  try {
    return JSON.parse(fs.readFileSync(path.join(home(), name), "utf8"));
  } catch {
    return null;
  }
}

function writeJson(name, data) {
  const file = path.join(home(), name);
  const tmp = file.replace(/\.json$/, ".tmp");
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2), { encoding: "utf8", mode: 0o600 });
  fs.renameSync(tmp, file);
}

function remove(name) {
  try {
    fs.unlinkSync(path.join(home(), name));
  } catch {
    /* already gone */
  }
}

/** The gateway key used when none is given. Created once, then reused. */
function loadOrCreateApiKey() {
  const file = path.join(home(), "api_key");
  try {
    const key = fs.readFileSync(file, "utf8").trim();
    if (key) return key;
  } catch {
    /* create it below */
  }
  const key = "rai-" + crypto.randomBytes(24).toString("base64url");
  fs.writeFileSync(file, key, { encoding: "utf8", mode: 0o600 });
  return key;
}

// ------------------------------------------------------------------ processes

function pidAlive(pid) {
  if (!pid) return false;
  try {
    process.kill(Number(pid), 0); // signal 0 only checks, on every platform
    return true;
  } catch (err) {
    return err.code === "EPERM";
  }
}

/**
 * Stop a process (and with tree=true its children). Safe on a pid that is
 * already gone. On Windows a detached child still counts as a child, so
 * tree=false is how the gateway is stopped without taking opencode with it.
 */
function killTree(pid, tree = true) {
  if (!pidAlive(pid)) return;
  if (IS_WIN) {
    const args = ["/PID", String(pid), "/F", ...(tree ? ["/T"] : [])];
    spawnSync("taskkill", args, { stdio: "ignore", windowsHide: true });
    return;
  }
  try {
    if (!tree) throw new Error("single process");
    process.kill(-Number(pid), "SIGTERM"); // started detached, so it leads a process group
  } catch {
    try {
      process.kill(Number(pid), "SIGTERM");
    } catch {
      /* gone */
    }
  }
}

function quoteWin(arg) {
  return /[\s"&|<>^]/.test(arg) ? `"${String(arg).replace(/"/g, '""')}"` : String(arg);
}

/** Spawn options that let a Windows .cmd/.bat shim run (Node needs a shell for those). */
function commandFor(cmd, args) {
  if (IS_WIN && /\.(cmd|bat)$/i.test(cmd)) {
    return { file: [cmd, ...args].map(quoteWin).join(" "), args: [], shell: true };
  }
  return { file: cmd, args, shell: false };
}

/** Start a process that keeps running after this one exits. Returns its pid. */
function spawnDetached(cmd, args, logName, { env, cwd } = {}) {
  const log = fs.openSync(path.join(home(), logName), "a");
  const c = commandFor(cmd, args);
  const child = spawn(c.file, c.args, {
    shell: c.shell,
    detached: true,
    windowsHide: true,
    stdio: ["ignore", log, log],
    env: env || process.env,
    cwd,
  });
  child.unref();
  fs.closeSync(log);
  return child.pid;
}

/** Find an executable on PATH (with PATHEXT on Windows). */
function which(name) {
  if (!name) return null;
  if (name.includes("/") || name.includes("\\")) return fs.existsSync(name) ? name : null;
  const exts = IS_WIN ? (process.env.PATHEXT || ".EXE;.CMD;.BAT;.COM").split(";").filter(Boolean) : [""];
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    if (!dir) continue;
    for (const ext of IS_WIN ? ["", ...exts] : exts) {
      const candidate = path.join(dir, name + ext);
      try {
        const stat = fs.statSync(candidate);
        if (stat.isFile() && (IS_WIN ? ext !== "" : (stat.mode & 0o111) !== 0)) return candidate;
      } catch {
        /* keep looking */
      }
    }
  }
  return null;
}

/**
 * npm installs a .cmd shim on Windows. When it only forwards to an .exe,
 * return the .exe so the pid we track is the real server and not cmd.exe.
 */
function resolveWindowsShim(file) {
  if (!IS_WIN || !file || !/\.cmd$/i.test(file)) return file;
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    return file;
  }
  for (const m of text.matchAll(/"%(?:dp0|~dp0)%\\([^"]+?\.exe)"/gi)) {
    const candidate = path.join(path.dirname(file), m[1]);
    try {
      if (fs.statSync(candidate).size > 1024 * 1024) return candidate; // a real binary, not a stub
    } catch {
      /* try the next one */
    }
  }
  return file;
}

function portFree(port, host) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once("error", () => resolve(null));
    srv.listen({ port, host, exclusive: true }, () => {
      const actual = srv.address().port;
      srv.close(() => resolve(actual));
    });
  });
}

/** `preferred` if it is free, else the next free one. 0 means any free port. */
async function freePort(preferred, host = "127.0.0.1", attempts = 50) {
  const ports = !preferred ? [0] : Array.from({ length: attempts }, (_, i) => Number(preferred) + i);
  for (const port of ports) {
    const got = await portFree(port, host);
    if (got) return got;
  }
  throw new Error(`no free port found from ${preferred}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

module.exports = {
  IS_WIN,
  home,
  workspace,
  readJson,
  writeJson,
  remove,
  loadOrCreateApiKey,
  pidAlive,
  killTree,
  commandFor,
  spawnDetached,
  which,
  resolveWindowsShim,
  freePort,
  sleep,
};
