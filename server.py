"""
CiNii Research MCP Server
==================================
An MCP server for searching Japan's national academic information database
(CiNii Research), operated by the National Institute of Informatics (NII).

v2.0.0 — clean replacement of the response format. All search tools now emit the
unified response envelope shared with jstage-mcp (see mediation.py /
response-schema.json): typed query/script, matching_mode (metadata_conjunction),
graduated breadth, per-item matched_in, typed diagnostics (including
ZERO_CONJUNCTION and SCRIPT_LATIN_QUERY), a loggable receipt, and attribution.
This is a breaking change from v1.x, which returned formatted markdown text.

Requires an Application ID (appid) from NII, read from CINII_APPID. No secret is
embedded; the appid is taken from the environment at runtime.
"""
from __future__ import annotations

import os
import re
from enum import Enum
from typing import Optional

import httpx
from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP

import mediation as M

__version__ = "2.0.1"

# ==============================================================================
# Configuration
# ==============================================================================

CINII_APPID = os.environ.get("CINII_APPID", "")
BASE_URL = "https://cir.nii.ac.jp"
OPENSEARCH_V2 = f"{BASE_URL}/opensearch/v2"
TIMEOUT = 30.0
ATTRIBUTION = "Data via CiNii Research, National Institute of Informatics (NII)."
MATCHING_MODE = "metadata_conjunction"
ARTICLE_COVERAGE_NOTE = (
    "The CiNii article index excludes most monographs and book chapters; "
    "foundational studies may sit in cinii_search_books."
)

mcp = FastMCP("cinii_mcp")


class SortOrder(str, Enum):
    NEWEST = "0"
    OLDEST = "1"
    RELEVANCE = "4"
    CITATIONS_DESC = "10"


class ResponseLang(str, Enum):
    JAPANESE = "ja"
    ENGLISH = "en"


# ==============================================================================
# HTTP
# ==============================================================================


async def _cinii_request(path: str, params: dict) -> dict:
    params = dict(params)
    params["appid"] = CINII_APPID
    params["format"] = "json"
    url = f"{OPENSEARCH_V2}/{path}" if path else OPENSEARCH_V2
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, params=params)
    if resp.status_code == 404:
        return {"error": "Not found (HTTP 404)."}
    if resp.status_code >= 400:
        return {"error": f"CiNii API error {resp.status_code}: {resp.text[:300]}"}
    return resp.json()


async def _run(path: str, qp: dict) -> tuple[Optional[dict], Optional[dict]]:
    """Return (data, error_diag). error_diag is a typed diagnostic or None."""
    try:
        data = await _cinii_request(path, qp)
    except Exception as exc:  # noqa: BLE001
        return None, M.diag("error", "TRANSPORT_ERROR", f"Network error reaching CiNii: {exc}.", "Retry shortly.")
    if isinstance(data, dict) and "error" in data:
        return None, M.diag("error", "API_ERROR", str(data["error"]), None)
    return data, None


# ==============================================================================
# CiNii JSON-LD -> envelope item
# ==============================================================================


