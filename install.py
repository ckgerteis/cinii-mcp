#!/usr/bin/env python3
"""
install.py - install the bibliographic MCP server family and point them at one
receipts folder.

Cross-platform Python port of install.ps1 (vendored byte-identical into the
six sibling repositories: cinii-mcp, jstage-mcp, ndl-mcp,
korea-scholarship-mcp, semantic-scholar-mcp, openalex-mcp). Standard library
only, Python 3.10+, works on Windows, macOS and Linux.

    python install.py                                   # this repo's server
    python install.py --all                              # the whole family
    python install.py --servers cinii,ndl
    python install.py --dry-run --all
    python install.py --print-config

See README.md ("Installing more than this one") and install.ps1 in any of
the six repos for the behaviour this reproduces. Differences from install.ps1
are called out in comments marked "DEVIATION:".
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------
# The catalogue. Short name -> repo (git checkout / GitHub repo name), dist
# (distribution name as pip reports it), pkg (importable
# package name), cmd (console-script name), creds (env vars read by the
# server; a name ending in EMAIL is optional and never nags if unset, same
# rule install.ps1 uses: `$_ -notmatch 'EMAIL$'`).
#
# --------------------------------------------------------------------------

SERVERS: dict[str, dict] = {
    "cinii": {
        "repo": "cinii-mcp",
        "dist": "cinii-mcp",
        "pkg": "cinii_mcp",
        "cmd": "cinii-mcp",
        "creds": ["CINII_APPID"],
    },
    "jstage": {
        "repo": "jstage-mcp",
        "dist": "jstage-mcp",
        "pkg": "jstage_mcp",
        "cmd": "jstage-mcp",
        "creds": [],
    },
    "ndl": {
        "repo": "ndl-mcp",
        "dist": "ndl-mcp",
        "pkg": "ndl_mcp",
        "cmd": "ndl-mcp",
        "creds": [],
    },
    "korea_scholarship": {
        "repo": "korea-scholarship-mcp",
        "dist": "korea-scholarship-mcp",
        "pkg": "korea_scholarship_mcp",
        "cmd": "korea-scholarship-mcp",
        "creds": ["KCI_API_KEY"],
    },
    "openalex": {
        "repo": "openalex-mcp",
        "dist": "openalex-mcp",
        "pkg": "openalex_mcp",
        "cmd": "openalex-mcp",
        "creds": ["OPENALEX_API_KEY", "OPENALEX_EMAIL"],
    },
    "semantic_scholar": {
        "repo": "semantic-scholar-mcp",
        "dist": "semantic-scholar-mcp",
        "pkg": "semantic_scholar_mcp",
        "cmd": "semantic-scholar-mcp",
        "creds": ["SEMANTIC_SCHOLAR_API_KEY"],
    },
}

# Family list used by the post-install identity check (mirrors _FAMILY in the
# ps1 probe script).
FAMILY_PKGS = tuple(v["pkg"] for v in SERVERS.values())

FORM_URL = "https://form2.ndl.go.jp/form/pub/ndl07/api"
TERMS_URL = "https://ndlsearch.ndl.go.jp/help/api"

GIT_ORG = "ckgerteis"


class InstallError(Exception):
    """Expected, user-facing failure. Caught in main() and printed without a
    traceback, same spirit as install.ps1's `$ErrorActionPreference = "Stop"`
    plus `throw`."""


def step(msg: str) -> None:
    print(f"\n==> {msg}")


def warn(msg: str) -> None:
    print(f"    {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# Platform-specific locations
# --------------------------------------------------------------------------

def config_path() -> Path:
    """Claude Desktop's claude_desktop_config.json."""
    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise InstallError("APPDATA is not set; cannot locate Claude's config directory.")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    # Linux and anything else POSIX-like
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def default_venv_dir() -> Path:
    """Where the shared venv lives if --venv is not given.

    install.ps1 uses %APPDATA%\\Claude\\mcp-servers\\.venv on Windows, i.e. a
    "mcp-servers" folder next to Claude Desktop's own config directory. We
    keep that on Windows, and use the platform-idiomatic equivalent of "next
    to Claude's own per-user directory" elsewhere: Application Support on
    macOS, ~/.config on Linux (the same directory family
    claude_desktop_config.json itself lives in on each OS - see
    config_path() above - so `mcp-servers/.venv` sits beside the config file
    rather than in some unrelated location).
    """
    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise InstallError("APPDATA is not set; cannot locate the default venv directory.")
        return Path(appdata) / "Claude" / "mcp-servers" / ".venv"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "mcp-servers" / ".venv"
    return Path.home() / ".config" / "Claude" / "mcp-servers" / ".venv"


