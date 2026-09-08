# Changelog

Versions are the thing to cite. A count produced under one release is not
reproducible against another, so the release actually used should be named in
the text and, where a version DOI exists, cited by it.

Releases earlier than those below are on the repository's releases page; this
file begins where the record is precise enough to be worth writing down.

## 3.1.0 — 2026-09-08

**The Claude Desktop bundle runs again, on every supported interpreter.** The
same change as ndl-mcp 1.2.0, where it was proved before being copied here.

- **What was wrong.** Every `.mcpb` published so far imported under CPython
  3.12 and nothing else. `mcpb/build.py` vendored the dependencies with
  `pip install --target` under the interpreter running the build, the release
  workflow pinned that interpreter to 3.12, and `pydantic-core`, `rpds-py` and
  `cffi` ship native wheels tagged for one interpreter. The manifest meanwhile
  declared `runtimes.python >= 3.10` and launched bare `python` from the
  user's PATH, so Claude Desktop picked whatever satisfied the range, the
  import failed at module scope, and the user saw "Server disconnected".
- **What changed.** The manifest now declares `server.type: "uv"` (manifest
  0.4). Claude Desktop runs the bundle with uv, using a uv already on the
  PATH and otherwise the copy the app ships, from `server/pyproject.toml`,
  `server/.python-version` (3.13) and `server/uv.lock`. Nothing compiled is
  in the bundle, so one bundle serves Windows, macOS and Linux and is about
  100 KB instead of 10 to 17 MB. The first launch downloads Python 3.13 if the
  machine lacks it and the locked libraries, roughly 60 MB; measured on
  ndl-mcp at 26 s with a system 3.13 present and 46 s without, against Claude
  Desktop's 60 s request limit. A first launch that runs past the limit
  self-heals on restart, because uv caches what it fetched. Later launches
  take under a second.
- **`main.py` says what is wrong.** The import is guarded: on `ImportError`
  the entry point writes one line naming the running interpreter, its path
  and the supported range to stderr before re-raising. The `MCP_RECEIPT*`
  blank-stripping and every `user_config` field are unchanged.
- **A blank field in Claude Desktop's install dialog no longer becomes a
  folder or a credential.** Claude Desktop substitutes `${user_config.KEY}`
  only for fields that have a value and passes the placeholder verbatim
  otherwise, so a blank receipts folder became a folder named after the
  placeholder and a blank optional key would have been sent to the provider
  as the key. The entry point now drops any variable whose value still
  carries a placeholder before the package imports, and the handshake gate
  checks that it does.
- **A gate that would have caught this.** `tests/bundle_handshake.py`
  (vendored across the family) unpacks the built bundle, runs it exactly as
  the host would, and requires the `initialize` reply to name the manifest's
  version, on a cold cache and under interpreters other than the bundle's
  pin. The release workflow runs it on all three operating systems before
  anything is attached to a release.
