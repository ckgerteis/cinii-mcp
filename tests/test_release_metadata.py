"""Every file that carries a version number must carry the same one.

pyproject.toml is the source. mcpb/build.py already refuses to build when
manifest.template.json disagrees with it, so that pair is covered. This test
covers the two files nothing checked: .zenodo.json, which Zenodo reads at the
tagged commit when it mints the DOI, and CITATION.cff, which GitHub parses for
the "Cite this repository" button.

Both are maintained by hand, and on 2026-09-08 ndl-mcp released v1.2.1 with
`version: 1.2.0` and `cff-version: 1.2.1` in CITATION.cff — the two numbers
transposed. The consequences were not symmetrical. The stale `version` offered
a release that was never made; the invalid `cff-version` named a specification
that has never existed, which is enough for the citation widget to refuse the
file outright. Neither could be seen from the release pipeline, because nothing
read either file.

Running here rather than only in the release workflow is deliberate: this
executes on every push and pull request as well as on the tagged commit, so the
mismatch is caught when it is introduced rather than when it ships.

Stdlib only, and textual rather than parsed. Adding a YAML dependency to seven
repositories to read three scalars would cost more than it verifies, and the
regexes below are anchored to line starts, so they read top-level keys only.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The current released Citation File Format specification. If the CFF project
# releases a later one, upgrading is a deliberate edit here and in the file,
# not something that should drift in unnoticed.
CFF_SPEC_VERSION = "1.2.0"


def _one(pattern: str, text: str, what: str, where: str) -> str:
    matches = re.findall(pattern, text, re.MULTILINE)
    if not matches:
        raise AssertionError(f"{where}: no {what} found")
    if len(matches) > 1:
        raise AssertionError(f"{where}: {what} appears {len(matches)} times, expected once")
    return matches[0].strip()


def _read(name: str) -> str:
    path = ROOT / name
    assert path.is_file(), f"{name} is missing from the repository root"
    return path.read_text(encoding="utf-8")


def project_version() -> str:
    return _one(r'^version\s*=\s*"([^"]+)"', _read("pyproject.toml"), "version", "pyproject.toml")


def test_zenodo_version_matches_pyproject() -> None:
    """Zenodo reads this file at the tagged commit; a stale value is deposited."""
    zenodo = _one(r'^\s*"version"\s*:\s*"([^"]+)"', _read(".zenodo.json"), "version", ".zenodo.json")
    assert zenodo == project_version(), (
        f".zenodo.json says {zenodo}, pyproject says {project_version()}. "
        "The deposit would carry the wrong version."
    )


def test_citation_version_matches_pyproject() -> None:
    """GitHub's citation widget reads this file; a stale value is what people cite."""
    cff = _one(r'^version:\s*"?([^"\n]+)"?\s*$', _read("CITATION.cff"), "version", "CITATION.cff")
    assert cff == project_version(), (
        f"CITATION.cff says {cff}, pyproject says {project_version()}. "
        "Anyone citing from the repository page would name a release that does not exist."
    )


def test_citation_declares_a_released_specification() -> None:
    """cff-version names the format specification, not this software's version."""
    spec = _one(r'^cff-version:\s*"?([^"\n]+)"?\s*$', _read("CITATION.cff"), "cff-version", "CITATION.cff")
    assert spec == CFF_SPEC_VERSION, (
        f"cff-version is {spec}; the released specification is {CFF_SPEC_VERSION}. "
        "An unrecognised value stops GitHub rendering the citation at all."
    )


def test_citation_release_date_is_a_date() -> None:
    raw = _one(r'^date-released:\s*"?([^"\n]+)"?\s*$', _read("CITATION.cff"), "date-released", "CITATION.cff")
    _dt.date.fromisoformat(raw)  # raises ValueError on anything that is not YYYY-MM-DD
