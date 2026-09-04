"""Stdio smoke test, vendored byte-identical across the server family.

Starts the installed console script as a subprocess, performs the MCP
initialize handshake over stdio, lists the tools, and checks the list against
the tool table in README.md. With RUN_LIVE=1 it also calls one tool named on
the command line and reports the envelope's diagnostic codes.

    python tests/smoke_stdio.py                      # handshake + tools/list
    RUN_LIVE=1 python tests/smoke_stdio.py <tool> '<json params>'

Exit status is non-zero on any mismatch, so the script can gate a release.
It needs nothing beyond the standard library and an installed package.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10: no tomllib; a regex over [project] is enough here
    tomllib = None


def _load_project(text: str) -> dict:
    """[project] name/version/scripts, via tomllib where present."""
    if tomllib is not None:
        return tomllib.loads(text)["project"]
    proj: dict = {}
    body = text.split("[project]", 1)[1]
    for key in ("name", "version"):
        m = re.search(rf'^{key}\s*=\s*"([^"]+)"', body, re.M)
        if m:
            proj[key] = m.group(1)
    proj["scripts"] = {}
    if "[project.scripts]" in text:
        section = text.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
        proj["scripts"] = dict(re.findall(r'^([\w-]+)\s*=\s*"([^"]+)"', section, re.M))
    return proj

ROOT = Path(__file__).resolve().parent.parent


def _console_script() -> str:
    """The first [project.scripts] entry that is not the ledger CLI."""
    scripts = _load_project((ROOT / "pyproject.toml").read_text(encoding="utf-8")).get("scripts", {})
    for name in scripts:
        if not name.endswith("-ledger"):
            return name
    raise SystemExit("no console script in pyproject.toml")


def _readme_tools() -> list[str]:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"^## Tools\s*$(.*?)^## ", text, re.S | re.M)
    if not m:
        raise SystemExit("README.md has no '## Tools' section")
    return sorted(set(re.findall(r"^\|\s*`([a-z0-9_]+)`", m.group(1), re.M)))


def _rpc(proc: subprocess.Popen, msgs: list[dict], want: set[int], wait: float) -> list[dict]:
    """Send the messages, then read stdout until every id in `want` has
    answered or `wait` seconds pass. Closing stdin first would race the server:
    some exit on end-of-input before flushing a reply already in hand."""
    import threading

    replies: list[dict] = []
    seen: set[int] = set()
    done = threading.Event()

    def reader():
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("{"):
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                replies.append(msg)
                if msg.get("id") in want:
                    seen.add(msg["id"])
                    if seen >= want:
                        done.set()
                        return
        done.set()

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    for m in msgs:
        proc.stdin.write(json.dumps(m) + "\n")
    proc.stdin.flush()
    done.wait(wait)
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    err = proc.stderr.read()
    if err.strip():
        print("stderr:", err.strip()[:800], file=sys.stderr)
    return replies


def main() -> int:
    script = _console_script()
    exe = Path(sys.executable).parent / script
    if not exe.exists():
        exe = Path(script)  # rely on PATH
    init = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    live = os.environ.get("RUN_LIVE") == "1" and len(sys.argv) >= 3
    if live:
        init.append({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": sys.argv[1], "arguments": {"params": json.loads(sys.argv[2])}}})

    proc = subprocess.Popen([str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", env=os.environ.copy())
    t0 = time.monotonic()
    replies = _rpc(proc, init, want={1, 2, 3} if live else {1, 2}, wait=40 if live else 15)
    by_id = {r.get("id"): r for r in replies if "id" in r}

    ok = True
    info = by_id.get(1, {}).get("result", {}).get("serverInfo", {})
    print(f"server: {info.get('name')} {info.get('version')}  ({time.monotonic() - t0:.1f}s)")
    if not info:
        print("FAIL: no initialize reply"); return 1

    listed = sorted(t["name"] for t in by_id.get(2, {}).get("result", {}).get("tools", []))
    readme = _readme_tools()
    print(f"tools/list: {len(listed)}  README: {len(readme)}")
    if listed != readme:
        ok = False
        print("FAIL: tool list differs from README table")
        print("  only in server:", sorted(set(listed) - set(readme)))
        print("  only in README:", sorted(set(readme) - set(listed)))

    if live:
        r = by_id.get(3, {})
        res = r.get("result", {})
        text = (res.get("content") or [{}])[0].get("text", "")
        if res.get("isError"):
            ok = False
            print("FAIL: tool call errored:", text[:300])
        else:
            try:
                env = json.loads(text)
                codes = [d.get("code") for d in env.get("diagnostics", [])]
                print(f"live {sys.argv[1]}: total={env.get('result', {}).get('total')} "
                      f"returned={env.get('result', {}).get('returned')} diagnostics={codes}")
                if any(c in ("TRANSPORT_ERROR", "API_ERROR") for c in codes):
                    ok = False
            except ValueError:
                print("live reply is not JSON:", text[:200])
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
