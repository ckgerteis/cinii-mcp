# cinii-mcp

A FastMCP stdio server exposing the [CiNii Research API](https://support.nii.ac.jp/en/cinii/api/api_outline) — Japan's national academic database, operated by the National Institute of Informatics (NII) — as seven tools for use with Claude Desktop and other MCP clients.

CiNii Research aggregates metadata from KAKEN, CiNii Articles, CiNii Books, IRDB, Crossref, DataCite, PubMed, and NDL Search. There is no established open MCP tooling for it, so this server fills that gap for researchers querying Japanese-language scholarship.

## What this is for

CiNii Research indexes Japanese scholarship across five kinds of record, and this puts all of them inside a Claude conversation: journal articles, books and monographs, doctoral dissertations, KAKEN grant projects, and researcher profiles, plus single-record lookup by CRID. Ask a question in English and get Japanese-language scholarship back, with the Japanese term actually sent shown beside the results.

KAKEN repays separate attention — it records what was *funded*, so it surfaces projects underway, collaborations forming, and research that reached a grant report before it reached print.

Every result carries the term sent, its script, how CiNii matched it, and a receipt fixing the query, so a search standing behind a footnote can be named, cited, and run again by someone else.

## Tools

| Tool | Purpose |
| --- | --- |
| `cinii_search_articles` | Journal articles (JALC, Crossref, PubMed, IRDB) |
| `cinii_search_books` | Books and monographs (NACSIS-CAT, NDL Search) |
| `cinii_search_dissertations` | Doctoral dissertations from Japanese universities |
| `cinii_search_kaken` | KAKEN (科研費) funded research projects |
| `cinii_search_all` | Cross-type search across all content types |
| `cinii_search_researchers` | Researcher profiles and affiliations |
| `cinii_get_record` | Single record lookup by URL or CRID |

Results come from the CiNii Research OpenSearch v2 API as JSON-LD and are returned as one typed JSON response envelope — see [Response format](#response-format) below. (Releases before v2.0.1 returned formatted markdown text; that is a breaking change, not a formatting preference.)

## Response format

Every tool returns one JSON response envelope, built by `mediation.py` and defined in [`response-schema.json`](response-schema.json). Schema version 2.3.0. The same module and schema are vendored byte-identically across the server family, so an envelope from one server can be read by a consumer written for another.

The envelope reports how the search was made, not only what it found:

- **`searched_for`** — on search operations, the term actually sent, its detected script, and the matching mode, hoisted to the top of the envelope so a relaying client cannot drop it. Fetch operations (`cinii_get_record`) omit it: they were handed an identifier and chose no term.
- **`query`** — `input_terms` as supplied, `normalized` as sent, and the detected `script`. This pair is the record of any rendering performed between the caller's language and the corpus.
- **`matching_mode`** — `metadata_conjunction` for this server. It tells you how to read `result.total`.
- **`result.breadth`** — `none`, `narrow` (1–50), `broad` (51–1000), `very_broad` (>1000). Thresholds are low on purpose: a few hundred hits that look like a literature are marked rather than passed through clean.
- **`items[].matched_in`** — which field the match was made in, per record.
- **`receipt`** — an ISO 8601 timestamp, a SHA-256 taken over the normalised query and its parameters, and the identifiers returned. The hash verifies a term you already hold; it cannot be inverted to produce one, so the unit of deposit is the envelope, not the receipt.
- **`attribution`** — the required credit line, in every response.

### Diagnostic codes

Typed and closed. A diagnostic is never prose the client has to parse.

| Code | Level | Meaning |
| --- | --- | --- |
| `OK` | info | Records returned; nothing to flag. |
| `ZERO_CONJUNCTION` | warning | No records. CiNii matches catalogued metadata and ANDs a multi-word query, so an un-indexed compound returns zero even where related work exists. Vary the rendering before concluding the literature is absent. |
| `SCRIPT_LATIN_QUERY` | warning | The query was Latin-script, so it matched romanised and English metadata only. The Japanese-script form reaches a different, larger corpus. |
| `API_ERROR` | error | The API answered, and answered with an error. |
| `TRANSPORT_ERROR` | error | The request did not complete. Kept distinct from `API_ERROR` because a failed search has an unknown result and must never be written up as an absence. |
| `RECEIPT_NOT_DEPOSITED` | info | The response was not written to the query ledger, because no receipts destination is configured. The search is unaffected; no receipt survives it. |
| `RECEIPT_WRITE_FAILED` | warning | A receipts destination is set, the write was attempted, and it did not land. Distinct from the line above because one is a choice and the other is a fault. |

### Query receipts

Every envelope can be deposited to an append-only, hash-chained JSONL log by `ledger.py`. It is **off unless `MCP_RECEIPT_DIR` (or the legacy `MCP_RECEIPT_LOG`) is set**, and a logging failure is swallowed rather than raised — a search matters more than the record of it. Secrets are redacted before a line is composed.

Since schema 2.3.0 the envelope says so. When a response is not deposited, `emit()` appends `RECEIPT_NOT_DEPOSITED` if the variable is unset, or `RECEIPT_WRITE_FAILED` if it is set and the write did not land. The gap is then visible in the artefact that becomes the record, rather than only in a configuration file. `mediation.deposit_enabled()` reports the same fact on demand.

```
MCP_RECEIPT_DIR=C:\path\to\receipts        # a folder, not a file
MCP_RECEIPT_SESSION=project-or-article-slug
MCP_RECEIPT_STRICT=1                         # optional: make logging failure raise
MCP_RECEIPT_LOG=C:\path\to\receipts.jsonl  # legacy single file; ignored when _DIR is set
```

**A folder, and one file per server.** `MCP_RECEIPT_DIR` points at a directory
and each server writes its own `<server>.jsonl` inside it. That is not tidiness.
Appending is read-the-last-hash-then-write, and the lock around it is a threading
lock, which holds within one process and not between several — six servers are
six processes, and two answering at the same moment will both read the same
predecessor and both claim it. Measured, not theorised: six processes writing 150
lines to one file produced fourteen forks. `MCP_RECEIPT_LOG` still works and is
still correct for a single server; it is the wrong shape for a family.

`install.ps1` sets this up for all six and writes a README into the folder.

Verify one chain, or the whole folder:

```bash
cinii-mcp-ledger verify      receipts/cinii.jsonl
cinii-mcp-ledger verify-dir  receipts
cinii-mcp-ledger manifest    receipts        # writes receipts/manifest.json
```

`verify` exits non-zero on failure and says which kind it found: a **fork**
(concurrent writers — a configuration fault, and every line is still there), a
**missing** line, a **reordering**, or **tamper** (a line that does not hash to
its own content). Only the last is a claim about honesty, and reporting them
alike would invite a reader to mistake one for the other. The manifest is the
object to cite: one description of the whole deposit — per-file line counts,
first and last timestamps, terminal hashes, and combined totals by server,
script and session.

## Prerequisites

- Python 3.10+ on PATH.
- A CiNii Web API **application ID** (`appid`) — free; required.

## Getting an application ID

The CiNii Research API requires a registered application ID, sent as a parameter on every request.

1. Register at the [CiNii Web API Developer Registration](https://api.ci.nii.ac.jp/en/) page and obtain your application ID.
2. Agree to NII's [API regulations](https://support.nii.ac.jp/en/cinii/api/developer): the Academic Content Service Usage Regulations, the CiNii Research Usage Detailed Regulations, and the Academic Content Service Web API Usage Detailed Regulations.
3. For commercial use, email `ciniiadm@nii.ac.jp` before applying.

The same application ID also works for the KAKEN API, which `cinii_search_kaken` uses.

## Install

The package installs a `cinii-mcp` console script. It is namespaced, so it can
share one environment with the rest of this server family.

```bash
python3 -m venv .venv
.venv/bin/pip install .
```

On Windows:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\pip.exe install .
```

Or straight from the repository, without cloning:

```bash
uvx --from "git+https://github.com/ckgerteis/cinii-mcp" cinii-mcp
```

Verify the install:

```bash
.venv/bin/python -c "import cinii_mcp; print(cinii_mcp.__version__)"
```

That fails loudly if the package or one of its vendored modules is missing. Do
not use `cinii-mcp --help` as the check: unknown arguments are ignored, the
server starts, reads end-of-input and exits 0, so it reports success whatever
the state of the code.

### Installing more than this one

Six independent packages. None imports another, none depends on another, and
each installs and answers on its own — `pip install .` in this directory is a
complete install of this server and nothing else.

They do share three things: a response envelope, a query ledger, and — if you
run more than one — a receipts folder. `install.ps1` is vendored byte-identical
into all six and handles that. **It installs this server by default**, because
cloning one repository is not a request for five more.

```powershell
.\install.ps1                        # this server
.\install.ps1 -All                   # all six
.\install.ps1 -Servers cinii,ndl           # a chosen subset
```

Whatever subset you name is registered against one receipts folder, asked for
once. The script prefers a sibling checkout to the network, carries across
credentials already registered rather than asking again, leaves servers it was
not asked about alone, and stops rather than guessing where the servers already
registered disagree about the folder or the session slug. It also asserts that
`ledger.py` and `mediation.py` are byte-identical across everything it
installed, so two envelope versions cannot end up in one environment unnoticed.

## Configuration

The server reads your application ID from the `CINII_APPID` environment variable. Copy the example file and fill it in (never commit the real value):

```bash
cp .env.example .env
```

```
CINII_APPID=your_application_id_here
```

### Claude Desktop configuration

Add an entry to `%APPDATA%\Claude\claude_desktop_config.json` under
`mcpServers`, pointing at the console script in the environment you installed
into. On macOS or Linux use the absolute path to `.venv/bin/cinii-mcp`.

```json
{
  "mcpServers": {
    "cinii": {
      "command": "C:\\path\\to\\.venv\\Scripts\\cinii-mcp.exe",
      "env": {
        "CINII_APPID": "your_application_id_here"
      }
    }
  }
}
```

**Changed in 3.0.0.** Earlier versions were registered by path —
`"command": "…\\python.exe", "args": ["…\\server.py"]`. That entry will not
start this version, because `server.py` is now a module inside a package rather
than a script beside its imports. Replace it with the console script above.

Restart Claude Desktop. The seven tools should appear under "cinii" in the
tool list.

## Usage rules

NII enforces usage rules; breaking them can get your access blocked or your registration cancelled. This server sends your `appid` on every request (required) and is designed to respect the rules, but you remain responsible for use:

- Do not issue a high volume of requests in a short time. Excessive access that affects other users may be blocked without notice.
- The `appid` is for API requests only; do not expose it in user-facing links to CiNii pages.
- Respect copyright when using retrieved data, per NII's regulations.

## Citation

If this software supports your research, please cite it. See [`CITATION.cff`](CITATION.cff), or use the "Cite this repository" button on GitHub.

## Tests

```bash
.venv/bin/python tests/smoke_stdio.py
```

Starts the installed console script over stdio, performs the MCP handshake, and checks `tools/list` against the tool table above; exits non-zero on a mismatch. `RUN_LIVE=1 … <tool> '<json params>'` adds one live call and reports the envelope's diagnostic codes.

## License

[MIT](LICENSE) © 2026 Christopher Gerteis.

This license covers the server code only. It grants no rights over CiNii data or the CiNii API, which remain governed by NII's terms linked above.

## Disclaimer

A research tool, maintained on a best-effort basis and provided "as is", without warranty. Not affiliated with or endorsed by the National Institute of Informatics.

## Author

[Dr Christopher Gerteis](https://www.christophergerteis.net), SOAS University of London. Data provided by [CiNii Research](https://cir.nii.ac.jp/en), National Institute of Informatics.
