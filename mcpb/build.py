"""Build the Claude Desktop bundle (.mcpb) for this server.

    python mcpb/build.py                              # one bundle: dist/<name>-<version>.mcpb
    python mcpb/build.py --check-identity             # vendored files match VENDORED.sha256
    python mcpb/build.py --write-identity             # regenerate VENDORED.sha256
    python mcpb/build.py --release-notes v3.0.0       # print that version's CHANGELOG section

The bundle vendors no libraries. Its manifest declares server.type "uv"
(manifest 0.4), and its layout is the one Claude Desktop's UV runtime expects:

    manifest.json
    pyproject.toml        <- at the root: this is what the host looks for
    uv.lock
    main.py               <- the entry point
    src/<package>/...
    README.md, LICENSE, CHANGELOG.md, response-schema.json

Claude Desktop does two things with a uv-type bundle, and the layout matters
for both. At install time it looks for pyproject.toml at the bundle root and,
if it is there, takes a uv already on the PATH or downloads its own (0.9.7 at
the time of writing, into the app's uv-runtime folder), then runs `uv sync` in the bundle folder with a progress bar: that is when the
interpreter and the locked libraries are fetched. If pyproject.toml is not at
the root, the app logs "missing pyproject.toml. Cannot proceed with UV setup"
and installs the extension anyway, with nothing provisioned. At launch time
it runs its own uv with the manifest's args and the bundle folder as the
working directory.

The bundles before this layout kept pyproject.toml under server/, so the
install-time step was skipped on every machine and the first connection
attempt had to download uv, an interpreter and ~40 MB of libraries inside the
host's connection window: on the author's connection that took 26-46 s
against a 60 s limit, and on slower or filtered networks it never completed
("Unable to connect to extension server", reported from a Mac mini on macOS
26 on 7 September 2026). With pyproject.toml at the root the environment is
built during installation and the launch reuses it (under a second, measured).
The manifest's `uv run --frozen` still builds the environment itself where a
host skipped the install-time step, so an older host is slower, not broken.

Why uv replaced the vendored-lib bundle: `pip install --target` vendors
native wheels (pydantic-core, rpds-py, cffi) tagged for the interpreter that
runs the build, and the release workflow pinned that to CPython 3.12 while the
manifest promised >=3.10 and launched whatever `python` the user's PATH held.
Every published bundle imported under 3.12 alone and failed silently elsewhere
("Server disconnected"). Locking rather than vendoring removes the ABI
coupling instead of multiplying it.

The lock is written at build time by the uv that builds, so the bundle pins
what CI resolved on the release day; tests/bundle_handshake.py then installs
and launches the built bundle the way the host does, under interpreters other
than the one that built it.
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
    if template["server"]["entry_point"] != "main.py":
        sys.exit('manifest.template.json must set entry_point "main.py": the entry point sits beside '
                 "pyproject.toml at the bundle root, where Claude Desktop's UV runtime looks")

    out = ROOT / "build" / "bundle"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # The project, as uv will see it from the bundle root: sources, metadata,
    # the files pyproject refers to, and the entry point. No lib tree, no venv.
    shutil.copytree(ROOT / "src", out / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in ("pyproject.toml", "README.md", "LICENSE", "CHANGELOG.md", "response-schema.json"):
        if (ROOT / f).exists():
            shutil.copy(ROOT / f, out / f)
    shutil.copy(ROOT / "mcpb" / "main.py", out / "main.py")
    subprocess.run([_uv(uv_exe), "lock", "--directory", str(out)], check=True)
    if not (out / "uv.lock").exists():
        sys.exit("uv lock wrote no uv.lock")

    (out / "manifest.json").write_text(json.dumps(template, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    bundle = dist / f"{name}-{version}.mcpb"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and ".venv" not in p.parts:
                z.write(p, p.relative_to(out).as_posix())
    size = bundle.stat().st_size // 1024
    print(f"built {bundle.relative_to(ROOT)} ({size} KB): server.type=uv, pyproject.toml at the bundle root, "
          f"python: any that satisfies requires-python, platforms {', '.join(template['compatibility']['platforms'])}")
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
