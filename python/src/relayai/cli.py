"""The `relayai` command line tool."""
import argparse
import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time

from . import state
from ._version import __version__
from .client import Client
from .config import Config
from .errors import ApiError, OpencodeNotInstalled, RelayAIError
from .gateway import Gateway
from .opencode import OpencodeClient, OpencodeManager
from .sdk import GATEWAY_STATE, RelayAI, running_gateway

log = logging.getLogger("relayai")

EPILOG = """
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
"""


# ---------------------------------------------------------------------- helpers

def _err(message):
    print(f"relayai: {message}", file=sys.stderr)


def _settings(args):
    """Config overrides from the command line (None means: not given)."""
    return {
        "host": args.host, "port": args.port, "api_key": args.api_key, "model": args.model,
        "opencode_url": args.opencode_url, "opencode_password": args.opencode_password,
        "opencode_port": args.opencode_port, "opencode_bin": args.opencode_bin,
        "timeout": args.timeout, "max_concurrency": args.max_concurrency,
        "strict_models": False if args.no_strict_models else None,
    }


def _forward_flags(args):
    """The same flags again, for a child `relayai serve` process."""
    out = []
    for flag, value in (("--host", args.host), ("--port", args.port), ("--api-key", args.api_key),
                        ("--model", args.model), ("--opencode-url", args.opencode_url),
                        ("--opencode-password", args.opencode_password), ("--opencode-port", args.opencode_port),
                        ("--opencode-bin", args.opencode_bin), ("--timeout", args.timeout),
                        ("--max-concurrency", args.max_concurrency)):
        if value is not None:
            out += [flag, str(value)]
    if args.no_strict_models:
        out.append("--no-strict-models")
    return out


def _config(args):
    config = Config.from_env(**_settings(args))
    if not config.api_key:
        config.api_key = state.load_or_create_api_key()
    return config


@contextlib.contextmanager
def _client(args, quiet=False):
    """A client for the background gateway, or a short-lived private gateway."""
    saved = None if args.opencode_url else running_gateway()
    if saved:
        yield Client(saved["url"], saved["api_key"], model=args.model or "default")
        return
    if not quiet:
        print("(starting a private gateway; `relayai start` keeps one running and makes this instant)",
              file=sys.stderr)
    settings = {k: v for k, v in _settings(args).items() if k not in ("port", "host")}
    ai = RelayAI(reuse=False, **settings)
    try:
        yield ai
    finally:
        ai.close()


# --------------------------------------------------------------------- commands

def cmd_serve(args):
    config = _config(args)
    try:
        gw = Gateway(config, stop_opencode=not args.keep_opencode)
    except OpencodeNotInstalled as e:
        _err(str(e))
        return 1
    except OSError as e:
        _err(f"cannot listen on {config.host}:{config.port} ({e}). Use --port to pick another port.")
        return 1
    state.write_json(GATEWAY_STATE, {"pid": os.getpid(), "url": gw.url, "port": gw.port,
                                     "api_key": config.api_key, "version": __version__, "started": int(time.time())})
    try:
        reachable, version = gw.opencode.health()
        count = len(gw.catalog.ids())
    except ApiError as e:
        reachable, version, count = False, None, 0
        log.warning("%s", e.message)
    print(f"RelayAI {__version__}  (OpenAI and Anthropic compatible gateway for opencode)")
    print(f"  OpenAI base URL    : {gw.base_url}")
    print(f"  Anthropic base URL : {gw.url}")
    print(f"  API key            : {config.api_key}")
    print(f"  Default model      : {config.model or 'chosen by opencode'}")
    print(f"  opencode           : {gw.opencode.url}"
          + (f" (version {version}, {count} models" if reachable else " (not reachable yet")
          + (", started by relayai)" if gw.started_opencode else ")"))
    print("Press Ctrl+C to stop.", flush=True)

    def _stop(*_):
        raise KeyboardInterrupt

    with contextlib.suppress(ValueError, AttributeError):
        signal.signal(signal.SIGTERM, _stop)
    try:
        gw.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        gw.close()
        saved = state.read_json(GATEWAY_STATE)
        if saved and saved.get("pid") == os.getpid():
            state.remove(GATEWAY_STATE)
        print("\nRelayAI stopped.")
    return 0


