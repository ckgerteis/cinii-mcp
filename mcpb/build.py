#!/usr/bin/env python3
"""Build a Claude Desktop .mcpb bundle for this server; check vendored-file identity; cut release notes.

Vendored byte-identical into every repository of the family. It reads what it
needs from pyproject.toml and mcpb/manifest.template.json, so the file itself
carries nothing server-specific.

    python mcpb/build.py --platform win32-x64        # build/<platform>/ then dist/<name>-<version>-<platform>.mcpb
    python mcpb/build.py --check-identity            # vendored family files match VENDORED.sha256
    python mcpb/build.py --write-identity            # regenerate VENDORED.sha256 (do this in all six at once)
    python mcpb/build.py --release-notes v3.0.0      # print that version's CHANGELOG section

Why one bundle per platform: pydantic-core ships as a native wheel, so the
libraries vendored under server/lib are platform-specific. The release
workflow runs this once per OS in a matrix and attaches all of them.

Why the bundle still needs Python: MCPB carries no interpreter for Python
servers. Claude Desktop launches `python` (Windows) or `python3` (macOS,
Linux) from PATH; the manifest declares runtimes.python >= 3.10.
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
PLATFORMS = {
    "win32-x64": ("win32", "python"),
    "win32-arm64": ("win32", "python"),
    "darwin-arm64": ("darwin", "python3"),
    "darwin-x64": ("darwin", "python3"),
    "linux-x64": ("linux", "python3"),
    "linux-arm64": ("linux", "python3"),
}
# Files that must be byte-identical across the family. Paths relative to the
# repo root; the package-relative ones are resolved through pyproject.
VENDORED = [
    "response-schema.json",
    "install.ps1",
    "install.py",
    "mcpb/build.py",
    "tests/smoke_stdio.py",
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
    (ROOT / "VENDORED.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
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

def build(platform: str, python_exe: str | None) -> Path:
    if platform not in PLATFORMS:
        sys.exit(f"unknown platform {platform}; one of {', '.join(PLATFORMS)}")
    os_name, command = PLATFORMS[platform]
    proj = _pyproject()
    name, version = proj["name"], proj["version"]
    template = json.loads((ROOT / "mcpb" / "manifest.template.json").read_text(encoding="utf-8"))
    if template["version"] != version:
        sys.exit(f"manifest.template.json says {template['version']}, pyproject says {version}")

    out = ROOT / "build" / platform
    if out.exists():
        shutil.rmtree(out)
    lib = out / "server" / "lib"
    lib.mkdir(parents=True)

    # Vendor the package and every dependency for this interpreter/platform.
    py = python_exe or sys.executable
    subprocess.run(
        [py, "-m", "pip", "install", "--quiet", "--no-compile", "--target", str(lib), str(ROOT)],
        check=True,
    )
    # Console-script shims and dist-info are not needed at runtime; leave
    # dist-info (harmless, records versions) and drop bin/.
    for junk in ("bin", "Scripts"):
        shutil.rmtree(lib / junk, ignore_errors=True)
    shutil.copy(ROOT / "mcpb" / "main.py", out / "server" / "main.py")
    for extra in ("response-schema.json", "LICENSE", "README.md", "CHANGELOG.md"):
        if (ROOT / extra).exists():
            shutil.copy(ROOT / extra, out / extra)

    manifest = json.loads(json.dumps(template).replace("__PYTHON__", command).replace("__PLATFORM__", os_name))
    manifest["compatibility"]["platforms"] = [os_name]
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    bundle = dist / f"{name}-{version}-{platform}.mcpb"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                z.write(p, p.relative_to(out).as_posix())
    size = bundle.stat().st_size // 1024
    print(f"built {bundle.relative_to(ROOT)} ({size} KB) for {platform}: command={command}")
    return bundle


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--platform", choices=sorted(PLATFORMS))
    ap.add_argument("--python", help="interpreter whose pip vendors the libraries (default: this one)")
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
    if a.platform:
        build(a.platform, a.python)
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
