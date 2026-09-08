"""Build the Claude Desktop bundle (.mcpb) for this server.

    python mcpb/build.py                              # one bundle: dist/<name>-<version>.mcpb
    python mcpb/build.py --check-identity             # vendored files match VENDORED.sha256
    python mcpb/build.py --write-identity             # regenerate VENDORED.sha256
    python mcpb/build.py --release-notes v3.0.0       # print that version's CHANGELOG section

The bundle vendors no libraries. Its manifest declares server.type "uv"
(manifest 0.4), so Claude Desktop runs it with uv: a uv already on the PATH if
there is one, otherwise the uv the app ships and, failing that, one it
downloads. uv reads server/pyproject.toml and server/uv.lock, uses an
interpreter already on the machine that satisfies requires-python, downloads
one only where there is none, and installs the locked dependencies on first
launch. One bundle serves every
platform, because nothing in it is compiled.

Why this replaced the vendored-lib bundle: `pip install --target` vendors
native wheels (pydantic-core, rpds-py, cffi) tagged for the interpreter that
runs the build, and the release workflow pinned that to CPython 3.12 while the
manifest promised >=3.10 and launched whatever `python` the user's PATH held.
Every published bundle imported under 3.12 alone and failed silently elsewhere
("Server disconnected"). Locking rather than vendoring removes the ABI
coupling instead of multiplying it.

The lock is written at build time by the uv that builds, so the bundle pins
what CI resolved on the release day; tests/bundle_handshake.py then runs the
built bundle under interpreters other than the pinned one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
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
# No .python-version in the bundle. uv then takes any installed interpreter
# that satisfies pyproject's requires-python and downloads one only when none
# exists, so most first launches skip the interpreter download. The lock
# resolves for every version the range allows, and tests/bundle_handshake.py
# proves the bundle under several of them before each release.
# Files that must be byte-identical across the family. Paths relative to the
# repo root; the package-relative ones are resolved through pyproject.
VENDORED = [
    "response-schema.json",
    "install.ps1",
    "install.py",
    "mcpb/build.py",
    "tests/smoke_stdio.py",
    "tests/bundle_handshake.py",
    "{pkg}/ledger.py",
    "{pkg}/mediation.py",
]


def _pyproject() -> dict:
    return _load_project((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _package_dir() -> Path:
    src = ROOT / "src"
    pkgs = [p for p in src.iterdir() if p.is_dir() and (p / "__init__.py").exists()]
    if len(pkgs) != 1:
        sys.exit(f"expected one package under src/, found {[p.name for p in pkgs]}")
    return pkgs[0]


def _sha(path: Path) -> str:
    """sha256 over LF-normalised bytes: a Windows checkout with autocrlf on
    rewrites line endings, and identity is a claim about content, not about
    the platform the file was checked out on."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _vendored_paths() -> list[tuple[str, Path]]:
    pkg = _package_dir()
    out = []
    for rel in VENDORED:
        if "{pkg}" in rel:
            out.append((rel, pkg / rel.split("/", 1)[1]))
        else:
            out.append((rel, ROOT / rel))
    return out


# ---------------------------------------------------------------- identity

def write_identity() -> None:
    lines = []
    for rel, p in _vendored_paths():
        if p.exists():
            lines.append(f"{_sha(p)}  {rel}")
    (ROOT / "VENDORED.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote VENDORED.sha256 ({len(lines)} files)")


def check_identity() -> int:
    ref = ROOT / "VENDORED.sha256"
    if not ref.exists():
        print("VENDORED.sha256 missing; run --write-identity in every repository of the family")
        return 1
    want = {}
    for line in ref.read_text(encoding="utf-8").splitlines():
        if line.strip():
            h, rel = line.split(None, 1)
            want[rel.strip()] = h
    bad = 0
    for rel, p in _vendored_paths():
        if rel not in want:
            continue
        if not p.exists():
            print(f"MISSING  {rel}")
            bad += 1
        elif _sha(p) != want[rel]:
            print(f"DIFFERS  {rel}")
            bad += 1
        else:
            print(f"ok       {rel}")
    if bad:
        print(f"{bad} vendored file(s) differ from VENDORED.sha256. Either this copy drifted, or the "
              "family moved on and this repository was not updated with it. Sync and --write-identity in all six.")
    return 1 if bad else 0


# ---------------------------------------------------------------- release notes

def release_notes(tag: str) -> str:
    version = tag.lstrip("v")
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(version)}\b.*?$(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not m:
        return f"See CHANGELOG.md for {version}."
    body = m.group(1).strip()
    # The "Not released" paragraph is true until the moment this runs; drop it.
    body = re.sub(r"\*\*Not released\.\*\*.*?(?=\n\n)", "", body, count=1, flags=re.S).strip()
    return body + "\n"


# ---------------------------------------------------------------- bundle

def _uv(explicit: str | None) -> str:
    uv = explicit or os.environ.get("UV") or shutil.which("uv")
    if not uv:
        sys.exit("uv is needed to write the bundle's lock file; install it "
                 "(https://docs.astral.sh/uv/) or pass --uv PATH")
    return uv


def build(uv_exe: str | None) -> Path:
    proj = _pyproject()
    name, version = proj["name"], proj["version"]
    template = json.loads((ROOT / "mcpb" / "manifest.template.json").read_text(encoding="utf-8"))
    if template["version"] != version:
        sys.exit(f"manifest.template.json says {template['version']}, pyproject says {version}")
    if template["server"]["type"] != "uv":
        sys.exit('manifest.template.json must declare server.type "uv"; this build vendors nothing')

    out = ROOT / "build" / "bundle"
    if out.exists():
        shutil.rmtree(out)
    server = out / "server"
    server.mkdir(parents=True)

    # The project, as uv will see it: sources, metadata, and the files
    # pyproject refers to. No lib tree, no venv.
    shutil.copytree(ROOT / "src", server / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy(ROOT / f, server / f)
    shutil.copy(ROOT / "mcpb" / "main.py", server / "main.py")
    subprocess.run([_uv(uv_exe), "lock", "--directory", str(server)], check=True)
    if not (server / "uv.lock").exists():
        sys.exit("uv lock wrote no uv.lock")

    for extra in ("response-schema.json", "LICENSE", "README.md", "CHANGELOG.md"):
        if (ROOT / extra).exists():
            shutil.copy(ROOT / extra, out / extra)
    (out / "manifest.json").write_text(json.dumps(template, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    bundle = dist / f"{name}-{version}.mcpb"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and ".venv" not in p.parts:
                z.write(p, p.relative_to(out).as_posix())
    size = bundle.stat().st_size // 1024
    print(f"built {bundle.relative_to(ROOT)} ({size} KB): server.type=uv, python: any that satisfies requires-python, "
          f"platforms {', '.join(template['compatibility']['platforms'])}")
    return bundle


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uv", help="uv executable that writes the lock (default: UV env var, then PATH)")
    ap.add_argument("--check-identity", action="store_true")
    ap.add_argument("--write-identity", action="store_true")
    ap.add_argument("--release-notes", metavar="TAG")
    a = ap.parse_args()
    if a.write_identity:
        write_identity()
        return 0
    if a.check_identity:
        return check_identity()
    if a.release_notes:
        sys.stdout.write(release_notes(a.release_notes))
        return 0
    build(a.uv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
