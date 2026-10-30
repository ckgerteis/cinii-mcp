"""
CiNii Research MCP Server
=========================
(The version is __version__ below; it is not repeated here, because set_version.py
does not edit this docstring and a second copy drifts.)
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

import logging
import os
import re
import sys
from enum import Enum
from typing import Any, ClassVar, Optional
from urllib.parse import quote, quote_plus, urlsplit

import httpx
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
try:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServer
except ModuleNotFoundError:  # mcp SDK 2.x removed mcp.server.fastmcp
    from mcp.server.mcpserver import MCPServer as _MCPServer

from . import mediation as M

__version__ = "3.2.0"


def _silence_http_logging() -> None:
    """Two reasons, either sufficient on its own.

    httpx logs every request at INFO with the full URL — and the CiNii appid
    travels in the query string, so a default logging setup writes the
    credential into whatever stream the client is capturing.

    Second, this is a stdio server: stdout carries JSON-RPC and nothing else.
    Any handler that defaults to stdout corrupts the protocol frame.
    """
    for name in ("httpx", "httpcore", "httpx._client"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING)
        lg.propagate = False
    # The MCP SDK logs every request it handles at INFO ("Processing request
    # of type ListToolsRequest"). Noise, not a leak; still, stderr should
    # carry faults only.
    logging.getLogger("mcp").setLevel(logging.WARNING)
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "stream", None) is sys.stdout:
            root.removeHandler(h)
    if not root.handlers:
        root.addHandler(logging.StreamHandler(sys.stderr))


_silence_http_logging()

# ==============================================================================
# Configuration
# ==============================================================================

CINII_APPID = os.environ.get("CINII_APPID", "")
BASE_URL = "https://cir.nii.ac.jp"
OPENSEARCH_V1 = f"{BASE_URL}/opensearch"
OPENSEARCH_V2 = f"{BASE_URL}/opensearch/v2"
TIMEOUT = 30.0
ATTRIBUTION = "Data via CiNii Research (https://cir.nii.ac.jp/), National Institute of Informatics (NII)."
MATCHING_MODE = "metadata_conjunction"
ARTICLE_COVERAGE_NOTE = (
    "The CiNii article index excludes most monographs and book chapters; "
    "foundational studies may sit in cinii_search_books."
)

# mcp 1.x's FastMCP takes no `version`; 2.x's MCPServer does. Passed where it is
# accepted, because a server that answers `initialize` with an empty version
# string cannot be cited by the disclosure that has to name the build it ran.
try:
    mcp = _MCPServer("cinii_mcp", version=__version__)
except TypeError:  # mcp SDK 1.x
    mcp = _MCPServer("cinii_mcp")


class SortOrder(str, Enum):
    """CiNii Research OpenSearch v2 `sortorder` codes.

    Not every code is valid for every search type, and CiNii does not refuse
    one that is not: sortorder=5 on articles is silently ignored and the
    results come back in relevance order, a page that says it is sorted when
    it is not. Each input model therefore checks its sort against _SORT_SCOPE.
    """
    NEWEST = "0"            # publication / degree-award / project-start year, newest first
    OLDEST = "1"            # the same, oldest first (not researchers)
    RELEVANCE = "4"         # default
    NAME_ASC = "5"          # researcher name A-Z (researchers only)
    NAME_DESC = "6"         # researcher name Z-A (researchers only)
    CITATIONS_DESC = "10"   # citation count, descending (articles only)
    PROJECTS_DESC = "20"    # number of related projects, descending (researchers only)
    PROJECTS_ASC = "21"     # number of related projects, ascending (researchers only)
    PRODUCTS_DESC = "30"    # number of related products, descending (researchers only)
    PRODUCTS_ASC = "31"     # number of related products, ascending (researchers only)
    CRID_ASC = "40"         # CRID ascending (all only)


SORT_DESCRIPTION = (
    "Sort order. NEWEST/OLDEST = by year (publication, degree award or project start); "
    "RELEVANCE (default); CITATIONS_DESC = citation count, articles only; CRID_ASC = by "
    "CRID, cross search only; NAME_ASC/NAME_DESC, PROJECTS_DESC/PROJECTS_ASC and "
    "PRODUCTS_DESC/PRODUCTS_ASC = researcher name, number of related projects and number "
    "of related research products, researcher search only (which does not accept OLDEST)."
)

_BASE_SORTS = frozenset({SortOrder.NEWEST, SortOrder.OLDEST, SortOrder.RELEVANCE})
_SORT_SCOPE: dict[str, frozenset] = {
    "articles": _BASE_SORTS | {SortOrder.CITATIONS_DESC},
    "books": _BASE_SORTS,
    "dissertations": _BASE_SORTS,
    "projects": _BASE_SORTS,
    "data": _BASE_SORTS,
    "projectsAndProducts": _BASE_SORTS,
    "all": _BASE_SORTS | {SortOrder.CRID_ASC},
    "researchers": frozenset({
        SortOrder.NEWEST, SortOrder.RELEVANCE, SortOrder.NAME_ASC, SortOrder.NAME_DESC,
        SortOrder.PROJECTS_DESC, SortOrder.PROJECTS_ASC,
        SortOrder.PRODUCTS_DESC, SortOrder.PRODUCTS_ASC,
    }),
}

# dataSourceType values as listed in the CiNii Research OpenSearch specification
# (support.nii.ac.jp/en/cir/r_opensearch, revision of 9 Dec 2024: CID removed,
# NDL_SEARCH added). An unknown value is not refused upstream; it returns zero
# records, which reads as an absence. So it is refused here.
DATA_SOURCE_TYPES = frozenset({
    "JALC", "IRDB", "CROSSREF", "DATACITE", "NDL_SEARCH", "NDL_DC", "RUDA", "JDCAT",
    "NINJAL", "DBPEDIA", "CIA", "CIB", "NACSIS_CAT", "KAKEN", "INTEGBIO", "PUBMED",
    "LSDB_ARCHIVE", "NIPR_ADS", "NIPR_SCIENCE_DB", "OPENAIRE", "PUBLIC_DATA",
})


class ResponseLang(str, Enum):
    JAPANESE = "ja"
    ENGLISH = "en"


# ==============================================================================
# HTTP
# ==============================================================================


def _redact(text: str) -> str:
    """Remove the appid in every form a URL or echo could carry it."""
    if CINII_APPID:
        for form in {CINII_APPID, quote(CINII_APPID, safe=""), quote_plus(CINII_APPID), quote(CINII_APPID)}:
            text = text.replace(form, "[redacted]")
    return text


def _transport_diag(exc: Exception) -> dict:
    if isinstance(exc, httpx.InvalidURL):
        return M.diag("error", "TRANSPORT_ERROR",
                      f"The request to CiNii could not be formed: {_redact(str(exc))[:300]}.",
                      "Shorten the query or remove control characters; nothing was sent.")
    return M.diag("error", "TRANSPORT_ERROR",
                  f"Network error reaching CiNii: {_redact(f'{type(exc).__name__}: {exc}')[:300]}.", "Retry shortly.")


async def _cinii_request(path: str, params: dict, base: str = OPENSEARCH_V2) -> Any:
    params = dict(params)
    params["appid"] = CINII_APPID
    params["format"] = "json"
    url = f"{base}/{path}" if path else base
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
        resp = await client.get(url, params=params)
    if resp.status_code == 404:
        return {"error": "Not found (HTTP 404)."}
    if resp.status_code >= 400:
        return {"error": f"CiNii API error {resp.status_code}: {_redact(resp.text)[:300]}"}
    try:
        return resp.json()
    except ValueError:
        # CiNii answers some malformed requests (a researcher CRID passed as
        # researcherId to /projects, for one) with HTTP 200 and its HTML
        # "ただいまご利用いただけません" page. Until 3.3.0 the JSON decode error
        # escaped to _run's transport handler and was reported as "Network
        # error reaching CiNii": the request had completed, and the server
        # had answered, with an error.
        m = re.search(r"<title>\s*([^<]{1,120})", resp.text or "")
        page = f' (an HTML page titled "{m.group(1).strip()}")' if m else ""
        return {"error": f"CiNii answered HTTP {resp.status_code} with a body that is not JSON{page}."}


def _not_shape(expected: str, data: Any) -> dict:
    said = ""
    if isinstance(data, dict):
        msg = data.get("error") or data.get("message")
        if msg:
            said = f' It said: "{str(msg)[:200]}".'
        shape = f"an object with keys {sorted(map(str, data))[:6]}"
    else:
        shape = "null" if data is None else f"a {type(data).__name__}"
    return M.diag(
        "error", "API_ERROR",
        f"CiNii answered 200, but with {shape} rather than {expected}.{said}",
        "The result is unknown, not empty. Retry before concluding anything "
        "about the literature.",
    )


async def _run(path: str, qp: dict) -> tuple[Optional[dict], Optional[dict]]:
    """Return (data, error_diag). error_diag is a typed diagnostic or None."""
    try:
        data = await _cinii_request(path, qp)
    except Exception as exc:  # noqa: BLE001
        return None, _transport_diag(exc)
    if isinstance(data, dict) and "error" in data:
        return None, M.diag("error", "API_ERROR", str(data["error"]), None)
    # opensearch:totalResults is required by the CiNii OpenSearch spec. A 200
    # body without it (null, {}, a gateway's error object) is an answer that did
    # not arrive; read as a page it came back as ZERO_CONJUNCTION, an outage
    # reported as an un-indexed compound.
    if not (isinstance(data, dict) and "opensearch:totalResults" in data
            and isinstance(data.get("items", []), list)):
        return None, _not_shape("a search results page", data)
    return data, None


async def _run_nacsis(path: str, qp: dict) -> tuple[Optional[dict], Optional[dict]]:
    """The holdings and library endpoints added on 30 March 2026 (the CiNii
    Books integration). They sit outside /opensearch/v2, at /opensearch/holder
    and /opensearch/library, and their JSON-LD wraps the channel in @graph
    rather than returning it at the top level. Returns the unwrapped channel."""
    try:
        data = await _cinii_request(path, qp, base=OPENSEARCH_V1)
    except Exception as exc:  # noqa: BLE001
        return None, _transport_diag(exc)
    if isinstance(data, dict) and "error" in data:
        return None, M.diag("error", "API_ERROR", str(data["error"]), None)
    channel = data
    if isinstance(data, dict) and isinstance(data.get("@graph"), list) and data["@graph"]:
        channel = data["@graph"][0]
    if not (isinstance(channel, dict) and "opensearch:totalResults" in channel
            and isinstance(channel.get("items", []), list)):
        return None, _not_shape("a holdings or library results page", data)
    return channel, None


# ==============================================================================
# CiNii JSON-LD -> envelope item
# ==============================================================================


def _split_lang(s: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Route a string to (ja, en) by detected script."""
    if not s:
        return (None, None)
    return (s, None) if M.detect_script(s) in ("han", "kana", "han_kana", "mixed") else (None, s)