- **CI matrix: 3.10, 3.12, 3.13 and 3.14.** 3.12 was the version the old
  bundles shipped and was never tested. The reported `import mcp` failure on
  3.14 (`TypeError: _eval_type() got an unexpected keyword argument
  'prefer_fwd_module'`) was re-tested: it is pydantic ≥ 2.12.4 meeting a 3.14
  interpreter built before the keyword landed (pydantic/pydantic#12544,
  #12597); 3.14.2, 3.14.3 and 3.14.7 are clean with pydantic 2.13.5.
  `requires-python` stays `>=3.10`; pre-release 3.14 builds are not
  supported.
- **The installers ask where to install, and never guess.** `install.py` and
  `install.ps1` chose the virtual environment silently and, run without a
  terminal, fell back to defaults for the receipts folder as well. Both now
  ask for the install location, the receipts folder and the session slug,
  offering a neutral suggestion that Enter accepts, and run without a
  terminal they stop before touching anything unless `--venv` and
  `--receipts-dir` (or `--no-receipts`; `-VenvDir`, `-ReceiptsDir`,
  `-NoReceipts` for PowerShell) say so. The author's own project slugs, which
  had served as examples in the installer help and the bundle's
  `user_config` description, are replaced with neutral ones.
- **A complete public repository.** `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`
  (Contributor Covenant 2.1), `SECURITY.md`, issue forms that ask for the
  install route, the interpreter and the log tail, a pull request template,
  and Dependabot for the workflow actions and the Python dependencies.
  Repository topics and homepage set on GitHub.
- **The README says what the receipts are for.** A section after the opening
  explains, for a researcher rather than a maintainer, why a hash-chained
  record of every query matters: a citable search, negative findings that
  carry weight, a method section the manifest writes, a record of what an
  assistant actually asked, and nothing interpreted.
- **The README says where to get Python.** A "Getting Python" subsection at
  the head of Install: python.org on Windows with the PATH tick and the
  Microsoft Store stub explained, python.org or Homebrew on macOS, the
  distribution package on Linux, or uv on any of them.
- **Nothing here is specific to Claude, and the README now says so.** The
  server is a Model Context Protocol server over stdio; the bundle and the
  installers are conveniences for one client. A new "Any other MCP client"
  section gives the JSON any client takes and the `claude mcp add` line for
  Claude Code, with the receipts variables as optional environment.
- **The DOI is in the repository.** The Zenodo concept DOI is a badge under
  the README title and an identifier in `CITATION.cff`; neither carried it
  before, although every release has been archived.
- `tests/smoke_stdio.py` finds the console script through `sysconfig` and
  with its `.exe` suffix on Windows, so it no longer depends on the scripts
  directory being on PATH. Vendored across the family.
- `install.ps1` gains `-ConfigPath`, as `install.py` already had, and writes
  the configuration file as UTF-8 without a byte-order mark.
- README: how to read a "Server disconnected" log, where the log lives on
  each platform, and how an `ImportError` differs from a missing interpreter.
  Pins moved to v3.1.0.
- The `attribution` line in every envelope now carries the CiNii Research
  URL (https://cir.nii.ac.jp/) beside NII's name, as the J-STAGE and OpenAlex
  servers' lines already did. Text only; the envelope schema is unchanged.
- Workflow actions moved to their current majors; setup-uv is pinned exactly
  (v10.0.1) because it publishes no moving major tag past v7.

## 3.0.1 — 2026-09-04

- **`cinii_get_record` returns what the record holds.** The single-record
  JSON-LD was read with the OpenSearch schema's assumptions. An untagged
  Japanese title was reported as its own English title as well (`_lit(v, "ja")`
  and `_lit(v, "en")` both fell through to the same value); the `creator`
  nodes, where CiNii puts authors as language-tagged `foaf:name` lists, were
  not read at all, so a fetched record had no authors; NAID and ISSN were
  dropped. Found on 2026-09-04 by fetching a record the search had just
  returned and comparing the two. Language-tagged values now go where their
  tag says and untagged values are routed by script once; authors come from
  `creator` with `dc:creator` as fallback; NAID and ISSN are carried.
  `tests/test_parse.py` checks two records captured from CiNii.
- The MCP SDK's per-request INFO lines no longer reach stderr.
- CI runs `pytest` as well as the stdio smoke test.
- The 3.0.0 entry below no longer describes itself as unreleased; it was
  tagged and released on 2026-09-04, and archived as 10.5281/zenodo.22304321.

## 3.0.0 — 2026-09-04

Tagged and released on GitHub on 2026-09-04; Zenodo 10.5281/zenodo.22304321. The
version was first written on 2026-08-23 and held until the release pipeline
existed.

### Added on 2026-09-04, released with the tag

- **Released on GitHub as a package.** `.github/workflows/release.yml` runs
  on a `vX.Y.Z` tag: tests on three OSes, wheel and sdist, one Claude
  Desktop `.mcpb` bundle per platform, then a GitHub release carrying all of
  them. Installable pinned to the tag with `pip install
  "git+https://github.com/ckgerteis/cinii-mcp@vX.Y.Z"` or `uvx --from`
  the same URL. The release is what fires the Zenodo webhook. Nothing is
  published to a package index.
- **Suite install.** `install.py` is the cross-platform port of `install.ps1`
  (Windows, macOS, Linux; same behaviour, importable). The family is also
  installable as one package, `bibliograph-mcp`, whose `bibliograph install`
  registers all six with one receipts folder.
- **httpx request logging silenced.** The appid travels in the query string
  and httpx logs the full URL at INFO, so a default logging configuration
  wrote the credential to the stderr Claude Desktop captures. The rest of the
  family already muted it; this server had not.
- `tests/smoke_stdio.py`: stdio handshake, `tools/list` checked against the
  README table, optional live call. Vendored byte-identical across the six.
- `response-schema.json`'s self-description said 2.2.0 and named four
  servers; it now says 2.3.0 and names six. Text only; the schema is unchanged.
- Module docstring banner corrected from v2.0.0 to v3.0.0; README subset
  example corrected (`-Servers cinii,ndl`, not `cinii,cinii`).

- **A receipts folder, and one chain per server.** `ledger.py` 1.1.0 adds
  `MCP_RECEIPT_DIR`: point it at a directory and each server writes its own
  `<server>.jsonl` inside it. `MCP_RECEIPT_LOG` still names a single file and is
  honoured when `MCP_RECEIPT_DIR` is unset, so nothing existing breaks.
- **Why, precisely.** Appending is read-the-last-hash-then-write and `_LOCK` is a
  `threading.Lock`, which holds within one process and not between several. Six
  servers are six processes. Six of them writing 150 lines to one file produced
  **fourteen forks** — two lines claiming the same predecessor, over and over.
  That was measured, not inferred, and it means the family's shared log was never
  safe to verify as one chain. One writer per file removes the race rather than
  mitigating it.
- **`verify_chain()` now types its failures.** It reported everything as
  `prev_hash mismatch`. It distinguishes a **fork** (concurrent writers; every
  line still present, and the file is several chains rather than one), a
  **missing** line, a **reordering**, and **tamper** (a line that does not hash to
  its own content). Only the last is a claim about honesty, and a reader given one
  label for all four cannot tell a misconfiguration from interference.
- **`verify_dir()` and a manifest.** One pass over a receipts folder returns
  per-file verdicts, line counts, first and last timestamps and terminal hashes,
  plus combined totals by server, script and session. `<dist>-ledger manifest
  <dir>` writes it to `manifest.json`. That file is what a disclosure cites: one
  description of the deposit rather than six assertions to reconcile.
- `<dist>-ledger` gains `verify-dir` and `manifest`, and `verify` now exits
  non-zero when a chain does not verify.
- **`install.ps1` installs this server by default, not the family.** These are
  six independent packages — none imports another, none depends on another, and
  each installs alone. The installer defaulted to all six, so cloning one
  repository and running it would have registered five servers nobody asked for
  and fetched them from GitHub. It now resolves the default from the repository
  it sits in; `-All` opts into the family and `-Servers` names a subset.
- The verification step now **asserts that `ledger.py` and `mediation.py` are
  byte-identical across everything it installed** and stops if they are not.
  Nothing else enforces that invariant at install time, and two envelope
  versions in one environment is precisely the sort of thing that would be found
  later, in a deposit.
- **`install.ps1` installs the family.** Vendored byte-identical into all six
  repositories: it installs any or all of the six into one environment, asks once
  for the receipts folder and the session slug, and registers every server against
  the same pair. It prefers a sibling checkout to the network, carries across
  credentials already registered rather than asking again, and stops rather than
  guessing where the registered servers disagree about either value.
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
