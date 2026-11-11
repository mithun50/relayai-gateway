"""
Files under the relayai home folder (default ~/.relayai, or $RELAYAI_HOME),
plus small process helpers. The Python and Node packages use the same layout,
so either one can see and stop what the other started.

  api_key          the persistent gateway key, created on first use
  gateway.json     the background gateway: pid, url, port, api_key
  opencode.json    the opencode serve relayai started: pid, url, password
  gateway.log      output of the background gateway
  opencode.log     output of the managed opencode serve
  workspace/       working folder for opencode serve, so it never touches your projects
"""
import json
import os
import re
import secrets
import socket
import subprocess
from pathlib import Path


def home():
    path = Path(os.environ.get("RELAYAI_HOME") or (Path.home() / ".relayai"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspace():
    path = home() / "workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(name):
    try:
        return json.loads((home() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(name, data):
    path = home() / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    _private(path)


def remove(name):
    try:
        (home() / name).unlink()
    except OSError:
        pass


def _private(path):
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def load_or_create_api_key():
    """The gateway key used when none is given. Created once, then reused."""
    path = home() / "api_key"
    try:
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    except OSError:
        pass
    key = "rai-" + secrets.token_urlsafe(24)
    path.write_text(key, encoding="utf-8")
    _private(path)
    return key


# ------------------------------------------------------------------ processes

def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        # Never use os.kill(pid, 0) on Windows: it terminates the process.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def kill_tree(pid, tree=True):
    """
    Stop a process (and with tree=True its children). Safe on a pid that is
    already gone. On Windows a detached child still counts as a child, so
    tree=False is how the gateway is stopped without taking opencode with it.
    """
    if not pid_alive(pid):
        return
    if os.name == "nt":
        cmd = ["taskkill", "/PID", str(pid), "/F"] + (["/T"] if tree else [])
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    import signal

    try:
        if not tree:
            raise OSError("single process")
        os.killpg(int(pid), signal.SIGTERM)  # started with start_new_session, so it leads a group
    except OSError:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass


def spawn_detached(cmd, log_name, env=None, cwd=None):
    """Start a process that keeps running after this one exits. Returns its pid."""
    kwargs = {}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    with open(home() / log_name, "ab") as log:
        proc = subprocess.Popen(cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, **kwargs)
    return proc.pid


def resolve_windows_shim(path):
    """
    npm installs a .cmd shim on Windows. When the shim only forwards to an .exe,
    return the .exe so the pid we track is the real server and not cmd.exe.
    """
    if os.name != "nt" or not path or not path.lower().endswith(".cmd"):
        return path
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return path
    for target in re.findall(r'"%(?:dp0|~dp0)%\\([^"]+?\.exe)"', text, re.I):
        candidate = Path(path).parent / target
        if candidate.is_file() and candidate.stat().st_size > 1024 * 1024:
            return str(candidate)  # a real binary, not a tiny placeholder stub
    return path


def free_port(preferred, host="127.0.0.1", attempts=50):
    """`preferred` if it is free, else the next free one. 0 means any free port."""
    ports = [0] if not preferred else list(range(int(preferred), int(preferred) + attempts))
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return sock.getsockname()[1]
            except OSError:
                continue
    raise OSError(f"no free port found from {preferred}")