_RECORD_TYPE = {
    "Article": "article", "Book": "book", "Data": "dataset", "Dataset": "dataset",
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


def _pair(v) -> tuple[Optional[str], Optional[str]]:
    """(ja, en) from a JSON-LD literal list. Language-tagged values go where
    their tag says; an untagged value is routed by its script, once. Until
    3.0.1 the untagged case was read twice — `_lit(v, "ja")` and `_lit(v, "en")`
    both fell through to the same value — so a Japanese title was also reported
    as its own English title."""
    if v is None:
        return (None, None)
    if isinstance(v, (str, dict)):
        return _split_lang(_lit(v))
    ja = en = None
    untagged: list[str] = []
    for x in v:
        if isinstance(x, str):
            untagged.append(x)
        elif isinstance(x, dict) and x.get("@value"):
            lang = x.get("@language")
            if lang == "ja" and ja is None:
                ja = x["@value"]
            elif lang == "en" and en is None:
                en = x["@value"]
            elif not lang:
                untagged.append(x["@value"])
    for s in untagged:
        sja, sen = _split_lang(s)
        ja = ja or sja
        en = en or sen
    return (ja, en)


def _typed_identifier(entries, kind: str) -> Optional[str]:
    """`{"identifier": {"@type": kind, "@value": ...}}` or `{"@type": kind, ...}`."""
    if not isinstance(entries, list):
        return None
    for e in entries:
        node = e.get("identifier", e) if isinstance(e, dict) else None
        if isinstance(node, dict) and node.get("@type") == kind and node.get("@value"):
            return str(node["@value"])
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

    title_ja, title_en = _pair(d.get("dc:title"))

    # Authors sit under `creator` as Researcher nodes with language-tagged
    # foaf:name lists; `dc:creator` is the older flat form. Until 3.0.1 only
    # the flat form was read, so a fetched record came back with no authors.
    authors = []
    for c in (d.get("creator") or [])[:12]:
        if isinstance(c, dict):
            cja, cen = _pair(c.get("foaf:name"))
            if cja or cen:
                authors.append({"ja": cja, "en": cen})
    if not authors:
        dcc = d.get("dc:creator")
        if isinstance(dcc, str):
            dcc = [dcc]
        if isinstance(dcc, list):
            for c in dcc[:12]:
                name = c if isinstance(c, str) else _lit(c)
                if name:
                    cja, cen = _split_lang(name)
                    authors.append({"ja": cja, "en": cen})

    journal_ja = journal_en = volume = issue = pages = year = doi = issn = None
    naid = _typed_identifier(d.get("productIdentifier"), "NAID")
    pub = d.get("publication")
    if isinstance(pub, dict):
        journal_ja, journal_en = _pair(pub.get("prism:publicationName"))
        issn = _typed_identifier(pub.get("publicationIdentifier"), "ISSN")
        volume = pub.get("prism:volume")
        issue = pub.get("prism:number")
        sp, ep = pub.get("prism:startingPage"), pub.get("prism:endingPage")
        pages = f"{sp}-{ep}" if sp and ep else (sp or None)
        mm = re.search(r"(\d{4})", str(pub.get("prism:publicationDate") or ""))
        year = int(mm.group(1)) if mm else None
    if record_type == "book":
        pubs = d.get("dcterms:publisher")
        if isinstance(pubs, list) and pubs and isinstance(pubs[0], dict):
            pja, pen = _pair(pubs[0].get("dc:publisher"))
            journal_ja, journal_en = journal_ja or pja, journal_en or pen
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
        year=year, doi=doi, crid=crid, naid=naid, issn=issn, url_ja=url,
        matched_in="metadata", record_type=record_type,
    )


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _item_from_cinii(raw: dict, record_type: str, per_item_type: bool = False) -> dict:
    """`per_item_type`: take the record type from the item's own dc:type
    (Article, Book, Dataset, Project, ...) and fall back to `record_type`.
    Used by the searches that mix types: all, data, projectsAndProducts."""
    if per_item_type:
        own = raw.get("dc:type")
        record_type = _RECORD_TYPE.get(own if isinstance(own, str) else "", record_type)
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
        extra=_book_ids(raw),
    )