def _split_lang(s: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Route a string to (ja, en) by detected script."""
    if not s:
        return (None, None)
    return (s, None) if M.detect_script(s) in ("han", "kana", "han_kana", "mixed") else (None, s)


_RECORD_TYPE = {
    "Article": "article", "Book": "book", "Data": "article",
    "DoctoralThesis": "dissertation", "Dissertation": "dissertation",
    "Project": "project", "Researcher": "researcher",
}


def _lit(v, lang: Optional[str] = None) -> Optional[str]:
    """Extract a display string from a JSON-LD value: a str, a {@value} dict,
    or a list of {@language, @value}. Prefers `lang` when supplied."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return v.get("@value")
    if isinstance(v, list):
        if lang:
            for x in v:
                if isinstance(x, dict) and x.get("@language") == lang:
                    return x.get("@value")
        for x in v:
            if isinstance(x, dict) and x.get("@value"):
                return x.get("@value")
            if isinstance(x, str):
                return x
    return None


def _item_from_record(d: dict) -> dict:
    """Parse a CiNii Research single-record JSON-LD (/crid/<id>.json) into an
    envelope item. This schema differs from the OpenSearch item schema: titles
    are language-tagged lists, @type carries the record kind, and article
    journal data sits under `publication` (book imprint under dcterms:publisher)."""
    rt = d.get("@type")
    if isinstance(rt, list):
        rt = rt[0] if rt else None
    record_type = _RECORD_TYPE.get(rt, "article")

    title_ja = _lit(d.get("dc:title"), "ja")
    title_en = _lit(d.get("dc:title"), "en")

    authors = []
    dcc = d.get("dc:creator")
    if isinstance(dcc, str):
        dcc = [dcc]
    if isinstance(dcc, list):
        for c in dcc[:12]:
            name = c if isinstance(c, str) else _lit(c)
            if name:
                cja, cen = _split_lang(name)
                authors.append({"ja": cja, "en": cen})

    journal_ja = journal_en = volume = issue = pages = year = doi = None
    pub = d.get("publication")
    if isinstance(pub, dict):
        journal_ja = _lit(pub.get("prism:publicationName"), "ja")
        journal_en = _lit(pub.get("prism:publicationName"), "en")
        volume = pub.get("prism:volume")
        issue = pub.get("prism:number")
        sp, ep = pub.get("prism:startingPage"), pub.get("prism:endingPage")
        pages = f"{sp}-{ep}" if sp and ep else (sp or None)
        mm = re.search(r"(\d{4})", str(pub.get("prism:publicationDate") or ""))
        year = int(mm.group(1)) if mm else None
    if record_type == "book":
        pubs = d.get("dcterms:publisher")
        if isinstance(pubs, list) and pubs and isinstance(pubs[0], dict):
            journal_ja = journal_ja or _lit(pubs[0].get("dc:publisher"), "ja")
            journal_en = journal_en or _lit(pubs[0].get("dc:publisher"), "en")
    if year is None:
        mm = re.search(r"(\d{4})", str(_lit(d.get("dc:date")) or ""))
        year = int(mm.group(1)) if mm else None

    doi_v = d.get("prism:doi")
    doi = _lit(doi_v) if doi_v else None
    at = d.get("@id") or ""
    crid = at.split("/crid/")[-1].replace(".json", "").strip("/") if "/crid/" in at else None
    url = at.replace(".json", "") if at else None

    return M.make_item(
        title_ja=title_ja, title_en=title_en, authors=authors,
        journal_ja=journal_ja, journal_en=journal_en,
        volume=str(volume) if volume else None,
        issue=str(issue) if issue else None,
        pages=str(pages) if pages else None,
        year=year, doi=doi, crid=crid, url_ja=url,
        matched_in="metadata", record_type=record_type,
    )


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _item_from_cinii(raw: dict, record_type: str) -> dict:
    title = _first(raw.get("title") or raw.get("dc:title"))
    tja, ten = _split_lang(title if isinstance(title, str) else None)

    creators = raw.get("dc:creator") or []
    if isinstance(creators, str):
        creators = [creators]
    authors = []
    for c in creators[:12]:
        if not isinstance(c, str):
            continue
        cja, cen = _split_lang(c)
        authors.append({"ja": cja, "en": cen})

    pub = _first(raw.get("prism:publicationName") or raw.get("dc:publisher"))
    pja, pen = _split_lang(pub if isinstance(pub, str) else None)

    date = _first(raw.get("prism:publicationDate") or raw.get("dc:date")) or ""
    ym = re.search(r"(\d{4})", str(date))
    year = int(ym.group(1)) if ym else None

    doi = _first(raw.get("prism:doi"))
    link = raw.get("@id") or (raw.get("link", {}) or {}).get("@id")
    crid = link.split("/crid/")[-1].strip("/") if (link and "/crid/" in link) else None

    vol = _first(raw.get("prism:volume"))
    no = _first(raw.get("prism:number"))
    sp = _first(raw.get("prism:startingPage"))
    ep = _first(raw.get("prism:endingPage"))
    if sp and ep:
        pages = f"{sp}-{ep}"
    elif raw.get("prism:pageRange"):
        pages = _first(raw.get("prism:pageRange"))
    elif sp:
        pages = sp
    else:
        pages = None

    return M.make_item(
        title_ja=tja, title_en=ten,
        authors=authors,
        journal_ja=pja, journal_en=pen,
        volume=str(vol) if vol is not None else None,
        issue=str(no) if no is not None else None,
        pages=str(pages) if pages is not None else None,
        year=year,
        doi=doi if isinstance(doi, str) else None,
        crid=crid,
        url_ja=link,
        matched_in="metadata",
        record_type=record_type,
    )


def _envelope(
    *, operation: str, record_type: str, query_str: str, qp: dict,
    data: Optional[dict], error_diag: Optional[dict], monograph_note: bool = False,
) -> str:
    items = [_item_from_cinii(r, record_type) for r in (data.get("items", []) if data else [])]
    total = 0
    if data is not None:
        try:
            total = int(data.get("opensearch:totalResults", 0))
        except (TypeError, ValueError):
            total = 0
    script = M.detect_script(query_str)

    ds: list[dict] = []
    if error_diag:
        ds.append(error_diag)
    if script == "latin":
        ds.append(M.diag(
            "warning", "SCRIPT_LATIN_QUERY",
            f"Query is Latin-script; CiNii matched romanized/English metadata only "
            f"({total} records). The Japanese-script form reaches a different, larger corpus.",
            "Re-issue in kanji/kana (e.g. 暴走族) to search the Japanese-language literature.",
        ))
    if not error_diag and not items:
        ds.append(M.diag(
            "warning", "ZERO_CONJUNCTION",
            "No records. CiNii matches catalogued metadata and ANDs multi-word queries, "
            "so an un-indexed compound returns zero even when related work exists.",
            "Vary the rendering: a single key term, an emic alternative, the kanji compound, "
            "or a different record type.",
        ))
    if not ds:
        ds.append(M.diag("info", "OK", f"{total} record(s) on metadata match.", None))

    coverage = None
    suggestions = None
    if not items and monograph_note:
        coverage = ARTICLE_COVERAGE_NOTE
        suggestions = [
            {"action": "cinii_search_articles", "reason": "retry with a single key term or an alternative Japanese rendering"},
            {"action": "cinii_search_books", "reason": "monographs and ethnographies are absent from the article index"},
        ]

    env = M.build_envelope(
        server="cinii", operation=operation,
        input_terms=query_str, normalized=query_str,
        params={k: v for k, v in qp.items() if k not in ("appid", "format")},
        matching_mode=MATCHING_MODE, total=total, start=int(qp.get("start", 1) or 1),
        items=items, diagnostics=ds, attribution=ATTRIBUTION,
        coverage_note=coverage, suggestions=suggestions,
    )
    return M.dumps(env)


def _build_params(base: dict, extra: dict) -> dict:
    return {k: v for k, v in {**base, **extra}.items() if v is not None}


# ==============================================================================
# Input models
# ==============================================================================


class ArticleSearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(..., description="Search query (Japanese or English)", min_length=1)
    title: Optional[str] = Field(default=None)
    author: Optional[str] = Field(default=None)
    journal: Optional[str] = Field(default=None)
    from_year: Optional[int] = Field(default=None, ge=1800)
    to_year: Optional[int] = Field(default=None)
    data_source: Optional[str] = Field(default=None)
    sort: SortOrder = Field(default=SortOrder.RELEVANCE)
    lang: ResponseLang = Field(default=ResponseLang.ENGLISH)
    count: int = Field(default=20, ge=1, le=200)
    start: int = Field(default=1, ge=1)


class BookSearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(..., min_length=1)
    title: Optional[str] = Field(default=None)
    author: Optional[str] = Field(default=None)
    publisher: Optional[str] = Field(default=None)
    isbn: Optional[str] = Field(default=None)
    from_year: Optional[int] = Field(default=None, ge=1800)
    to_year: Optional[int] = Field(default=None)
    lang: ResponseLang = Field(default=ResponseLang.ENGLISH)
    count: int = Field(default=20, ge=1, le=200)
    start: int = Field(default=1, ge=1)


class DissertationSearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(..., min_length=1)
    author: Optional[str] = Field(default=None)
    from_year: Optional[int] = Field(default=None, ge=1800)
    to_year: Optional[int] = Field(default=None)
    lang: ResponseLang = Field(default=ResponseLang.ENGLISH)
    count: int = Field(default=20, ge=1, le=200)
    start: int = Field(default=1, ge=1)


class KakenSearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(..., min_length=1)
    researcher: Optional[str] = Field(default=None)
    institution: Optional[str] = Field(default=None)
    from_year: Optional[int] = Field(default=None, ge=1900)
    to_year: Optional[int] = Field(default=None)
    lang: ResponseLang = Field(default=ResponseLang.ENGLISH)
    count: int = Field(default=20, ge=1, le=200)
    start: int = Field(default=1, ge=1)


class CrossSearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(..., min_length=1)
    from_year: Optional[int] = Field(default=None, ge=1800)
    to_year: Optional[int] = Field(default=None)
    lang: ResponseLang = Field(default=ResponseLang.ENGLISH)
    count: int = Field(default=20, ge=1, le=200)
    start: int = Field(default=1, ge=1)


class ResearcherSearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(..., min_length=1)
    institution: Optional[str] = Field(default=None)
    lang: ResponseLang = Field(default=ResponseLang.ENGLISH)
    count: int = Field(default=20, ge=1, le=200)
    start: int = Field(default=1, ge=1)


class RecordLookupInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    record_url: str = Field(..., description="Full CiNii URL or CRID", min_length=1)


# ==============================================================================
# Tools
# ==============================================================================


@mcp.tool(name="cinii_search_articles", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def cinii_search_articles(params: ArticleSearchInput) -> str:
    """Search CiNii Research for journal articles. Returns the unified envelope.

    CiNii matches catalogued metadata and ANDs a multi-word query, so an
    un-indexed compound returns zero even when related work exists — a
    ZERO_CONJUNCTION diagnostic marks this; vary the rendering rather than
    concluding the literature is absent. A SCRIPT_LATIN_QUERY diagnostic means
    the query searched romanized metadata only. The same string may behave very
    differently on J-STAGE (full text). Foundational monographs sit in
    cinii_search_books, not the article index.
    """
    qp = _build_params(
        {"q": params.query, "count": params.count, "start": params.start, "sortorder": params.sort.value, "lang": params.lang.value},
        {"title": params.title, "creator": params.author, "publicationName": params.journal,
         "from": params.from_year, "until": params.to_year, "dataSourceType": params.data_source},
    )
    data, err = await _run("articles", qp)
    return _envelope(operation="search_articles", record_type="article", query_str=params.query, qp=qp, data=data, error_diag=err, monograph_note=True)


@mcp.tool(name="cinii_search_books", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def cinii_search_books(params: BookSearchInput) -> str:
    """Search CiNii Research for books and monographs. Returns the unified envelope."""
    qp = _build_params(
        {"q": params.query, "count": params.count, "start": params.start, "lang": params.lang.value},
        {"title": params.title, "creator": params.author, "publisher": params.publisher,
         "isbn": params.isbn, "from": params.from_year, "until": params.to_year},
    )
    data, err = await _run("books", qp)
    return _envelope(operation="search_books", record_type="book", query_str=params.query, qp=qp, data=data, error_diag=err)


@mcp.tool(name="cinii_search_dissertations", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def cinii_search_dissertations(params: DissertationSearchInput) -> str:
    """Search CiNii Research for doctoral dissertations. Returns the unified envelope."""
    qp = _build_params(
        {"q": params.query, "count": params.count, "start": params.start, "lang": params.lang.value},
        {"creator": params.author, "from": params.from_year, "until": params.to_year},
    )
    data, err = await _run("dissertations", qp)
    return _envelope(operation="search_dissertations", record_type="dissertation", query_str=params.query, qp=qp, data=data, error_diag=err)


@mcp.tool(name="cinii_search_kaken", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def cinii_search_kaken(params: KakenSearchInput) -> str:
    """Search KAKEN (科研費) research projects. Returns the unified envelope (record_type 'project')."""
    qp = _build_params(
        {"q": params.query, "count": params.count, "start": params.start, "lang": params.lang.value},
        {"creator": params.researcher, "affiliation": params.institution, "from": params.from_year, "until": params.to_year},
    )
    data, err = await _run("projects", qp)
    return _envelope(operation="search_projects", record_type="project", query_str=params.query, qp=qp, data=data, error_diag=err)


@mcp.tool(name="cinii_search_all", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def cinii_search_all(params: CrossSearchInput) -> str:
    """Cross-type search across all CiNii content. Returns the unified envelope.

    Records are emitted with record_type 'article' as a default; the cross
    search mixes types and CiNii does not always disambiguate them in the
    OpenSearch response.
    """
    qp = _build_params(
        {"q": params.query, "count": params.count, "start": params.start, "lang": params.lang.value},
        {"from": params.from_year, "until": params.to_year},
    )
    data, err = await _run("all", qp)
    return _envelope(operation="search_all", record_type="article", query_str=params.query, qp=qp, data=data, error_diag=err)


@mcp.tool(name="cinii_search_researchers", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def cinii_search_researchers(params: ResearcherSearchInput) -> str:
    """Search for researchers in CiNii. Returns the unified envelope (record_type 'researcher').

    Note: researcher affiliation is not carried by the record schema; the
    researcher name occupies the title field and the profile URL the ids.url_ja
    field.
    """
    qp = _build_params(
        {"q": params.query, "count": params.count, "start": params.start, "lang": params.lang.value},
        {"affiliation": params.institution},
    )
    data, err = await _run("researchers", qp)
    return _envelope(operation="search_researchers", record_type="researcher", query_str=params.query, qp=qp, data=data, error_diag=err)


@mcp.tool(name="cinii_get_record", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def cinii_get_record(params: RecordLookupInput) -> str:
    """Fetch a single CiNii record by URL or CRID. Returns the unified envelope (operation 'get_record')."""
    raw = params.record_url.strip()
    crid = raw.rsplit("/crid/", 1)[-1].strip("/") if "/crid/" in raw else raw
    url = f"{BASE_URL}/crid/{crid}.json"  # JSON-LD record; the bare /crid/ page is HTML
    record, err = None, None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(url, params={"appid": CINII_APPID, "format": "json"})
        if resp.status_code >= 400:
            err = M.diag("error", "API_ERROR", f"CiNii returned HTTP {resp.status_code} for this record.", "Check the CRID and retry.")
        else:
            try:
                record = resp.json()
            except ValueError:
                err = M.diag("error", "API_ERROR", "CiNii did not return a JSON record for this identifier.", "Confirm the CRID identifies an indexed record.")
    except Exception as exc:  # noqa: BLE001
        err = M.diag("error", "TRANSPORT_ERROR", f"Network error reaching CiNii: {exc}.", "Retry shortly.")

    items = [_item_from_record(record)] if isinstance(record, dict) else []
    total = 1 if items else 0
    ds = [err] if err else [M.diag("info", "OK", "Single record resolved.", None)]
    env = M.build_envelope(
        server="cinii", operation="get_record", input_terms=params.record_url, normalized=crid,
        params={"record_url": params.record_url}, matching_mode=MATCHING_MODE, total=total, start=1,
        items=items, diagnostics=ds, attribution=ATTRIBUTION,
    )
    return M.dumps(env)


if __name__ == "__main__":
    mcp.run()