def cmd_start(args):
    saved = running_gateway()
    if saved:
        print(f"RelayAI is already running at {saved['url']} (pid {saved['pid']}).")
        return 0
    if not args.opencode_url and not OpencodeManager(binary=args.opencode_bin or "").find_binary():
        _err(str(OpencodeNotInstalled()))
        return 1
    state.remove(GATEWAY_STATE)
    cmd = [sys.executable, "-m", "relayai", "serve"] + _forward_flags(args)
    pid = state.spawn_detached(cmd, "gateway.log", env=dict(os.environ, PYTHONUNBUFFERED="1"))
    deadline = time.time() + args.wait
    while time.time() < deadline:
        saved = running_gateway()
        if saved:
            print(f"RelayAI started in the background (pid {saved['pid']}).")
            print(f"  OpenAI base URL    : {saved['url']}/v1")
            print(f"  Anthropic base URL : {saved['url']}")
            print(f"  API key            : {saved['api_key']}")
            print("Stop it with `relayai stop`. Logs: " + str(state.home() / "gateway.log"))
            return 0
        if not state.pid_alive(pid) and not state.read_json(GATEWAY_STATE):
            break
        time.sleep(0.5)
    _err(f"the gateway did not start. See {state.home() / 'gateway.log'}")
    return 1


def cmd_stop(args):
    saved = state.read_json(GATEWAY_STATE)
    stopped = False
    if saved and state.pid_alive(saved.get("pid")):
        state.kill_tree(saved["pid"], tree=False)  # opencode is handled on its own below
        stopped = True
    state.remove(GATEWAY_STATE)
    opencode_stopped = False if args.keep_opencode else OpencodeManager().stop()
    print(("Stopped the gateway." if stopped else "No gateway was running.")
          + (" Stopped opencode serve." if opencode_stopped else ""))
    return 0


def cmd_status(args):
    gateway = running_gateway()
    manager = OpencodeManager()
    opencode = manager.running()
    if args.json:
        print(json.dumps({"gateway": gateway, "opencode": opencode and {k: v for k, v in opencode.items()
                                                                       if k != "password"}}, indent=2))
        return 0 if gateway else 1
    if gateway:
        print(f"gateway   running  {gateway['url']}  (pid {gateway['pid']})")
        print(f"          OpenAI base URL {gateway['url']}/v1, API key {gateway['api_key']}")
    else:
        print("gateway   not running  (start it with `relayai start`)")
    if opencode:
        print(f"opencode  running  {opencode['url']}  (pid {opencode['pid']}, version {opencode.get('version')})")
    else:
        print("opencode  not running under relayai")
    return 0 if gateway else 1


def cmd_models(args):
    with _client(args, quiet=args.json) as client:
        ids = client.models()
    if args.json:
        print(json.dumps(ids, indent=2))
    else:
        print("\n".join(ids) if ids else "(opencode lists no models; connect a provider in opencode first)")
    return 0


def cmd_chat(args):
    prompt = " ".join(args.prompt).strip()
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
    with _client(args) as client:
        if prompt:
            _answer(client, [{"role": "user", "content": prompt}], args)
            return 0
        print("RelayAI chat. Empty line or Ctrl+C to quit, /reset to forget the conversation.")
        history = []
        while True:
            try:
                line = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not line:
                return 0
            if line == "/reset":
                history = []
                print("(conversation cleared)")
                continue
            history.append({"role": "user", "content": line})
            print("ai> ", end="", flush=True)
            reply = _answer(client, history, args)
            if reply is None:
                history.pop()
            else:
                history.append({"role": "assistant", "content": reply})


def _answer(client, messages, args):
    try:
        if args.no_stream:
            text = client.chat(messages, model=args.model, system=args.system)
            print(text)
            return text
        pieces = []
        for piece in client.stream(messages, model=args.model, system=args.system):
            pieces.append(piece)
            print(piece, end="", flush=True)
        print()
        return "".join(pieces)
    except ApiError as e:
        print()
        _err(e.message)
        return None


def cmd_env(args):
    saved = running_gateway()
    if not saved:
        _err("no gateway is running. Start one with `relayai start`.")
        return 1
    url, key = saved["url"], saved["api_key"]
    model = args.model or "default"
    pairs = [("OPENAI_BASE_URL", url + "/v1"), ("OPENAI_API_KEY", key),
             ("ANTHROPIC_BASE_URL", url), ("ANTHROPIC_AUTH_TOKEN", key), ("ANTHROPIC_MODEL", model)]
    shell = args.shell or ("powershell" if os.name == "nt" else "bash")
    for name, value in pairs:
        if shell == "powershell":
            print(f'$env:{name} = "{value}"')
        elif shell == "cmd":
            print(f"set {name}={value}")
        elif shell == "fish":
            print(f"set -gx {name} {value}")
        else:
            print(f"export {name}={value}")
    return 0