def _book_ids(raw: dict) -> dict:
    """NCID and ISBN from dc:identifier ({"@type": "cir:NCID", ...}). They were
    dropped, so a book found by search could not be passed to
    cinii_get_holdings without a cinii_get_record call in between."""
    out: dict = {}
    ids = raw.get("dc:identifier")
    for kind, key in (("cir:NCID", "ncid"), ("cir:ISBN", "isbn")):
        vals = [str(e["@value"]) for e in (ids if isinstance(ids, list) else [])
                if isinstance(e, dict) and e.get("@type") == kind and e.get("@value")]
        if vals:
            out[key] = vals[0] if key == "ncid" and len(vals) == 1 else vals
    return out


# A record whose fields have the wrong types (a string where an object
# belongs, a number for a title) raised out of the builder and took the whole
# page with it: the caller got an exception, no envelope and no receipt.
_UNREADABLE = (AttributeError, TypeError, ValueError, KeyError, IndexError, OverflowError)


def _build_all(records: list, build, id_key: str) -> tuple[list[dict], Optional[dict]]:
    """Build every record that can be read; skip and name the rest.

    Returns (items, diagnostic-or-None). The diagnostic is RECORD_UNREADABLE,
    naming each skipped record by its position on the page and, where it has
    one, its identifier."""
    items: list[dict] = []
    bad: list[str] = []
    for pos, rec in enumerate(records, 1):
        try:
            items.append(build(rec))  # a non-object record fails here too
        except _UNREADABLE:
            rid = rec.get(id_key) if isinstance(rec, dict) else None
            bad.append(f"#{pos}" + (f" ({rid[:100]})" if isinstance(rid, str) and rid else ""))
    if not bad:
        return items, None
    named = "; ".join(bad[:10]) + ("; and more" if len(bad) > 10 else "")
    return items, M.diag(
        "warning", "RECORD_UNREADABLE",
        f"{len(bad)} of {len(records)} record(s) could not be read and were left out: {named}.",
        "The upstream record is malformed, not absent. The other records are complete; "
        "look the omitted ones up by identifier or retry later.",
    )


def _count(v: Any) -> Optional[int]:
    """A count from the API: a non-negative integer, or None. JSON numbers such
    as 1e309 parse as infinity, and int() of that raises OverflowError;
    is_integer() is False for infinity and NaN. A negative or fractional count
    is not a count either."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, float):
        return int(v) if v.is_integer() and v >= 0 else None
    if isinstance(v, str) and v.strip().isdecimal():
        return int(v.strip())
    return None


def _envelope(
    *, operation: str, record_type: str, query_str: Optional[str], qp: dict,
    data: Optional[dict], error_diag: Optional[dict], monograph_note: bool = False,
    per_item_type: bool = False,
) -> str:
    items, bad = _build_all((data.get("items", []) if data else []),
                            lambda r: _item_from_cinii(r, record_type, per_item_type), "@id")
    total = _count(data.get("opensearch:totalResults")) if data is not None else 0
    if total is None:
        total = len(items)
    # A search made by identifier or filter alone (ncid=…, projectId=…) has no
    # free-text term. The envelope then records the criteria as sent, and the
    # romanisation warning is not raised: an NCID is Latin script by nature,
    # and the warning is about how a chosen term reaches the corpus.
    free_text = query_str is not None
    if not free_text:
        query_str = _criteria_label(qp)
    script = M.detect_script(query_str) if free_text else None

    ds: list[dict] = []
    if error_diag:
        ds.append(error_diag)
    if bad:
        ds.append(bad)
    if script == "latin":
        ds.append(M.diag(
            "warning", "SCRIPT_LATIN_QUERY",
            f"Query is Latin-script; CiNii matched romanized/English metadata only "
            f"({total} records). The Japanese-script form reaches a different, larger corpus.",
            "Re-issue in kanji/kana (e.g. 暴走族) to search the Japanese-language literature.",
        ))
    if not error_diag and not items and not bad:
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
    return M.emit(env)


def _build_params(base: dict, extra: dict) -> dict:
    """Drop unset values; send booleans as CiNii's literal "true"/"false"."""
    out = {}
    for k, v in {**base, **extra}.items():
        if v is None:
            continue
        out[k] = ("true" if v else "false") if isinstance(v, bool) else v
    return out


