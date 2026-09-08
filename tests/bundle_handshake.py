"""Run a built .mcpb the way Claude Desktop would and require the handshake.

Vendored byte-identical across the server family; needs only the standard
library and a uv.

    python tests/bundle_handshake.py dist/<name>-<version>.mcpb
    python tests/bundle_handshake.py dist/x.mcpb --cold          # empty uv cache and python
                                                                 # dir: the first-launch cost
    python tests/bundle_handshake.py dist/x.mcpb --python 3.10   # an interpreter other than
                                                                 # the bundle's pin
    python tests/bundle_handshake.py dist/x.mcpb --uv PATH       # a particular uv, e.g. the
                                                                 # one Claude Desktop ships

Unpacks the bundle, reads manifest.json, substitutes ${__dirname} in
server.mcp_config.args exactly as the host does, runs the command with uv,
sends JSON-RPC initialize and tools/list on stdin, and requires serverInfo to
name the manifest's version. It also reports which interpreter uv chose and
requires its major.minor to match --python when one was given. This is the
gate that would have caught a bundle whose native wheels matched one
interpreter only: a bundle that builds is not a bundle that runs.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path


def _rpc(cmd: list[str], env: dict, cwd: str, wait: float) -> tuple[dict, str, float]:
    init = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "bundle-handshake", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8")
    replies: dict = {}

    def reader():
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("{"):
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") in (1, 2):
                    replies[msg["id"]] = msg
                    if len(replies) == 2:
                        return

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    for m in init:
        proc.stdin.write(json.dumps(m) + "\n")
    proc.stdin.flush()
    th.join(wait)
    elapsed = time.monotonic() - t0
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    err = proc.stderr.read()
    return replies, err, elapsed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bundle")
    ap.add_argument("--uv", help="uv executable (default: UV env var, then PATH)")
    ap.add_argument("--python", help="interpreter request for uv, instead of the one uv would pick from the machine")
    ap.add_argument("--cold", action="store_true",
                    help="empty UV_CACHE_DIR and UV_PYTHON_INSTALL_DIR, managed interpreters only: the first launch on a machine with no usable Python")
    ap.add_argument("--timeout", type=float, default=240.0)
    a = ap.parse_args()

    uv = a.uv or os.environ.get("UV") or shutil.which("uv")
    if not uv:
        print("FAIL: no uv found; pass --uv", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="mcpb-handshake-"))
    try:
        with zipfile.ZipFile(a.bundle) as z:
            z.extractall(work)
        manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
        want = manifest["version"]
        server = manifest["server"]
        if server["type"] != "uv":
            print(f"FAIL: server.type is {server['type']!r}, expected 'uv'")
            return 1
        cfg = server["mcp_config"]
        if cfg["command"] != "uv":
            print(f"FAIL: mcp_config.command is {cfg['command']!r}, expected 'uv'")
            return 1
        args = [x.replace("${__dirname}", str(work)) for x in cfg.get("args", [])]
        env = os.environ.copy()
        # Pass the manifest's env block exactly as Claude Desktop does when every
        # user_config field is left blank: the "${user_config.KEY}" placeholders
        # arrive verbatim (Desktop substitutes only fields that have a value).
        # The entry point must treat those as unset; the check after the run
        # confirms nothing was created under a placeholder name.
        for k, v in (cfg.get("env") or {}).items():
            env[k] = v
        env.pop("VIRTUAL_ENV", None)
        if a.cold:
            env["UV_CACHE_DIR"] = str(work / "_uv-cache")
            env["UV_PYTHON_INSTALL_DIR"] = str(work / "_uv-python")
            env["UV_PYTHON_PREFERENCE"] = "only-managed"
        if a.python:
            env["UV_PYTHON"] = a.python

        cmd = [uv] + args
        print("command:", " ".join(f'"{c}"' if " " in c else c for c in cmd))
        replies, err, elapsed = _rpc(cmd, env, str(work), a.timeout)
        info = replies.get(1, {}).get("result", {}).get("serverInfo", {})
        tools = [t["name"] for t in replies.get(2, {}).get("result", {}).get("tools", [])]
        mode = "cold" if a.cold else "warm"
        print(f"initialize: {info.get('name')} {info.get('version')}  tools/list: {len(tools)}  ({elapsed:.1f}s, {mode})")

        # Which interpreter did uv choose for that environment?
        srv_dir = next((args[i + 1] for i, x in enumerate(args) if x == "--directory"), str(work / "server"))
        probe = [uv, "--directory", srv_dir, "run", "--frozen", "python", "-c",
                 "import sys; print(sys.version.split()[0], sys.executable)"]
        p = subprocess.run(probe, env=env, capture_output=True, text=True, encoding="utf-8")
        interp = p.stdout.strip()
        print("interpreter:", interp or p.stderr.strip()[-300:])

        # Does the entry point treat unsubstituted placeholders as unset? Import
        # main.py (its server start is behind __name__ == "__main__") with the
        # manifest's env block plus a placeholder credential, then look at what
        # survived. The handshake alone would not show this: the ledger only
        # writes on a tool call, and a blank credential only matters on one.
        entry = str(work / server["entry_point"])
        probe_code = (
            "import os, importlib.util, sys\n"
            "os.environ['X_PROBE_KEY'] = '${user_config.x_probe_key}'\n"
            f"spec = importlib.util.spec_from_file_location('bundle_entry', {entry!r})\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "left = sorted(k for k, v in os.environ.items() if '${user_config.' in v)\n"
            "print('LEFT', left)\n"
        )
        p2 = subprocess.run([uv, "--directory", srv_dir, "run", "--frozen", "python", "-c", probe_code],
                            env=env, capture_output=True, text=True, encoding="utf-8")
        left_line = next((ln for ln in p2.stdout.splitlines() if ln.startswith("LEFT ")), None)
        placeholders_ok = left_line == "LEFT []"
        print("placeholders:", "stripped" if placeholders_ok else (left_line or p2.stderr.strip()[-300:]))

        ok = True
        stray = [p for p in work.rglob("*") if "${user_config" in p.name]
        if stray:
            ok = False
            print("FAIL: the server created a path from an unsubstituted user_config placeholder:")
            for p in stray[:5]:
                print("   ", p.relative_to(work))
        if not placeholders_ok:
            ok = False
            print("FAIL: the entry point left an unsubstituted user_config placeholder in the environment")
        if not info:
            ok = False
            print("FAIL: no initialize reply")
            if err.strip():
                print("stderr:", err.strip()[-1500:])
        elif info.get("version") != want:
            ok = False
            print(f"FAIL: server answered version {info.get('version')!r}, manifest says {want!r}")
        if not tools:
            ok = False
            print("FAIL: tools/list is empty")
        if a.python and interp:
            got = ".".join(interp.split()[0].split(".")[:2])
            if got != a.python:
                ok = False
                print(f"FAIL: asked uv for Python {a.python}, it ran {got}")
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
