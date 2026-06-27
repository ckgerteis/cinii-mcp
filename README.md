# cinii-mcp

A FastMCP stdio server exposing the [CiNii Research API](https://support.nii.ac.jp/en/cinii/api/api_outline) — Japan's national academic database, operated by the National Institute of Informatics (NII) — as seven tools for use with Claude Desktop and other MCP clients.

CiNii Research aggregates metadata from KAKEN, CiNii Articles, CiNii Books, IRDB, Crossref, DataCite, PubMed, and NDL Search. There is no established open MCP tooling for it, so this server fills that gap for researchers querying Japanese-language scholarship.

> In **v2**, every tool now returns one structured JSON
> *response envelope* (shared with [jstage-mcp](https://github.com/ckgerteis/jstage-mcp))
> instead of v1's formatted markdown text. The envelope moves the interpretation
> keys — how a query was matched, how broad the result is, what script was
> searched — into typed fields. See **Response format** below. v2 also ships a
> companion `mediation.py` that must sit beside `server.py`.

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

Records are drawn from the CiNii Research OpenSearch v2 API; `cinii_get_record`
resolves a single record from its JSON-LD representation (`/crid/<id>.json`).

## Response format

Every tool returns the unified envelope:

```jsonc
{
  "server": "cinii",
  "operation": "search_articles",
  "query": { "input_terms": "...", "normalized": "...",
             "script": "han|kana|han_kana|latin|mixed", "params": { ... } },
  "matching_mode": "metadata_conjunction",   // CiNii ANDs catalogued metadata
  "result": { "total": 0, "returned": 0, "start": 1,
              "breadth": "none|narrow|broad|very_broad" },
  "items": [ { "title": {"ja":..,"en":..,"romanized":..},
               "authors": [{"ja":..,"en":..}],
               "source": {"journal_ja":..,"journal_en":..,"volume":..,"issue":..,"pages":..,"year":..},
               "ids": {"doi":..,"crid":..,"naid":..,"url_ja":..,"url_en":..},
               "matched_in": "metadata", "record_type": "article|book|dissertation|project|researcher" } ],
  "diagnostics": [ { "level": "info|warning|error", "code": "...", "message": "...", "hint": "..." } ],
  "coverage_note": "...|null",
  "suggestions": [ { "action": "...", "reason": "..." } ],
  "receipt": { "issued_at": "<ISO-8601>", "query_hash": "sha256:...", "result_ids": [ ... ] },
  "attribution": "Data via CiNii Research, National Institute of Informatics (NII)."
}
```

Diagnostic codes: `OK`, `ZERO_CONJUNCTION` (a multi-word metadata query ANDed to
nothing — vary the rendering), `SCRIPT_LATIN_QUERY` (a romaji query matched only
the Latin-script metadata; re-issue in kanji/kana for the Japanese corpus),
`API_ERROR`, `TRANSPORT_ERROR`. The `receipt` is designed to be logged so a
search can be reconstructed.

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

The server is `server.py` plus a companion `mediation.py` (the shared response
envelope; pure standard library, no extra dependency). **Both files must live in
the same directory.** Use a dedicated virtual environment.

```powershell
# from the directory containing server.py and mediation.py
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