_PAGING_KEYS = ("count", "start", "lang", "sortorder", "p", "appid", "format")


def _criteria_label(qp: dict) -> str:
    """The search criteria as sent, for an envelope whose search had no free
    text: 'ncid=BC08431161', 'projectId=18K00968 languageType=ja'."""
    return " ".join(f"{k}={v}" for k, v in qp.items() if k not in _PAGING_KEYS)


# ==============================================================================
# Input models
# ==============================================================================


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
QUERY_MAX = 2000


_LANG_CODE = re.compile(r"^[a-z]{2}$")


def _csv(v: Optional[str]) -> Optional[list[str]]:
    if v is None:
        return None
    parts = [p.strip() for p in v.split(",")]
    if not all(parts):
        raise ValueError("empty entry in comma-separated list")
    return parts


_DESC = {
    "query": "Free-text search terms (Japanese or English). Optional when another criterion "
             "(an identifier, a title, a filter) is given.",
    "language": "Language(s) of the records, as ISO 639-1 codes, comma-separated: 'ja', "
                "'en', 'zh', 'ko', or 'ja,en'. Sent as languageType. Filters by the "
                "language catalogued for the record, not the language of the query.",
    "has_fulltext": "true: only records CiNii links to full text; false: only records it "
                    "does not. Sent as hasLinkToFullText.",
    "resource_type": "Resource type(s), comma-separated, as CiNii names them: e.g. 'journal "
                     "article', 'departmental bulletin paper', 'conference paper', 'review "
                     "article', 'book', 'Dataset'. Sent as resourceType.",
    "data_source": "Data source(s), comma-separated, e.g. 'JALC', 'IRDB', 'CROSSREF', "
                   "'NDL_SEARCH', 'NACSIS_CAT', 'KAKEN', 'CIA', 'CIB', 'DATACITE', "
                   "'JDCAT'. Sent as dataSourceType. Unknown values are refused here, "
                   "because CiNii answers them with zero records.",
    "researcher_id": "Researcher identifier(s), comma-separated: a KAKEN researcher number, "
                     "an NRID, a CiNii author ID (DA…) or a researcher CRID. Sent as "
                     "researcherId.",
}