def cmd_claude(args):
    claude = shutil.which("claude")
    if not claude:
        _err("Claude Code (`claude`) was not found on PATH. Install it from https://claude.com/claude-code")
        return 1
    passthrough = list(args.claude_args)
    if passthrough[:1] == ["--"]:
        passthrough = passthrough[1:]
    with _client(args) as client:
        model = args.model or "default"
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        env.update({
            "ANTHROPIC_BASE_URL": client.anthropic_base_url, "ANTHROPIC_AUTH_TOKEN": client.api_key,
            "ANTHROPIC_MODEL": model, "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model, "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        })
        print(f"Claude Code -> RelayAI at {client.anthropic_base_url} (model: {model})", file=sys.stderr)
        try:
            return subprocess.call([claude] + passthrough, env=env)
        except KeyboardInterrupt:
            return 130


def cmd_doctor(args):
    ok = True

    def line(good, label, detail=""):
        nonlocal ok
        ok = ok and good is not False
        mark = {True: "ok  ", False: "FAIL", None: "info"}[good]
        print(f"[{mark}] {label}" + (f": {detail}" if detail else ""))

    line(True, "relayai", f"{__version__} (Python {sys.version.split()[0]})")
    line(True, "home folder", str(state.home()))
    manager = OpencodeManager(binary=args.opencode_bin or "")
    binary = manager.find_binary()
    if args.opencode_url:
        line(None, "opencode binary", "not needed, using --opencode-url")
    elif binary:
        line(True, "opencode binary", f"{binary} (version {manager.version() or 'unknown'})")
    else:
        line(False, "opencode binary", "not found. Run `relayai install-opencode`")
    line(None, "npm", shutil.which("npm") or "not found (only needed to install opencode)")
    line(None, "Claude Code", shutil.which("claude") or "not found (only needed for `relayai claude`)")

    if args.opencode_url:
        client = OpencodeClient(args.opencode_url, password=args.opencode_password
                                or os.environ.get("OPENCODE_SERVER_PASSWORD", ""))
    else:
        running = manager.running()
        line(None, "managed opencode serve", f"running at {running['url']}" if running else "not running")
        client = OpencodeClient(running["url"], password=running.get("password", "")) if running else None

    gateway = running_gateway()
    line(None, "background gateway", f"running at {gateway['url']}" if gateway else "not running")

    if client:
        try:
            reachable, version = client.health()
            line(reachable, "opencode API", f"{client.api_version()} at {client.url}, version {version}"
                 if reachable else f"cannot reach {client.url}")
            if reachable:
                models = client.model_ids()
                line(bool(models), "models", f"{len(models)} available" if models else
                     "none. Connect a provider in opencode (opencode auth login)")
                if args.test and models:
                    model = args.model
                    picked = {"providerID": model.split("/", 1)[0], "modelID": model.split("/", 1)[1]} \
                        if model and "/" in model else None
                    started = time.time()
                    text, _, _ = client.generate("Answer with one word.", "Reply with exactly: OK", picked)
                    line(True, "test generation", f"{text[:40]!r} in {time.time() - started:.1f}s")
        except ApiError as e:
            line(False, "opencode API", e.message)
    elif args.test and binary:
        try:
            settings = {k: v for k, v in _settings(args).items() if k not in ("port", "host")}
            with RelayAI(reuse=False, **settings) as ai:
                started = time.time()
                text = ai.chat("Reply with exactly: OK")
                line(True, "test generation", f"{text[:40]!r} in {time.time() - started:.1f}s")
        except (ApiError, RelayAIError) as e:
            line(False, "test generation", str(e))
    if not args.test:
        line(None, "tip", "add --test to send one real request through opencode")
    return 0 if ok else 1