def python_exe_in(venv_dir: Path) -> Path:
    if platform.system() == "Windows":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def scripts_dir_of(venv_dir: Path) -> Path:
    return python_exe_in(venv_dir).parent


def exe_path_in(venv_dir: Path, cmd: str) -> Path:
    if platform.system() == "Windows":
        return scripts_dir_of(venv_dir) / f"{cmd}.exe"
    return scripts_dir_of(venv_dir) / cmd


def default_receipts_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise InstallError("APPDATA is not set; cannot locate the default receipts directory.")
        return Path(appdata) / "Claude" / "mcp-receipts"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "mcp-receipts"
    return Path.home() / ".config" / "Claude" / "mcp-receipts"


# --------------------------------------------------------------------------
# Which server does this copy of the script belong to?
#
# DEVIATION: install.ps1 answers this by looking at the *name of the folder
# the script sits in* (Split-Path $PSScriptRoot -Leaf) against a hardcoded
# folder-name table. That breaks the moment someone clones a repo under a
# different folder name. This port instead reads the `name` key out of
# pyproject.toml sitting beside the script - the same file that already
# names each repo unambiguously and cannot be renamed by accident - and maps
# that to a short server key via SERVERS[*]['repo'] (which equals the
# pyproject name in every one of the six repos). Requested explicitly by the
# porting brief.
# --------------------------------------------------------------------------

_NAME_RE = re.compile(r'(?m)^\s*name\s*=\s*"([^"]+)"\s*$')


def read_pyproject_name(pyproject_path: Path) -> Optional[str]:
    try:
        text = pyproject_path.read_text(encoding="utf-8")
    except OSError:
        return None
    # Only look inside the [project] table, not [tool.*] tables that might
    # also define a `name` key.
    m = re.search(r"(?ms)^\[project\]\s*$(.*?)(?=^\[|\Z)", text)
    body = m.group(1) if m else text
    m2 = _NAME_RE.search(body)
    return m2.group(1) if m2 else None


def detect_self(script_dir: Path) -> Optional[str]:
    name = read_pyproject_name(script_dir / "pyproject.toml")
    if not name:
        return None
    for short, meta in SERVERS.items():
        if meta["repo"] == name:
            return short
    return None


# --------------------------------------------------------------------------
# Source resolution: sibling checkout > GitHub, pinned to a tag with --tag.
# GitHub releases are the distribution channel; nothing is on a package index.
# --------------------------------------------------------------------------

@dataclass
class Source:
    kind: str  # "local" | "git"
    value: str  # path or "git+https://...[@tag]"