class _SearchBase(BaseModel):
    """Paging, language of response, and the cross-cutting checks. Each
    subclass names its v2 search type in `_search_type`, which governs which
    sort orders it accepts."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid", str_max_length=QUERY_MAX)
    _search_type: ClassVar[str] = "all"
    _non_criteria: ClassVar[frozenset] = frozenset({"sort", "lang", "count", "start"})

    sort: SortOrder = Field(default=SortOrder.RELEVANCE, description=SORT_DESCRIPTION)
    lang: ResponseLang = Field(default=ResponseLang.ENGLISH)
    count: int = Field(default=20, ge=1, le=200)
    start: int = Field(default=1, ge=1, le=10000)

    @field_validator("language", check_fields=False)
    @classmethod
    def _iso639_1(cls, v: Optional[str]) -> Optional[str]:
        parts = _csv(v)
        if parts is None:
            return None
        parts = [p.lower() for p in parts]
        bad = [p for p in parts if not _LANG_CODE.match(p)]
        if bad:
            raise ValueError(f"language takes ISO 639-1 codes (ja, en, zh, ko); not {bad}")
        return ",".join(parts)

    @field_validator("data_source", check_fields=False)
    @classmethod
    def _known_source(cls, v: Optional[str]) -> Optional[str]:
        parts = _csv(v)
        if parts is None:
            return None
        parts = [p.upper() for p in parts]
        bad = [p for p in parts if p not in DATA_SOURCE_TYPES]
        if bad:
            raise ValueError(f"unknown data_source {bad}; one of {sorted(DATA_SOURCE_TYPES)}")
        return ",".join(parts)

    @model_validator(mode="after")
    def _sort_in_scope_and_some_criterion(self):
        allowed = _SORT_SCOPE[self._search_type]
        if self.sort not in allowed:
            names = ", ".join(s.name for s in SortOrder if s in allowed)
            raise ValueError(
                f"sort {self.sort.name} is not valid for {self._search_type} search "
                f"(CiNii would ignore it silently); use one of: {names}")
        if not any(v is not None for k, v in self if k not in self._non_criteria):
            raise ValueError("give a query or at least one other search criterion")
        return self


class ArticleSearchInput(_SearchBase):
    _search_type: ClassVar[str] = "articles"
    query: Optional[str] = Field(default=None, description=_DESC["query"], min_length=1)
    title: Optional[str] = Field(default=None)
    author: Optional[str] = Field(default=None)
    journal: Optional[str] = Field(default=None, description="Journal title. Sent as publicationTitle.")
    from_year: Optional[int] = Field(default=None, ge=1800)
    to_year: Optional[int] = Field(default=None)
    researcher_id: Optional[str] = Field(default=None, description=_DESC["researcher_id"])
    language: Optional[str] = Field(default=None, description=_DESC["language"])
    has_fulltext: Optional[bool] = Field(default=None, description=_DESC["has_fulltext"])
    resource_type: Optional[str] = Field(default=None, description=_DESC["resource_type"])
    data_source: Optional[str] = Field(default=None, description=_DESC["data_source"])


class BookSearchInput(_SearchBase):
    _search_type: ClassVar[str] = "books"
    query: Optional[str] = Field(default=None, description=_DESC["query"], min_length=1)
    title: Optional[str] = Field(default=None)
    exact_title: Optional[bool] = Field(default=None, description="true: match `title` exactly. Sent as isFullTitle.")
    author: Optional[str] = Field(default=None)
    publisher: Optional[str] = Field(default=None)
    isbn: Optional[str] = Field(default=None)
    ncid: Optional[str] = Field(default=None, description="NACSIS-CAT record ID(s), comma-separated (e.g. BC08431161).")
    category: Optional[str] = Field(default=None, description="Classification, comma-separated (e.g. an NDC number such as 379). Sent as category.")
    from_year: Optional[int] = Field(default=None, ge=1800)
    to_year: Optional[int] = Field(default=None)
    researcher_id: Optional[str] = Field(default=None, description=_DESC["researcher_id"])
    language: Optional[str] = Field(default=None, description=_DESC["language"])
    has_fulltext: Optional[bool] = Field(default=None, description=_DESC["has_fulltext"])
    resource_type: Optional[str] = Field(default=None, description=_DESC["resource_type"])
    data_source: Optional[str] = Field(default=None, description=_DESC["data_source"])


class DissertationSearchInput(_SearchBase):
    _search_type: ClassVar[str] = "dissertations"
    query: Optional[str] = Field(default=None, description=_DESC["query"], min_length=1)
    author: Optional[str] = Field(default=None)
    award_institution: Optional[str] = Field(default=None, description="Degree-granting university (e.g. 東京大学). Sent as awardInstitution.")
    degree: Optional[str] = Field(default=None, description="Degree title as catalogued (e.g. 博士（教育学）). Sent as degree.")
    from_year: Optional[int] = Field(default=None, ge=1800)
    to_year: Optional[int] = Field(default=None)
    language: Optional[str] = Field(default=None, description=_DESC["language"])
    has_fulltext: Optional[bool] = Field(default=None, description=_DESC["has_fulltext"])
    data_source: Optional[str] = Field(default=None, description=_DESC["data_source"])


class KakenSearchInput(_SearchBase):
    _search_type: ClassVar[str] = "projects"
    query: Optional[str] = Field(default=None, description=_DESC["query"], min_length=1)
    researcher: Optional[str] = Field(default=None)
    institution: Optional[str] = Field(default=None)
    project_id: Optional[str] = Field(default=None, description="KAKEN project or area number(s), comma-separated (e.g. 18K00968). Sent as projectId.")
    researcher_id: Optional[str] = Field(default=None, description=_DESC["researcher_id"])
    from_year: Optional[int] = Field(default=None, ge=1900)
    to_year: Optional[int] = Field(default=None)
    language: Optional[str] = Field(default=None, description=_DESC["language"])
    data_source: Optional[str] = Field(default=None, description=_DESC["data_source"])


class CrossSearchInput(_SearchBase):
    _search_type: ClassVar[str] = "all"
    query: Optional[str] = Field(default=None, description=_DESC["query"], min_length=1)
    from_year: Optional[int] = Field(default=None, ge=1800)
    to_year: Optional[int] = Field(default=None)
    researcher_id: Optional[str] = Field(default=None, description=_DESC["researcher_id"])
    language: Optional[str] = Field(default=None, description=_DESC["language"])
    has_fulltext: Optional[bool] = Field(default=None, description=_DESC["has_fulltext"])
    data_source: Optional[str] = Field(default=None, description=_DESC["data_source"])


class DataSearchInput(_SearchBase):
    _search_type: ClassVar[str] = "data"
    query: Optional[str] = Field(default=None, description=_DESC["query"], min_length=1)
    title: Optional[str] = Field(default=None)
    author: Optional[str] = Field(default=None)
    publication_title: Optional[str] = Field(default=None, description="Title of the publication the dataset belongs to. Sent as publicationTitle.")
    dataset_format: Optional[str] = Field(default=None, description="Format(s) the data is provided in, comma-separated (OR). Sent as datasetFormat.")
    from_year: Optional[int] = Field(default=None, ge=1800)
    to_year: Optional[int] = Field(default=None)
    language: Optional[str] = Field(default=None, description=_DESC["language"])
    has_fulltext: Optional[bool] = Field(default=None, description=_DESC["has_fulltext"])
    resource_type: Optional[str] = Field(default=None, description=_DESC["resource_type"])
    data_source: Optional[str] = Field(default=None, description=_DESC["data_source"])


class ProjectsAndProductsSearchInput(_SearchBase):
    _search_type: ClassVar[str] = "projectsAndProducts"
    query: Optional[str] = Field(default=None, description=_DESC["query"], min_length=1)
    title: Optional[str] = Field(default=None)
    researcher_id: Optional[str] = Field(default=None, description=_DESC["researcher_id"])
    institution: Optional[str] = Field(default=None, description="Affiliation. Sent as affiliation.")
    from_year: Optional[int] = Field(default=None, ge=1800)
    to_year: Optional[int] = Field(default=None)
    language: Optional[str] = Field(default=None, description=_DESC["language"])
    has_fulltext: Optional[bool] = Field(default=None, description=_DESC["has_fulltext"])
    data_source: Optional[str] = Field(default=None, description=_DESC["data_source"])


class ResearcherSearchInput(_SearchBase):
    _search_type: ClassVar[str] = "researchers"
    query: Optional[str] = Field(default=None, description=_DESC["query"], min_length=1)
    institution: Optional[str] = Field(default=None)
    researcher_id: Optional[str] = Field(default=None, description=_DESC["researcher_id"])
    data_source: Optional[str] = Field(default=None, description=_DESC["data_source"])


# ---- NACSIS-CAT holdings and libraries (/opensearch/holder, /opensearch/library)

class Participation(str, Enum):
    PARTICIPANT = "A"
    NOT_PARTICIPANT = "N"


class Availability(str, Enum):
    AVAILABLE = "A"
    NOT_AVAILABLE = "N"


class ServiceStatus(str, Enum):
    """Copy and loan service. C: the request must go through another library."""
    AVAILABLE = "A"
    VIA_OTHER_LIBRARY = "C"
    NOT_AVAILABLE = "N"


class FaxStatus(str, Enum):
    AVAILABLE = "A"
    WITH_CONDITIONS = "C"
    NOT_AVAILABLE = "N"


_NCID = re.compile(r"^[A-Z]{2}[0-9]{7}[0-9X]$")
_FANO = re.compile(r"^FA[0-9]{5}[0-9X]$")
HOLDINGS_WINDOW_MAX = 1000


class HoldingsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid", str_max_length=QUERY_MAX)
    ncid: str = Field(..., description="NACSIS-CAT record ID of the book or journal, e.g. BC08431161 "
                      "(take it from cinii_search_books, ids or the record's dc:identifier).")
    library_id: Optional[str] = Field(default=None, description="Restrict to one library by its NACSIS-CAT library ID "
                                      "(FA…; find it with cinii_search_libraries). Sent as fano.")
    ill_member: Optional[Participation] = Field(default=None, description="ILL participation. Sent as ill.")
    ill_status: Optional[Availability] = Field(default=None, description="ILL currently accepted. Sent as ill_stat.")
    copy_service: Optional[ServiceStatus] = Field(default=None, description="Copy service. Sent as ill_copys.")
    loan_service: Optional[ServiceStatus] = Field(default=None, description="Loan service. Sent as ill_loans.")
    fax_service: Optional[FaxStatus] = Field(default=None, description="FAX service. Sent as ill_faxs.")
    japan_us_ill: Optional[Participation] = Field(default=None, description="Japan–US ILL (via OCLC) participation. Sent as ill_oclc.")
    japan_korea_ill: Optional[Participation] = Field(default=None, description="Japan–Korea ILL (via KERIS) participation. Sent as ill_keris.")
    ill_offset: Optional[Participation] = Field(default=None, description="ILL offset (fee clearing) participation. Sent as ill_offset.")
    year: Optional[int] = Field(default=None, ge=1000, le=2999, description="Journals only: holding includes this year. A single year, not a range.")
    volume: Optional[str] = Field(default=None, description="Journals only: holding includes this volume. Sent as vol.")
    issue: Optional[str] = Field(default=None, description="Journals only: holding includes this number. Sent as issue.")
    continuing: Optional[bool] = Field(default=None, description="Journals only: true for libraries still receiving the title. Sent as cont.")
    count: int = Field(default=100, ge=1, le=HOLDINGS_WINDOW_MAX,
                       description="How many holding libraries to return. CiNii sends the full list in one "
                                   "response; this window is cut here, and result.total keeps CiNii's count.")
    start: int = Field(default=1, ge=1)

    @field_validator("ncid")
    @classmethod
    def _ncid(cls, v: str) -> str:
        v = v.upper()
        if not _NCID.match(v):
            raise ValueError("ncid is two letters and eight characters, e.g. BC08431161 or AN00247656")
        return v

    @field_validator("library_id")
    @classmethod
    def _fano(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.upper()
        if not _FANO.match(v):
            raise ValueError("library_id is a NACSIS-CAT library ID such as FA013006")
        return v


class LibrarySort(str, Enum):
    RELEVANCE = "1"
    LIBRARY_ID_ASC = "2"
    LIBRARY_ID_DESC = "3"
    PREFECTURE_ASC = "4"
    PREFECTURE_DESC = "5"
    NAME_ASC = "6"
    NAME_DESC = "7"


class LibrarySearchInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid", str_max_length=QUERY_MAX)
    name: str = Field(..., min_length=1, description="Library name, partial match (Japanese or a catalogued "
                      "alternative: 'SOAS' finds ロンドン大学 SOAS図書館). Several words are ANDed.")
    sort: LibrarySort = Field(default=LibrarySort.RELEVANCE, description="Sort: relevance, library ID, prefecture code or name.")
    count: int = Field(default=20, ge=1, le=200)
    page: int = Field(default=1, ge=1, description="Page number. Sent as p.")


class RecordLookupInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid", str_max_length=QUERY_MAX)
    record_url: str = Field(..., description="Full CiNii URL or CRID", min_length=1, max_length=QUERY_MAX)

    @field_validator("record_url")
    @classmethod
    def _no_control(cls, v: str) -> str:
        if _CONTROL.search(v):
            raise ValueError("record_url contains a control character")
        return v


# ==============================================================================
# Tools
# ==============================================================================


_RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}


def _common(p: _SearchBase) -> dict:
    """Paging, sort, response language and the filters shared across types.
    getattr: not every model carries every filter."""
    return {
        "q": getattr(p, "query", None), "count": p.count, "start": p.start,
        # Articles have always sent sortorder; the other types sent none and
        # took CiNii's default (relevance). Sending the default now would
        # change the receipt hash of every unchanged call, so it is sent only
        # where it was before or where it is not the default.
        "sortorder": p.sort.value if (p._search_type == "articles" or p.sort is not SortOrder.RELEVANCE) else None,
        "lang": p.lang.value,
        "languageType": getattr(p, "language", None),
        "hasLinkToFullText": getattr(p, "has_fulltext", None),
        "resourceType": getattr(p, "resource_type", None),
        "dataSourceType": getattr(p, "data_source", None),
        "researcherId": getattr(p, "researcher_id", None),
    }


@mcp.tool(name="cinii_search_articles", annotations=_RO)
async def cinii_search_articles(params: ArticleSearchInput) -> str:
    """Search CiNii Research for journal articles. Returns the unified envelope.

    CiNii matches catalogued metadata and ANDs a multi-word query, so an
    un-indexed compound returns zero even when related work exists — a
    ZERO_CONJUNCTION diagnostic marks this; vary the rendering rather than
    concluding the literature is absent. A SCRIPT_LATIN_QUERY diagnostic means
    the query searched romanized metadata only. The same string may behave very
    differently on J-STAGE (full text). Foundational monographs sit in
    cinii_search_books, not the article index.

    `language` filters by the language catalogued for the record (languageType):
    'ja' for Japanese-language scholarship first, 'zh' or 'ko' for Chinese or
    Korean work indexed in CiNii.
    """
    qp = _build_params(_common(params), {
        "title": params.title, "creator": params.author, "publicationTitle": params.journal,
        "from": params.from_year, "until": params.to_year,
    })
    data, err = await _run("articles", qp)
    return _envelope(operation="search_articles", record_type="article", query_str=params.query, qp=qp, data=data, error_diag=err, monograph_note=True)


@mcp.tool(name="cinii_search_books", annotations=_RO)
async def cinii_search_books(params: BookSearchInput) -> str:
    """Search CiNii Research for books and monographs (NACSIS-CAT, NDL Search).
    Returns the unified envelope.

    A book can be looked up by `ncid` or `isbn` without a query. To find which
    libraries hold it, pass its NCID to cinii_get_holdings.
    """
    qp = _build_params(_common(params), {
        "title": params.title, "isFullTitle": params.exact_title, "creator": params.author,
        "publisher": params.publisher, "isbn": params.isbn, "ncid": params.ncid,
        "category": params.category, "from": params.from_year, "until": params.to_year,
    })
    data, err = await _run("books", qp)
    return _envelope(operation="search_books", record_type="book", query_str=params.query, qp=qp, data=data, error_diag=err)


@mcp.tool(name="cinii_search_dissertations", annotations=_RO)
async def cinii_search_dissertations(params: DissertationSearchInput) -> str:
    """Search CiNii Research for doctoral dissertations. Returns the unified envelope.

    `award_institution` and `degree` filter by the degree-granting university and
    the degree title as catalogued (博士（文学）, 博士（教育学）, …).
    """
    qp = _build_params(_common(params), {
        "creator": params.author, "awardInstitution": params.award_institution,
        "degree": params.degree, "from": params.from_year, "until": params.to_year,
    })
    data, err = await _run("dissertations", qp)
    return _envelope(operation="search_dissertations", record_type="dissertation", query_str=params.query, qp=qp, data=data, error_diag=err)


@mcp.tool(name="cinii_search_kaken", annotations=_RO)
async def cinii_search_kaken(params: KakenSearchInput) -> str:
    """Search KAKEN (科研費) research projects. Returns the unified envelope (record_type 'project').

    A project can be looked up by `project_id` (e.g. 18K00968) without a query.
    """
    qp = _build_params(_common(params), {
        "creator": params.researcher, "affiliation": params.institution,
        "projectId": params.project_id, "from": params.from_year, "until": params.to_year,
    })
    data, err = await _run("projects", qp)
    return _envelope(operation="search_projects", record_type="project", query_str=params.query, qp=qp, data=data, error_diag=err)


@mcp.tool(name="cinii_search_all", annotations=_RO)
async def cinii_search_all(params: CrossSearchInput) -> str:
    """Cross-type search across all CiNii content. Returns the unified envelope.

    The cross search mixes types. Each record's type is taken from its own
    dc:type (article, book, dataset, dissertation, project, researcher); a
    record without one is reported as 'article'.
    """
    qp = _build_params(_common(params), {"from": params.from_year, "until": params.to_year})
    data, err = await _run("all", qp)
    return _envelope(operation="search_all", record_type="article", query_str=params.query, qp=qp, data=data, error_diag=err, per_item_type=True)


@mcp.tool(name="cinii_search_data", annotations=_RO)
async def cinii_search_data(params: DataSearchInput) -> str:
    """Search CiNii Research for research data (datasets). Returns the unified envelope.

    Covers data deposited through DataCite, JDCAT, IRDB, institutional data
    archives and, since 21 April 2026, research data recorded in KAKEN. Records
    are typed from their own dc:type, normally 'dataset'.
    """
    qp = _build_params(_common(params), {
        "title": params.title, "creator": params.author, "publicationTitle": params.publication_title,
        "datasetFormat": params.dataset_format, "from": params.from_year, "until": params.to_year,
    })
    data, err = await _run("data", qp)
    return _envelope(operation="search_data", record_type="dataset", query_str=params.query, qp=qp, data=data, error_diag=err, per_item_type=True)


@mcp.tool(name="cinii_search_projects_and_products", annotations=_RO)
async def cinii_search_projects_and_products(params: ProjectsAndProductsSearchInput) -> str:
    """Search KAKEN projects together with their research products (articles,
    books, data, dissertations) in one result set. Returns the unified envelope.

    Useful with `researcher_id` to list a researcher's grants and outputs
    together. Records are typed from their own dc:type.
    """
    qp = _build_params(_common(params), {
        "title": params.title, "affiliation": params.institution,
        "from": params.from_year, "until": params.to_year,
    })
    data, err = await _run("projectsAndProducts", qp)
    return _envelope(operation="search_projects_and_products", record_type="article", query_str=params.query, qp=qp, data=data, error_diag=err, per_item_type=True)


@mcp.tool(name="cinii_search_researchers", annotations=_RO)
async def cinii_search_researchers(params: ResearcherSearchInput) -> str:
    """Search for researchers in CiNii. Returns the unified envelope (record_type 'researcher').

    Note: researcher affiliation is not carried by the record schema; the
    researcher name occupies the title field and the profile URL the ids.url_ja
    field. Researcher sorts: NAME_ASC/NAME_DESC, PROJECTS_DESC/PROJECTS_ASC,
    PRODUCTS_DESC/PRODUCTS_ASC, NEWEST, RELEVANCE.
    """
    qp = _build_params(_common(params), {"affiliation": params.institution})
    data, err = await _run("researchers", qp)
    return _envelope(operation="search_researchers", record_type="researcher", query_str=params.query, qp=qp, data=data, error_diag=err)


# ---- NACSIS-CAT holdings and libraries

HOLDINGS_MATCHING = "identifier_exact"
LIBRARY_MATCHING = "library_name_match"
HOLDINGS_COVERAGE_NOTE = (
    "NACSIS-CAT holdings: libraries that catalogue in NACSIS-CAT, chiefly Japanese "
    "university and research libraries, with some overseas libraries. It is not a census of "
    "copies. A library missing from this list may still hold the item without having "
    "registered it."
)


def _fano_of(raw: dict) -> Optional[str]:
    for e in raw.get("dc:identifier") or []:
        if isinstance(e, dict) and e.get("@type") == "cir:FANO" and e.get("@value"):
            return str(e["@value"])
    return None


def _item_from_library(raw: dict, record_type: str) -> dict:
    name = raw.get("title")
    name = name if isinstance(name, str) else None
    nja, nen = _split_lang(name)
    link = raw.get("@id") if isinstance(raw.get("@id"), str) else None
    cat = raw.get("cir:catalogUrl")
    cat = cat.get("@id") if isinstance(cat, dict) else None
    extra = {"library_id": _fano_of(raw)}
    if cat:
        extra["catalog_url"] = cat
    return M.make_item(
        title_ja=nja, title_en=nen, holding_org=name, url_ja=link,
        fulltext_url=None, matched_in="identifier" if record_type == "holding" else "metadata",
        record_type=record_type, extra=extra,
    )


@mcp.tool(name="cinii_get_holdings", annotations=_RO)
async def cinii_get_holdings(params: HoldingsInput) -> str:
    """List the libraries that hold a book or journal, by its NCID, from
    NACSIS-CAT through CiNii Research (/opensearch/holder, live since 30 March
    2026). Returns the unified envelope (operation 'get_holdings', record_type
    'holding'); each item names a library, its page, its NACSIS-CAT library ID
    (extra.library_id) and, where the library supplies one, a link into its own
    catalogue for this title (extra.catalog_url).

    Filters narrow the list to libraries that take ILL requests, offer copy or
    loan service, belong to Japan–US or Japan–Korea ILL, or (journals) hold a
    given year, volume or number. Zero holdings is HOLDINGS_NONE_REGISTERED,
    not proof that no library has the item.
    """
    qp = _build_params({"ncid": params.ncid}, {
        "fano": params.library_id,
        "ill": params.ill_member.value if params.ill_member else None,
        "ill_stat": params.ill_status.value if params.ill_status else None,
        "ill_copys": params.copy_service.value if params.copy_service else None,
        "ill_loans": params.loan_service.value if params.loan_service else None,
        "ill_faxs": params.fax_service.value if params.fax_service else None,
        "ill_oclc": params.japan_us_ill.value if params.japan_us_ill else None,
        "ill_keris": params.japan_korea_ill.value if params.japan_korea_ill else None,
        "ill_offset": params.ill_offset.value if params.ill_offset else None,
        "year": params.year, "vol": params.volume, "issue": params.issue,
        "cont": None if params.continuing is None else ("1" if params.continuing else "0"),
    })
    data, err = await _run_nacsis("holder", qp)
    raw_items = data.get("items", []) if data else []
    items, bad = _build_all(raw_items, lambda r: _item_from_library(r, "holding"), "@id")
    total = _count(data.get("opensearch:totalResults")) if data is not None else 0
    if total is None:
        total = len(items)
    window = items[params.start - 1: params.start - 1 + params.count]

    ds: list[dict] = [d for d in (err, bad) if d]
    if not err and not raw_items:
        ds.append(M.diag(
            "warning", "HOLDINGS_NONE_REGISTERED",
            f"No library has registered a holding of {params.ncid} in NACSIS-CAT"
            + (" under these filters." if len(qp) > 1 else "."),
            "This is a statement about NACSIS-CAT, not about the world's libraries. Check the NCID "
            "with cinii_search_books, drop the filters, or look in the library's own catalogue.",
        ))
    if not ds:
        ds.append(M.diag("info", "OK", f"{total} holding librar{'y' if total == 1 else 'ies'} registered in NACSIS-CAT.", None))

    env = M.build_envelope(
        server="cinii", operation="get_holdings", input_terms=params.ncid, normalized=params.ncid,
        params={**qp, "count": params.count, "start": params.start},
        matching_mode=HOLDINGS_MATCHING, total=total, start=params.start,
        items=window, diagnostics=ds, attribution=ATTRIBUTION, coverage_note=HOLDINGS_COVERAGE_NOTE,
    )
    return M.emit(env)


@mcp.tool(name="cinii_search_libraries", annotations=_RO)
async def cinii_search_libraries(params: LibrarySearchInput) -> str:
    """Find NACSIS-CAT member libraries by name (/opensearch/library, live since
    30 March 2026). Returns the unified envelope (record_type 'library'); each
    item carries the library's NACSIS-CAT ID in extra.library_id, which
    cinii_get_holdings takes as library_id.
    """
    qp = _build_params({"name": params.name}, {
        "sortorder": params.sort.value, "count": params.count, "p": params.page,
    })
    data, err = await _run_nacsis("library", qp)
    items, bad = _build_all((data.get("items", []) if data else []),
                            lambda r: _item_from_library(r, "library"), "@id")
    total = _count(data.get("opensearch:totalResults")) if data is not None else 0
    if total is None:
        total = len(items)
    ds: list[dict] = [d for d in (err, bad) if d]
    if not err and not items and not bad:
        ds.append(M.diag(
            "warning", "LIBRARY_NONE_MATCHED",
            "No NACSIS-CAT library matched this name.",
            "Library names are catalogued in Japanese (ロンドン大学 SOAS図書館), with some "
            "alternative names. Try the Japanese form or a single distinctive word.",
        ))
    if not ds:
        ds.append(M.diag("info", "OK", f"{total} librar{'y' if total == 1 else 'ies'} matched.", None))
    env = M.build_envelope(
        server="cinii", operation="search_libraries", input_terms=params.name, normalized=params.name,
        params=qp, matching_mode=LIBRARY_MATCHING, total=total,
        start=(params.page - 1) * params.count + 1,
        items=items, diagnostics=ds, attribution=ATTRIBUTION,
    )
    return M.emit(env)


@mcp.tool(name="cinii_get_record", annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def cinii_get_record(params: RecordLookupInput) -> str:
    """Fetch a single CiNii record by URL or CRID. Returns the unified envelope (operation 'get_record')."""
    raw = params.record_url.strip()
    if "/crid/" in raw:
        # A pasted URL may carry ?lang=en or a fragment; neither is part of the CRID.
        raw = urlsplit(raw).path if "://" in raw else raw.split("?", 1)[0].split("#", 1)[0]
    crid = raw.rsplit("/crid/", 1)[-1].strip("/") if "/crid/" in raw else raw
    url = f"{BASE_URL}/crid/{quote(crid, safe='')}.json"  # JSON-LD record; the bare /crid/ page is HTML
    record, err = None, None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            resp = await client.get(url, params={"appid": CINII_APPID, "format": "json"})
        if resp.status_code >= 400:
            err = M.diag("error", "API_ERROR", f"CiNii returned HTTP {resp.status_code} for this record.", "Check the CRID and retry.")
        else:
            try:
                record = resp.json()
            except ValueError:
                err = M.diag("error", "API_ERROR", "CiNii did not return a JSON record for this identifier.", "Confirm the CRID identifies an indexed record.")
            else:
                # A JSON-LD record names itself in @id. Anything else ({}, null,
                # an error object) used to come back as "Single record resolved."
                if not (isinstance(record, dict) and isinstance(record.get("@id"), str)):
                    err, record = _not_shape("a JSON-LD record", record), None
    except Exception as exc:  # noqa: BLE001
        err = _transport_diag(exc)

    items, bad = _build_all([record], _item_from_record, "@id") if isinstance(record, dict) else ([], None)
    total = len(items)
    ds = [err] if err else [bad] if bad else [M.diag("info", "OK", "Single record resolved.", None)]
    env = M.build_envelope(
        server="cinii", operation="get_record", input_terms=params.record_url, normalized=crid,
        params={"record_url": params.record_url}, matching_mode=MATCHING_MODE, total=total, start=1,
        items=items, diagnostics=ds, attribution=ATTRIBUTION,
    )
    return M.emit(env)


def main() -> None:
    """Console-script entry point (`cinii-mcp`)."""
    mcp.run()


if __name__ == "__main__":
    main()
