# Contributing to cinii-mcp

Issues and pull requests are welcome. This file says what a change needs to carry so that it can be
merged without a second round.

## Before you start

- **Report the fault, not the fix, first** if you are unsure. An issue with the request that failed,
  the envelope or log you got back, and the Python version takes five minutes and often settles the
  design before any code is written.
- **Read the README's "Response format" and "Receipts" sections.** Every tool answers with one typed
  envelope, and every query-answering tool deposits a receipt. A change that returns something
  outside the envelope, or that answers a query without a receipt, will be asked to change.
- **The server does not summarise, rank by relevance of its own, or invent.** It reports what the
  source returned and how it matched. That is the point of the project; changes that add
  interpretation on the server side are out of scope.

## Setting up

```bash
git clone https://github.com/ckgerteis/cinii-mcp
cd cinii-mcp
python -m venv .venv
.venv/bin/pip install -e ".[test]"        # Windows: .venv\Scripts\pip
```

Python 3.10 or later; 3.10, 3.12, 3.13 and 3.14 are what CI runs, on Windows, macOS and Linux.

## Running the checks

```bash
python -m pytest -q tests
python tests/smoke_stdio.py               # starts the console script, handshake, tools/list vs README
python mcpb/build.py --check-identity   # vendored files match VENDORED.sha256
```

`smoke_stdio.py` compares `tools/list` with the table in the README, so a new tool is a README
change too. With `RUN_LIVE=1` and a tool name and JSON parameters it also makes one live call and
reports the envelope's diagnostic codes.

### The Claude Desktop bundle

`python mcpb/build.py` writes `dist/cinii-mcp-<version>.mcpb`. The bundle vendors nothing: its manifest
declares `server.type: "uv"`, and Claude Desktop runs it with uv from the `pyproject.toml`,
`uv.lock` inside. Two checks gate it, and both must pass before a change to
`mcpb/` is merged:

```bash
npx --yes @anthropic-ai/mcpb validate build/bundle/manifest.json
python tests/bundle_handshake.py dist/*.mcpb --cold
python tests/bundle_handshake.py dist/*.mcpb --python 3.10
```

`mcpb validate` checks the manifest. The handshake gate unpacks the bundle, runs it the way the host
would, and requires the `initialize` reply to name the manifest's version, on an empty uv cache and
under interpreters other than the bundle's pin. A bundle that builds is not a bundle that runs.

### Files vendored across the family

These files are byte-identical in all six server repositories and must stay so:
`response-schema.json`, `install.ps1`, `install.py`, `mcpb/build.py`, `tests/smoke_stdio.py`,
`tests/bundle_handshake.py`, `src/<pkg>/ledger.py`, `src/<pkg>/mediation.py`. If your change touches
one of them, say so in the pull request: it has to land in all six, and each repository's CI checks
its copies against `VENDORED.sha256` (`python mcpb/build.py --check-identity`). A change to the
envelope (`mediation.py`, `response-schema.json`) is a change to what every server promises its
consumers and needs a schema version bump.

## What a pull request should carry

- One change per pull request, described in the first line of the description in terms of what a
  user of the server sees differently.
- A `CHANGELOG.md` line under the version at the top of the file. Entries are written for the
  reader who has to decide whether to upgrade: say what was wrong, what changed, and what it costs.
- Tests where the change is testable without the network: `tests/` uses captured responses under
  `tests/fixtures`, and a parser change should come with the response that exposed it.
- No credentials, no paths from your machine, and no `.env`. `.env.example` shows the variable
  names.

## Versions and releases

Releases are tags cut by the maintainer; contributors do not bump versions. A version touches
`pyproject.toml`, `.zenodo.json`, `CITATION.cff`, `mcpb/manifest.template.json`, the
server module's header and `__version__`, the README's pinned install lines and `CHANGELOG.md`, and
the release workflow attaches the wheel, the sdist and the bundle to a GitHub
release that Zenodo archives under the concept DOI in the README.

## Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Security-relevant reports go
by the route in [SECURITY.md](SECURITY.md), not to the public tracker.