def get_source(repo: str, script_dir: Path, tag: Optional[str] = None) -> Source:
    for cand in (script_dir.parent / repo, script_dir):
        if cand.name != repo:
            continue
        if (cand / "pyproject.toml").is_file():
            return Source("local", str(cand))
    url = f"git+https://github.com/{GIT_ORG}/{repo}"
    return Source("git", url + (f"@{tag}" if tag else ""))


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.is_file():
        return {"mcpServers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise InstallError(f"Could not read/parse {path}: {e}")
    if not isinstance(data, dict):
        raise InstallError(f"{path} does not contain a JSON object at the top level.")
    data.setdefault("mcpServers", {})
    if not isinstance(data["mcpServers"], dict):
        raise InstallError(f"{path}: 'mcpServers' is not an object.")
    return data


def gather_known(existing: dict) -> tuple[list[str], list[str], list[str]]:
    dirs: list[str] = []
    sess: list[str] = []
    legacy: list[str] = []
    for entry in existing.values():
        env = entry.get("env") if isinstance(entry, dict) else None
        if not isinstance(env, dict):
            continue
        if env.get("MCP_RECEIPT_DIR"):
            dirs.append(str(env["MCP_RECEIPT_DIR"]))
        if env.get("MCP_RECEIPT_SESSION"):
            sess.append(str(env["MCP_RECEIPT_SESSION"]))
        if env.get("MCP_RECEIPT_LOG"):
            legacy.append(str(env["MCP_RECEIPT_LOG"]))
    return (sorted(set(dirs)), sorted(set(sess)), sorted(set(legacy)))


# --------------------------------------------------------------------------
# Plan: everything that can be decided without touching disk beyond reading
# the existing config, and without running pip. Used by --dry-run,
# --print-config, and as the first half of a real install.
# --------------------------------------------------------------------------

@dataclass
class Plan:
    servers: list[str]
    venv_dir: Path
    python_exe: Path
    config_path: Path
    existing: dict                       # name -> existing mcpServers entry
    known_dirs: list[str]
    known_sessions: list[str]
    legacy_logs: list[str]
    sources: dict[str, Source]           # short name -> Source
    chosen_dir: Optional[Path]
    chosen_session: Optional[str]
    no_receipts: bool
    interactive: bool
    self_name: Optional[str] = None


def resolve_servers(args: argparse.Namespace, script_dir: Path) -> list[str]:
    if args.all:
        return list(SERVERS.keys())
    if args.servers:
        names = [s.strip() for s in args.servers.split(",") if s.strip()]
        bad = [n for n in names if n not in SERVERS]
        if bad:
            raise InstallError(
                f"Unknown server(s): {', '.join(bad)}. Known: {', '.join(sorted(SERVERS))}."
            )
        if not names:
            raise InstallError("--servers given but empty.")
        return names
    self_name = detect_self(script_dir)
    if self_name is None:
        raise InstallError(
            "Cannot tell which server to install: no pyproject.toml with a recognised "
            f"[project] name was found beside this script ({script_dir}).\n"
            f"Known repo names: {', '.join(sorted(m['repo'] for m in SERVERS.values()))}.\n"
            "Name them with --servers, or pass --all for the whole family."
        )
    return [self_name]


def is_interactive(args: argparse.Namespace) -> bool:
    if args.dry_run or args.print_config:
        # DEVIATION: install.ps1 prompts even under no special flag equivalent
        # to --dry-run (it has none). Both of our read-only modes are defined
        # as "touching nothing", so we never block on input() there.
        return False
    return sys.stdin is not None and sys.stdin.isatty() and sys.stdout.isatty()


def resolve_receipts(
    args: argparse.Namespace,
    known_dirs: list[str],
    known_sessions: list[str],
    interactive: bool,
) -> tuple[Optional[Path], Optional[str]]:
    if args.no_receipts:
        return None, None

    if args.receipts_dir:
        chosen_dir = args.receipts_dir
        # An explicit folder that differs from the one the family already uses
        # would fork the receipts: one chain per server is the design, one
        # folder per family is the deposit. Refuse unless told the fork is meant.
        wanted = str(Path(chosen_dir).expanduser().resolve())
        others = [d for d in known_dirs if str(Path(d).expanduser().resolve()) != wanted]
        if others and not args.force_receipts_dir:
            raise InstallError(
                f"--receipts-dir {chosen_dir} differs from the folder the registered servers "
                f"already use:\n  " + "\n  ".join(others)
                + "\nA family split across folders cannot be verified as one deposit. "
                "Re-run with --force-receipts-dir if the split is intended, or omit "
                "--receipts-dir to join the existing folder."
            )
    else:
        if len(known_dirs) > 1:
            raise InstallError(
                "The registered servers already use "
                f"{len(known_dirs)} different receipts folders:\n  "
                + "\n  ".join(known_dirs)
                + "\nPass --receipts-dir to say which this install should join."
            )
        suggested = known_dirs[0] if len(known_dirs) == 1 else str(default_receipts_dir())
        if interactive:
            print()
            print("  Every server writes its own hash-chained file into one folder.")
            print("  Put it somewhere you back up and would be willing to deposit.")
            print()
            answer = input(f"  Receipts folder [{suggested}]: ").strip()
            chosen_dir = answer.strip('"') if answer else suggested
        else:
            chosen_dir = suggested
            print(f"    Not interactive; using {chosen_dir}")

    chosen_dir_path = Path(chosen_dir).expanduser().resolve()

    if args.session:
        chosen_session = args.session
    elif len(known_sessions) == 1:
        chosen_session = known_sessions[0]
        print(f"    Session slug: {chosen_session} (from the registered servers)")
    elif len(known_sessions) > 1:
        raise InstallError(
            f"The registered servers use {len(known_sessions)} different session slugs:\n  "
            + "\n  ".join(known_sessions)
            + "\nThe slug groups a project's queries. Pass --session to say which."
        )
    elif interactive:
        answer = input("  Session slug (a project or article name): ").strip()
        chosen_session = answer or None
    else:
        chosen_session = None

    if not chosen_session:
        print("    No session slug. Lines will carry an empty label.")

    return chosen_dir_path, chosen_session


def plan(args: argparse.Namespace, script_dir: Optional[Path] = None) -> Plan:
    script_dir = script_dir or Path(__file__).resolve().parent
    servers = resolve_servers(args, script_dir)
    self_name = detect_self(script_dir)

    venv_dir = Path(args.venv).expanduser().resolve() if args.venv else default_venv_dir()
    python_exe = python_exe_in(venv_dir)

    cfg_path = Path(args.config_path).expanduser().resolve() if getattr(args, "config_path", None) else config_path()
    config = load_config(cfg_path)
    existing = dict(config.get("mcpServers") or {})
    known_dirs, known_sessions, legacy_logs = gather_known(existing)

    interactive = is_interactive(args)
    chosen_dir, chosen_session = resolve_receipts(args, known_dirs, known_sessions, interactive)

    sources = {
        name: get_source(SERVERS[name]["repo"], script_dir, getattr(args, "tag", None))
        for name in servers
    }

    return Plan(
        servers=servers,
        venv_dir=venv_dir,
        python_exe=python_exe,
        config_path=cfg_path,
        existing=existing,
        known_dirs=known_dirs,
        known_sessions=known_sessions,
        legacy_logs=legacy_logs,
        sources=sources,
        chosen_dir=chosen_dir,
        chosen_session=chosen_session,
        no_receipts=args.no_receipts,
        interactive=interactive,
        self_name=self_name,
    )


# --------------------------------------------------------------------------
# Pre-flight: refuse rather than half-install if a console script we would
# replace is locked open by a running Claude Desktop.
#
# DEVIATION: install.ps1 detects this on Windows by opening the .exe with
# FileShare.None and catching the sharing violation - a check with no direct
# POSIX equivalent (Linux/macOS let you overwrite an open executable; the
# running process keeps its old inode). We keep the same up-front, "refuse
# rather than repair" shape everywhere, but the underlying test differs by
# platform: on Windows we still use the same open-for-exclusive-access probe;
# on POSIX we treat it as inherently safe (nothing to detect) and skip it -
# an overwritten console-script file does not break an already-running
# process there.
# --------------------------------------------------------------------------

def check_locked(venv_dir: Path, servers: list[str]) -> list[str]:
    if platform.system() != "Windows":
        return []
    locked = []
    for name in servers:
        exe = exe_path_in(venv_dir, SERVERS[name]["cmd"])
        if not exe.exists():
            continue
        try:
            fd = os.open(str(exe), os.O_RDWR)
            os.close(fd)
        except OSError:
            locked.append(SERVERS[name]["cmd"])
    return locked


# --------------------------------------------------------------------------
# NDL notification step
# --------------------------------------------------------------------------

def ndl_step(args: argparse.Namespace, servers: list[str], sources: dict[str, Source],
             interactive: bool, dry_run: bool) -> None:
    if "ndl" not in servers:
        return
    step("NDL API notification")

    ndl_source = sources["ndl"]
    marker_file = Path(ndl_source.value) / "NDL-API-NOTIFICATION.txt" if ndl_source.kind == "local" else None

    if args.notification_filed:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", args.notification_filed):
            raise InstallError("--notification-filed must be YYYY-MM-DD.")
        if not marker_file:
            raise InstallError("--notification-filed needs a local ndl-mcp checkout to record the date into.")
        content = textwrap.dedent(f"""\
            NDL Search API - notification of continuous access
            Filed: {args.notification_filed}
            Form:  {FORM_URL}
            Terms: {TERMS_URL}
            Contact: di-api@ndl.go.jp

            Registered through the form described at section 17 of the NDL Search API help:
            contact details and nature of use. The library confirmed in August 2026 that this
            registration is no longer required, though still welcome. Providers used:
            iss-ndl-opac, iss-ndl-opac-national, zassaku, zassaku-online, ndl-dl-open - all
            NDL-created, none requiring a usage application for scholarly work.

            Undertakings given, and implemented in src/ndl_mcp/server.py:
              serial requests, no concurrency        -> _rate_lock held across each request
              minimum one-second interval            -> MIN_REQUEST_INTERVAL
              a cap on records per search            -> MAX_RECORDS, no auto-pagination
              no harvesting interface                -> OAI-PMH not implemented
              credit on every response               -> ATTRIBUTION + provider_credit()
              metadata displayed, not accumulated    -> no cache, no local store

            The undertakings above are kept because they are good practice toward a public
            service, not because a filing compels them. If the provider set or any undertaking
            changes, update this file so the record stays true.
            """)
        if dry_run:
            print(f"    Would record to {marker_file}")
        else:
            marker_file.write_text(content, encoding="utf-8")
            print(f"    Recorded to {marker_file}")
    elif marker_file and marker_file.is_file():
        print("    Registration on record:")
        with marker_file.open(encoding="utf-8") as fh:
            for i, line in zip(range(2), fh):
                print(f"      {line.rstrip()}")
    else:
        print()
        print("  Registering with the NDL is recommended, and not required.")
        print("  The library confirmed in August 2026 that notification of continuous")
        print("  use is no longer mandatory, though it remains welcome.")
        print()
        print("  Do it anyway. It takes a few minutes, it tells the library who is")
        print("  using the interface and for what, and a national library that can")
        print("  see researchers using its API has an argument for keeping it open.")
        print()
        print(f"  Form:  {FORM_URL}")
        print(f"  Terms: {TERMS_URL}")
        if interactive:
            answer = input("  Open the form now? [y/N]: ").strip()
            if answer[:1].lower() == "y":
                import webbrowser
                webbrowser.open(FORM_URL)
        print("  Continuing. Rerun with --notification-filed YYYY-MM-DD once registered.")


# --------------------------------------------------------------------------
# Install
# --------------------------------------------------------------------------

def ensure_venv(venv_dir: Path, python_version: str, dry_run: bool) -> Path:
    python_exe = python_exe_in(venv_dir)
    if python_exe.is_file():
        print("    Using the existing shared venv.")
        return python_exe
    if dry_run:
        print(f"    Would create venv at {venv_dir}")
        return python_exe
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([sys.executable, "-m", "venv", str(venv_dir)])
    if result.returncode != 0:
        raise InstallError(
            f"python -m venv failed for {venv_dir}. Install Python {python_version} "
            "or pass --venv to point at an existing environment."
        )
    print(f"    Created the shared venv at {venv_dir}.")
    return python_exe


def pip_install(python_exe: Path, target: str) -> None:
    result = subprocess.run([str(python_exe), "-m", "pip", "install", "--quiet", "--upgrade", target])
    if result.returncode != 0:
        raise InstallError(f"pip install failed for {target}.")


def pkg_version(python_exe: Path, pkg: str) -> str:
    result = subprocess.run(
        [str(python_exe), "-c", f"import {pkg} as p; print(p.__version__)"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise InstallError(f"Could not import {pkg} after install:\n{result.stderr}")
    return result.stdout.strip()


def install(plan_: Plan, dry_run: bool = False) -> dict[str, dict]:
    """Create the venv if needed and pip-install the planned servers into it.

    Returns {short_name: {"version": str, "exe": str}}.
    """
    step("Resolving Python")
    python_exe = ensure_venv(plan_.venv_dir, "3.13", dry_run)

    step("Checking that nothing we must replace is in use")
    locked = check_locked(plan_.venv_dir, plan_.servers)
    if locked:
        raise InstallError(
            "In use and not replaceable:\n  " + "\n  ".join(locked) +
            "\n\nClaude Desktop is running these servers and holds their launchers open."
            "\nQuit Claude Desktop entirely, then run this again."
            "\n\nNothing has been installed, uninstalled or reconfigured. Your current setup is untouched."
        )
    print("    nothing in use")

    step("Installing")
    installed: dict[str, dict] = {}
    if dry_run:
        for name in plan_.servers:
            m = SERVERS[name]
            src = plan_.sources[name]
            exe = exe_path_in(plan_.venv_dir, m["cmd"])
            print(f"    would pip install {m['dist']:<28} from {src.kind}:{src.value}")
            installed[name] = {"version": "(unknown - dry run)", "exe": str(exe)}
        return installed

    pip_install(python_exe, "pip")
    for name in plan_.servers:
        m = SERVERS[name]
        src = plan_.sources[name]
        pip_install(python_exe, src.value)
        ver = pkg_version(python_exe, m["pkg"])
        exe = exe_path_in(plan_.venv_dir, m["cmd"])
        if not exe.is_file():
            raise InstallError(f"{m['cmd']} console script not found at {exe} after install.")
        installed[name] = {"version": ver, "exe": str(exe)}
        print(f"    {m['dist']:<18} {ver:<8} {src.kind}")
    return installed


# --------------------------------------------------------------------------
# Verify: ledger.py and mediation.py byte-identical across every family
# package present in the venv (not just the ones this run installed).
# --------------------------------------------------------------------------

_PROBE_TEMPLATE = """\
import hashlib, importlib, pathlib
_names = {names!r}
for name in _names:
    pkg = importlib.import_module(name)
    srv = importlib.import_module(name + ".server")
    tools = sorted(srv.mcp._tool_manager._tools)
    line = "OK - %-24s %-8s %d tools" % (name, pkg.__version__, len(tools))
    try:
        med = importlib.import_module(name + ".mediation")
        line += "  ledger:%s" % ("reachable" if med.ledger_available() else "MISSING")
    except ModuleNotFoundError:
        importlib.import_module(name + ".ledger")
        line += "  ledger:reachable"
    print(line)
    if name == "ndl_mcp":
        assert srv.MIN_REQUEST_INTERVAL >= 1.0, "interval below what was filed with the NDL"
        assert srv.MAX_RECORDS <= 500, "record cap above the NDL ceiling"
        assert set(srv.ALL_DPIDS) <= set(srv.PROVIDERS), "undeclared provider"
        print("OK - ndl undertakings: interval %ss, cap %s records, providers declared"
              % (srv.MIN_REQUEST_INTERVAL, srv.MAX_RECORDS))

_FAMILY = {family!r}
_present = []
for _n in _FAMILY:
    try:
        importlib.import_module(_n); _present.append(_n)
    except ModuleNotFoundError:
        pass
_seen = {{}}
for name in _present:
    d = pathlib.Path(importlib.import_module(name).__file__).parent
    for fn in ("ledger.py", "mediation.py"):
        f = d / fn
        if f.exists():
            _seen.setdefault(fn, {{}}).setdefault(hashlib.sha256(f.read_bytes()).hexdigest(), []).append(name)
for fn, byhash in sorted(_seen.items()):
    if len(byhash) > 1:
        raise SystemExit("FAIL - %s differs between installed packages: %s"
                         % (fn, {{h[:12]: v for h, v in byhash.items()}}))
    if byhash:
        h, who = next(iter(byhash.items()))
        print("OK - %-14s identical across %d installed package(s): %s" % (fn, len(who), ", ".join(who)))
"""


def verify_identity(python_exe: Path, servers: list[str], dry_run: bool = False) -> None:
    """Run the post-install probe: import every installed server, and assert
    ledger.py / mediation.py are byte-identical across every family package
    present in the venv (gracefully skipping packages that lack
    mediation.py, exactly as the ps1 probe does)."""
    step("Verifying the installed packages")
    if dry_run:
        print("    (dry run - skipped)")
        return
    pkgs = [SERVERS[n]["pkg"] for n in servers]
    probe = _PROBE_TEMPLATE.format(names=pkgs, family=list(FAMILY_PKGS))
    import tempfile
    fd, probe_path = tempfile.mkstemp(prefix="mcp_family_probe_", suffix=".py")
    os.close(fd)
    try:
        Path(probe_path).write_text(probe, encoding="utf-8")
        result = subprocess.run([str(python_exe), probe_path], capture_output=True, text=True)
        if result.stdout:
            print(textwrap.indent(result.stdout.rstrip(), "    "))
        if result.returncode != 0:
            raise InstallError(f"Installed-package check failed.\n{result.stderr}")
    finally:
        try:
            os.remove(probe_path)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Receipts README
# --------------------------------------------------------------------------

def write_receipts_readme(chosen_dir: Path, servers: list[str], dry_run: bool = False) -> None:
    step("Writing the receipts folder README")
    any_cmd = SERVERS[servers[0]]["cmd"]
    readme = chosen_dir / "README.md"
    content = textwrap.dedent(f"""\
        # Query receipts

        Written by the bibliographic MCP server family. Every search these servers answer
        deposits one line here: the query as supplied, the term as normalised, its script,
        the parameters sent, a timestamp, a SHA-256 over query and parameters, and the
        identifiers of the records returned. Credentials are redacted before a line is
        composed. The records themselves are not held - logging a query is not
        accumulating a database.

        ## Layout

            <server>.jsonl    one append-only, hash-chained file per server
            manifest.json     written by `{any_cmd}-ledger manifest`; the thing to cite

        One file per server, and one writer per file. Appending is
        read-the-last-hash-then-write and the lock around it does not hold between
        processes, so several servers sharing one file will fork the chain. That is a
        configuration fault rather than tampering, and `verify` names it as such, but
        the layout exists so it cannot arise.

        ## Verifying

            {any_cmd}-ledger verify-dir  "{chosen_dir}"
            {any_cmd}-ledger manifest    "{chosen_dir}"
            {any_cmd}-ledger summary     "{chosen_dir}/<server>.jsonl"
            {any_cmd}-ledger csv         "{chosen_dir}/<server>.jsonl" out.csv

        `verify` exits non-zero if a chain does not verify, and distinguishes a fork
        (concurrent writers), a missing line, a reordering, and an edited line.

        ## Session slug

        Every line carries `MCP_RECEIPT_SESSION`. It groups a project's queries, so it
        should be the same across all six servers for one piece of research and changed
        deliberately when the research changes. It is written per line and cannot be
        corrected afterwards without breaking the chain.

        Configured {date.today().isoformat()} by install.py.
        """)
    if dry_run:
        print(f"    Would write {readme}")
        return
    chosen_dir.mkdir(parents=True, exist_ok=True)
    readme.write_text(content, encoding="utf-8")
    print(f"    {readme}")


# --------------------------------------------------------------------------
# Register with Claude Desktop
# --------------------------------------------------------------------------

def _prompt_credential(cred_name: str) -> Optional[str]:
    # DEVIATION: install.ps1 never actually prompts for a credential - it
    # only warns that a keyed tool will fail until one is set. The README
    # bundled with these repos promises the script "carr[ies] across
    # credentials already registered rather than asking again", which implies
    # it *does* ask the first time; the .ps1 code does not do this. The
    # porting brief asks for the promised behaviour, so this port adds a
    # one-time getpass prompt (never echoed) for a credential that is not yet
    # registered, when running interactively.
    try:
        value = getpass.getpass(f"  {cred_name} (leave blank to skip): ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return value.strip() or None


def build_registrations(
    plan_: Plan,
    installed: dict[str, dict],
    interactive: bool,
    prompt_for_missing: bool = True,
) -> dict[str, dict]:
    """Compute the mcpServers entries for plan_.servers. Prompts (once per
    distinct credential name across this run) for a credential that is
    neither already registered nor optional (EMAIL-suffixed), when
    interactive and prompt_for_missing. Returns only the entries for
    plan_.servers - servers not named in this run are left untouched by the
    caller, never returned here."""
    entries: dict[str, dict] = {}
    cred_cache: dict[str, str] = {}

    for name in plan_.servers:
        m = SERVERS[name]
        env_block: dict[str, str] = {}
        prior = plan_.existing.get(name) or {}
        prior_env = prior.get("env") if isinstance(prior, dict) else None
        prior_env = prior_env if isinstance(prior_env, dict) else {}

        missing = []
        for cred in m["creds"]:
            if prior_env.get(cred):
                env_block[cred] = str(prior_env[cred])
                continue
            is_optional = cred.endswith("EMAIL")
            if cred in cred_cache:
                env_block[cred] = cred_cache[cred]
                continue
            if interactive and prompt_for_missing and not is_optional:
                value = _prompt_credential(cred)
                if value:
                    env_block[cred] = value
                    cred_cache[cred] = value
                else:
                    missing.append(cred)
            elif not is_optional:
                missing.append(cred)
        if missing:
            warn(
                f"{name}: no {', '.join(missing)} registered; the server will install "
                "and its keyed tools will fail until one is set."
            )

        if plan_.chosen_dir:
            env_block["MCP_RECEIPT_DIR"] = str(plan_.chosen_dir)
        if plan_.chosen_session:
            env_block["MCP_RECEIPT_SESSION"] = plan_.chosen_session

        exe = installed.get(name, {}).get("exe") or str(exe_path_in(plan_.venv_dir, m["cmd"]))
        entry: dict = {"command": exe}
        if env_block:
            entry["env"] = env_block
        entries[name] = entry

    return entries


def register(
    plan_: Plan,
    installed: dict[str, dict],
    dry_run: bool = False,
) -> dict[str, dict]:
    """Update claude_desktop_config.json: back it up, carry forward every
    server not in plan_.servers untouched, and write/overwrite entries for
    plan_.servers. Returns the full new mcpServers mapping."""
    step("Updating Claude Desktop config")

    entries = build_registrations(plan_, installed, plan_.interactive)

    servers_hash = dict(plan_.existing)
    servers_hash.update(entries)

    if dry_run:
        print(f"    Would update {plan_.config_path}")
        return servers_hash

    if plan_.config_path.is_file():
        import datetime as _dt
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = plan_.config_path.with_name(plan_.config_path.name + f".{stamp}.bak")
        shutil.copy2(plan_.config_path, backup)
        print(f"    Backed up config to {backup}")

    full_config = load_config(plan_.config_path)
    full_config["mcpServers"] = servers_hash

    plan_.config_path.parent.mkdir(parents=True, exist_ok=True)
    plan_.config_path.write_text(
        json.dumps(full_config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return servers_hash


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="install.py",
        description="Install the bibliographic MCP server family and register it with Claude Desktop.",
    )
    p.add_argument("--servers", help="Comma-separated short names, e.g. cinii,ndl.")
    p.add_argument("--all", action="store_true", help="Install all six servers.")
    p.add_argument("--tag", help="Install from GitHub at this tag (e.g. v3.0.0) when no sibling checkout exists; default is main.")
    p.add_argument("--venv", help="Venv directory to install into (default: the family's shared venv).")
    p.add_argument("--receipts-dir", dest="receipts_dir", help="Receipts folder (MCP_RECEIPT_DIR).")
    p.add_argument("--force-receipts-dir", action="store_true",
                   help="allow --receipts-dir to differ from the folder already-registered servers use")
    p.add_argument("--session", help="Session/project slug (MCP_RECEIPT_SESSION).")
    p.add_argument("--no-receipts", action="store_true", help="Register without a receipts folder.")
    p.add_argument("--notification-filed", dest="notification_filed",
                    help="NDL notification date, YYYY-MM-DD (ndl only).")
    p.add_argument("--python-version", dest="python_version", default="3.13",
                    help="Informational only; used in error messages if venv creation fails.")
    p.add_argument("--config-path", dest="config_path",
                    help="Override claude_desktop_config.json path (mainly for testing).")
    p.add_argument("--dry-run", action="store_true", help="Print what would happen; touch nothing.")
    p.add_argument("--print-config", action="store_true",
                    help="Print the mcpServers entries this would write, and exit. Touches nothing.")
    return p


def _print_plan_summary(plan_: Plan) -> None:
    print(f"    Servers:  {', '.join(plan_.servers)}")
    print(f"    Venv:     {plan_.venv_dir}")
    print(f"    Config:   {plan_.config_path}")
    for name in plan_.servers:
        src = plan_.sources[name]
        print(f"      {name:<18} source={src.kind}:{src.value}")
    if plan_.no_receipts:
        print("    Receipts: disabled (--no-receipts)")
    elif plan_.chosen_dir:
        print(f"    Receipts dir:     {plan_.chosen_dir}")
        print(f"    Receipts session: {plan_.chosen_session or '(none)'}")
    if plan_.legacy_logs:
        print("    Legacy single-file logs on record (left alone):")
        for lg in plan_.legacy_logs:
            print(f"      {lg}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.all and args.servers:
        print("error: --all and --servers are mutually exclusive.", file=sys.stderr)
        return 2

    try:
        script_dir = Path(__file__).resolve().parent
        plan_ = plan(args, script_dir)

        if args.print_config:
            entries = build_registrations(plan_, installed={}, interactive=False, prompt_for_missing=False)
            print(json.dumps({"mcpServers": entries}, indent=2, ensure_ascii=False))
            return 0

        step("Reading the current Claude Desktop configuration")
        if plan_.config_path.is_file():
            print(f"    Registered now: {', '.join(sorted(plan_.existing)) or '(none)'}")
        else:
            print("    No config yet; one will be created.")

        step("Plan")
        _print_plan_summary(plan_)

        ndl_step(args, plan_.servers, plan_.sources, plan_.interactive, args.dry_run)

        installed = install(plan_, dry_run=args.dry_run)

        verify_identity(plan_.python_exe, plan_.servers, dry_run=args.dry_run)

        if plan_.chosen_dir:
            write_receipts_readme(plan_.chosen_dir, plan_.servers, dry_run=args.dry_run)

        servers_hash = register(plan_, installed, dry_run=args.dry_run)

        step("Done")
        print()
        label = "Would install and register" if args.dry_run else "Installed and registered"
        print(f"{label}:")
        for name in plan_.servers:
            info = installed.get(name, {})
            print(f"  {name:<18} {info.get('version', ''):<8} {info.get('exe', '')}")
        print()
        if plan_.chosen_dir:
            print("Receipts:")
            print(f"  folder : {plan_.chosen_dir}")
            print(f"  session: {plan_.chosen_session or '(none - lines will carry an empty label)'}")
            print(f"  verify : {SERVERS[plan_.servers[0]]['cmd']}-ledger verify-dir \"{plan_.chosen_dir}\"")
        else:
            print("Receipts: NOT DEPOSITED - no MCP_RECEIPT_DIR set.")
            print("          Searches will run and leave no record.")
        if plan_.legacy_logs:
            print()
            print("A previous single-file log was registered and is no longer written to:")
            for lg in plan_.legacy_logs:
                print(f"  {lg}")
            print("  Nothing has been moved or deleted. Keep it with the new folder if its")
            print("  lines belong to the same research; a chain is per-file and the two do")
            print("  not join.")
        print()
        print("mcpServers now in config:")
        for name in sorted(servers_hash):
            print(f"  - {name}")
        print()
        if args.dry_run:
            print("Dry run: nothing was installed or written.")
        else:
            print("Restart Claude Desktop to load them.")
        return 0

    except InstallError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
