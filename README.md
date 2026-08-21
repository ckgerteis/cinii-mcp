# cinii-mcp

A FastMCP stdio server exposing the [CiNii Research API](https://support.nii.ac.jp/en/cinii/api/api_outline) — Japan's national academic database, operated by the National Institute of Informatics (NII) — as seven tools for use with Claude Desktop and other MCP clients.

CiNii Research aggregates metadata from KAKEN, CiNii Articles, CiNii Books, IRDB, Crossref, DataCite, PubMed, and NDL Search. There is no established open MCP tooling for it, so this server fills that gap for researchers querying Japanese-language scholarship.

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

Every tool returns one JSON response envelope, built by `mediation.py` and defined in [`response-schema.json`](response-schema.json). Schema version 2.2.0. The same module and schema are vendored byte-identically across the server family, so an envelope from one server can be read by a consumer written for another.

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

### Query receipts

Every envelope can be deposited to an append-only, hash-chained JSONL log by `ledger.py`. It is **off unless `MCP_RECEIPT_LOG` is set**, and a logging failure is swallowed rather than raised — a search matters more than the record of it. Secrets are redacted before a line is composed.

```
MCP_RECEIPT_LOG=C:\path\to\receipts.jsonl
MCP_RECEIPT_SESSION=project-or-article-slug
MCP_RECEIPT_STRICT=1        # optional: make logging failure raise
```

Verify a deposited log's hash chain:

```bash
python ledger.py verify receipts.jsonl
```

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

The server is single-file with three runtime dependencies. Use a dedicated virtual environment.

```powershell
# from the directory containing server.py
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -e .
```

On macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configuration

The server reads your application ID from the `CINII_APPID` environment variable. Copy the example file and fill it in (never commit the real value):

```bash
cp .env.example .env
```

```
CINII_APPID=your_application_id_here
```

### Claude Desktop configuration

Add an entry to `%APPDATA%\Claude\claude_desktop_config.json` under `mcpServers`. Adjust the absolute paths and supply your appid in `env`.

```json
{
  "mcpServers": {
    "cinii": {
      "command": "C:\\path\\to\\cinii-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\cinii-mcp\\server.py"],
      "env": {
        "CINII_APPID": "your_application_id_here"
      }
    }
  }
}
```

Restart Claude Desktop. The seven tools should appear under "cinii" in the tool list.

## Usage rules

NII enforces usage rules; breaking them can get your access blocked or your registration cancelled. This server sends your `appid` on every request (required) and is designed to respect the rules, but you remain responsible for use:

- Do not issue a high volume of requests in a short time. Excessive access that affects other users may be blocked without notice.
- The `appid` is for API requests only; do not expose it in user-facing links to CiNii pages.
- Respect copyright when using retrieved data, per NII's regulations.

## Citation

If this software supports your research, please cite it. See [`CITATION.cff`](CITATION.cff), or use the "Cite this repository" button on GitHub.

## License

[MIT](LICENSE) © 2026 Christopher Gerteis.

This license covers the server code only. It grants no rights over CiNii data or the CiNii API, which remain governed by NII's terms linked above.

## Disclaimer

A research tool, maintained on a best-effort basis and provided "as is", without warranty. Not affiliated with or endorsed by the National Institute of Informatics.

## Author

[Dr Christopher Gerteis](https://www.christophergerteis.net), SOAS University of London. Data provided by [CiNii Research](https://cir.nii.ac.jp/en), National Institute of Informatics.
