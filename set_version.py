#!/usr/bin/env python3
"""Set the release version in every file that carries one.

    python set_version.py 1.2.2
    python set_version.py 1.2.2 --date 2026-09-15   # default is today, UTC
    python set_version.py --check                   # report, change nothing

Five places carry a version number and they must agree: pyproject.toml, which is
the source; mcpb/manifest.template.json, which mcpb/build.py refuses to build
against a mismatch; .zenodo.json, which Zenodo reads at the tagged commit when
it mints the DOI; CITATION.cff, which GitHub parses for the "Cite this
repository" button; and `__version__` in the package under src/, which the
server reports when a client connects. Editing them by hand is how ndl-mcp
v1.2.1 shipped with a CITATION.cff naming a release that did not exist; leaving
`__version__` out is how the 12 October 2026 bumps failed the bundle handshake,
whose server answered with the old version.

Run this, read the diff, commit, then tag. The tag is what fires the release
workflow, so the metadata must already be right in the commit the tag points at
— which is why this is a step before tagging rather than a job inside CI.

Edits are textual and anchored to line starts, so nothing else in the files
moves: no JSON reflow, no reordered YAML, a diff you can read in full.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")

# file, description, pattern with one capturing group for the value
TARGETS = [
    ("pyproject.toml", "the source", r'(?m)^(version\s*=\s*")([^"]+)(")'),
    ("mcpb/manifest.template.json", "the bundle manifest", r'(?m)^(\s*"version"\s*:\s*")([^"]+)(")'),
    (".zenodo.json", "the Zenodo deposit", r'(?m)^(\s*"version"\s*:\s*")([^"]+)(")'),
    ("CITATION.cff", "the citation file", r'(?m)^(version:\s*"?)([^"\n]+?)("?\s*)$'),
]

DATE_TARGET = ("CITATION.cff", r'(?m)^(date-released:\s*")([^"]+)("\s*)$')

SOURCE_PATTERN = r'(?m)^(__version__\s*=\s*")([^"]+)(")'


def _source_targets() -> list[tuple[str, str, str]]:
    """The module under src/ that declares __version__. Found rather than named,
    because the package name differs in each repository."""
    found = [p for p in sorted((ROOT / "src").glob("*/*.py"))
             if re.search(SOURCE_PATTERN, p.read_text(encoding="utf-8"))]
    return [(p.relative_to(ROOT).as_posix(), "the version the server reports", SOURCE_PATTERN)
            for p in found]


def _sub_once(path: Path, pattern: str, value: str) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        sys.exit(f"{path.name}: expected exactly one match, found {len(matches)}. Not edited.")
    old = matches[0].group(2)
    new_text = text[: matches[0].start()] + matches[0].group(1) + value + matches[0].group(3) + text[matches[0].end():]
    if new_text != text:
        path.write_text(new_text, encoding="utf-8", newline="")
    return old, value


def _read_once(path: Path, pattern: str) -> str:
    matches = list(re.finditer(pattern, path.read_text(encoding="utf-8")))
    if len(matches) != 1:
        sys.exit(f"{path.name}: expected exactly one match, found {len(matches)}.")
    return matches[0].group(2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("version", nargs="?", help="the new version, e.g. 1.2.2")
    ap.add_argument("--date", help="release date as YYYY-MM-DD (default: today, UTC)")
    ap.add_argument("--check", action="store_true", help="report the current values and exit")
    args = ap.parse_args()

    present = [(name, why, pat) for name, why, pat in TARGETS if (ROOT / name).is_file()]
    present += _source_targets()
    missing = [name for name, _, _ in TARGETS if not (ROOT / name).is_file()]

    if args.check:
        for name, why, pat in present:
            print(f"{_read_once(ROOT / name, pat):<12} {name}  ({why})")
        for name in missing:
            print(f"{'—':<12} {name}  (not in this repository)")
        values = {_read_once(ROOT / name, pat) for name, _, pat in present}
        if len(values) != 1:
            print("\nThese disagree. Run this script with the correct version to settle them.")
            return 1
        print("\nAll agree.")
        return 0

    if not args.version:
        ap.error("give a version, or --check")
    if not SEMVER.match(args.version):
        ap.error(f"{args.version!r} is not a version of the form X.Y.Z")

    date = args.date or _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    _dt.date.fromisoformat(date)

    for name, why, pat in present:
        old, new = _sub_once(ROOT / name, pat, args.version)
        print(f"{name}: {old} -> {new}" if old != new else f"{name}: already {new}")
    for name in missing:
        print(f"{name}: not in this repository, skipped")

    cff = ROOT / DATE_TARGET[0]
    if cff.is_file():
        old, new = _sub_once(cff, DATE_TARGET[1], date)
        print(f"{DATE_TARGET[0]}: date-released {old} -> {new}" if old != new else f"{DATE_TARGET[0]}: date already {new}")

    print(
        f"\nNow: review the diff, add a {args.version} section to CHANGELOG.md, "
        f"commit, then `git tag v{args.version} && git push --tags`."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