def cmd_install_opencode(args):
    manager = OpencodeManager()
    binary = manager.find_binary()
    if binary and not args.force:
        print(f"opencode is already installed: {binary} (version {manager.version() or 'unknown'}).")
        print("Use --force to reinstall or upgrade.")
        return 0
    cmd = OpencodeManager.install_command()
    if not cmd:
        _err("npm was not found. Install Node.js from https://nodejs.org, then run this again, "
             "or follow https://opencode.ai/docs to install opencode another way.")
        return 1
    print("This will run: " + " ".join(["npm"] + cmd[1:]))
    if not args.yes:
        if not sys.stdin.isatty():
            _err("not a terminal; add --yes to confirm")
            return 1
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    code = OpencodeManager.install()
    if code == 0:
        print(f"Installed. opencode version: {manager.version() or 'unknown'}")
    return code


# ------------------------------------------------------------------------ parser

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("gateway and opencode options (also settable with RELAYAI_* variables)")
    g.add_argument("--host", help="interface to listen on (default 127.0.0.1; 0.0.0.0 exposes it)")
    g.add_argument("--port", type=int, help="gateway port (default 8000)")
    g.add_argument("--api-key", help="key clients must send (default: stored in ~/.relayai/api_key)")
    g.add_argument("-m", "--model", help="model as provider/model (default: opencode's default)")
    g.add_argument("--opencode-url", help="use an opencode serve you run yourself instead of a managed one")
    g.add_argument("--opencode-password", help="password of that opencode serve")
    g.add_argument("--opencode-port", type=int, help="preferred port for the managed opencode serve (default 4096)")
    g.add_argument("--opencode-bin", help="path to the opencode executable")
    g.add_argument("--timeout", type=int, help="seconds to wait for one answer (default 600)")
    g.add_argument("--max-concurrency", type=int, help="answers generated at once (default 4)")
    g.add_argument("--no-strict-models", action="store_true", help="unknown model names fall back to the default")
    g.add_argument("-v", "--verbose", action="store_true", help="more logging")

    parser = argparse.ArgumentParser(
        prog="relayai", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
        description="OpenAI and Anthropic compatible API for the models in opencode, with tool calling.")
    parser.add_argument("--version", action="version", version=f"relayai {__version__} (python)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("serve", parents=[common], help="run the gateway in this terminal")
    p.add_argument("--keep-opencode", action="store_true", help="leave the managed opencode serve running on exit")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("start", parents=[common], help="run the gateway in the background")
    p.add_argument("--wait", type=int, default=120, help="seconds to wait for it to come up (default 120)")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", parents=[common], help="stop the background gateway and the managed opencode serve")
    p.add_argument("--keep-opencode", action="store_true", help="leave opencode serve running")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("status", parents=[common], help="show what is running")
    p.add_argument("--json", action="store_true", help="machine readable output")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("models", parents=[common], help="list the models opencode can use")
    p.add_argument("--json", action="store_true", help="machine readable output")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("chat", parents=[common], help="ask a question, or chat interactively")
    p.add_argument("prompt", nargs="*", help="the question (omit for an interactive chat; stdin works too)")
    p.add_argument("-s", "--system", help="system prompt")
    p.add_argument("--no-stream", action="store_true", help="print the answer in one piece")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("env", parents=[common], help="print variables for OpenAI / Anthropic clients")
    p.add_argument("--shell", choices=["bash", "powershell", "cmd", "fish"], help="output syntax")
    p.set_defaults(func=cmd_env)

    p = sub.add_parser("claude", parents=[common], help="run Claude Code through the gateway",
                       description="Run Claude Code with its API pointed at RelayAI. "
                                   "Arguments after -- go to claude, e.g. relayai claude -- -p \"hi\"")
    p.add_argument("claude_args", nargs=argparse.REMAINDER, help="arguments for claude")
    p.set_defaults(func=cmd_claude)

    p = sub.add_parser("doctor", parents=[common], help="check the setup")
    p.add_argument("--test", action="store_true", help="also send one real request through opencode")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("install-opencode", help="install opencode with npm (npm install -g @opencode/cli)")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--force", action="store_true", help="install even if opencode is already there")
    p.set_defaults(func=cmd_install_opencode)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    verbose = getattr(args, "verbose", False)
    level = logging.INFO if args.command == "serve" or verbose else logging.WARNING
    logging.basicConfig(level=logging.DEBUG if verbose else level,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        return args.func(args)
    except OpencodeNotInstalled as e:
        _err(str(e))
        return 1
    except ApiError as e:
        _err(e.message)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
