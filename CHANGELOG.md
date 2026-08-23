# Changelog

Versions are the thing to cite. A count produced under one release is not
reproducible against another, so the release actually used should be named in
the text and, where a version DOI exists, cited by it.

Releases earlier than those below are on the repository's releases page; this
file begins where the record is precise enough to be worth writing down.

## 3.0.0 — 2026-08-23

**Not released.** No tag was cut and no Zenodo record exists for this version, so
it is citable by commit alone. Tagging waits on confirmation that this
repository's Zenodo webhook is live: a release that mints nothing spends a
version number and returns nothing citable for it.

- **`src/` layout. Breaking: the server is started by console script, not by
  path.** `server.py`, `mediation.py` and `ledger.py` move to `src/cinii_mcp/` and install as a
  package. The flat layout installed them as *top-level* modules, so any two
  servers of this family in one environment overwrote each other — and
  `pip check` reported nothing wrong. The later install simply won, silently,
  and the survivor answered under the wrong server's name. All six now coexist:
  verified by installing every wheel into one environment and driving each
  through `initialize` and `tools/list`.
- **Claude Desktop entries must change.** Replace
  `"command": "…\\python.exe", "args": ["…\\server.py"]` with
  `"command": "…\\Scripts\\cinii-mcp.exe"`. An existing entry keeps working
  against an existing flat deployment and will fail against this one.
- `python -m cinii_mcp` and a `cinii-mcp-ledger` console script are installed
  alongside it.
- **The server reports its build.** `initialize` was answered with an empty
  `serverInfo.version`. It now carries `__version__` where the SDK accepts one
  (mcp 2.x `MCPServer`). Under mcp 1.x, whose `FastMCP` takes no `version`, the
  field still reports the SDK's version rather than the server's — the argument
  is passed only where it is accepted.

## 2.3.0 — 2026-08-22

- `mediation.py` 2.3.0. `emit()` now reports whether the deposit happened:
  `RECEIPT_NOT_DEPOSITED` (info) when `MCP_RECEIPT_LOG` is unset,
  `RECEIPT_WRITE_FAILED` (warning) when it is set and the write did not land.
  `deposit_enabled()` exposed beside `ledger_available()`.
- Additive. No field removed or renamed; `response-schema.json` unchanged.

## 2.2.0 — 2026-08-21

- `mediation.py` unified at 2.2.0 and vendored byte-identically across the
  server family: Hangul and CJK supplementary-plane script detection, ledger
  persistence via `emit()`, and the `searched_for` headline restored.
- `response-schema.json` published, with a Response format section in the README.
