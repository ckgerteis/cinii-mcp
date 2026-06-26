"""
CiNii Research MCP Server
==========================
An MCP server for searching Japan's national academic information database (CiNii Research),
operated by the National Institute of Informatics (NII). Provides tools for searching
articles, books, dissertations, research projects (KAKEN), datasets, and researchers
via the CiNii Research OpenSearch v2 API.

CiNii Research aggregates metadata from KAKEN, CiNii Articles, CiNii Books, IRDB,
Crossref, DataCite, PubMed, NDL Search, and other sources.

Requires an Application ID (appid) from NII: https://support.nii.ac.jp/en/cinii/api/developer
Set via environment variable CINII_APPID.
"""

import json
import urllib.parse
from typing import Optional, List
from enum import Enum

import httpx
from pydantic import BaseModel, Field, ConfigDict, field_validator
from mcp.server.fastmcp import FastMCP

# ==============================================================================
# Configuration
# ==============================================================================

import os
CINII_APPID = os.environ.get("CINII_APPID", "")
BASE_URL = "https://cir.nii.ac.jp"
OPENSEARCH_V2 = f"{BASE_URL}/opensearch/v2"
TIMEOUT = 30.0
MAX_COUNT = 200

# ==============================================================================
# Server
# ==============================================================================

mcp = FastMCP("cinii_mcp")

# ==============================================================================
# Enums
# ==============================================================================


class SortOrder(str, Enum):
    NEWEST = "0"
    OLDEST = "1"
    RELEVANCE = "4"
    CITATIONS_DESC = "10"


class ResponseLang(str, Enum):
    JAPANESE = "ja"
    ENGLISH = "en"


class DataSourceType(str, Enum):
    JALC = "JALC"
    IRDB = "IRDB"
    CROSSREF = "CROSSREF"
    DATACITE = "DATACITE"
    NDL_SEARCH = "NDL_SEARCH"
    CIA = "CIA"
    CIB = "CIB"
    KAKEN = "KAKEN"
    PUBMED = "PUBMED"


# ==============================================================================
# HTTP helpers
# ==============================================================================


async def _cinii_request(path: str, params: dict) -> dict:
    """Make a request to CiNii Research API and return JSON-LD."""
    params["appid"] = CINII_APPID
    params["format"] = "json"

    url = f"{OPENSEARCH_V2}/{path}" if path else OPENSEARCH_V2
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, params=params)

    if resp.status_code == 404:
        return {"error": "Not found."}
    if resp.status_code >= 400:
        return {"error": f"CiNii API error {resp.status_code}: {resp.text[:500]}"}

    return resp.json()


async def _fetch_record(url: str) -> dict:
    """Fetch a single CiNii record by URL (JSON-LD)."""
    params = {"appid": CINII_APPID, "format": "json"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, params=params)
    if resp.status_code >= 400:
        return {"error": f"CiNii API error {resp.status_code}"}
    return resp.json()


def _format_item(item: dict) -> str:
    """Format a CiNii search result item as readable text."""
    parts = []
    title = item.get("title", item.get("dc:title", "Untitled"))
    if isinstance(title, list):
        title = title[0] if title else "Untitled"
    parts.append(f"**{title}**")

    # Authors / creators
    creators = item.get("dc:creator", [])
    if isinstance(creators, str):
        creators = [creators]
    if creators:
        parts.append(f"  Authors: {', '.join(creators[:8])}")

    # Publication info
    pub = item.get("prism:publicationName") or item.get("dc:publisher")
    if pub:
        parts.append(f"  Source: {pub}")

    date = item.get("prism:publicationDate") or item.get("dc:date")
    if date:
        parts.append(f"  Date: {date}")

    # Identifiers
    doi = item.get("prism:doi")
    if doi:
        parts.append(f"  DOI: {doi}")

    link = item.get("@id") or item.get("link", {}).get("@id")
    if link:
        parts.append(f"  URL: {link}")

    # Description / abstract
    desc = item.get("description") or item.get("dc:description")
    if desc:
        if isinstance(desc, list):
            desc = desc[0]
        parts.append(f"  Description: {desc[:400]}{'…' if len(str(desc))>400 else ''}")

    return "\n".join(parts)


def _format_results(data: dict, label: str, query: Optional[dict] = None) -> str:
    """Format CiNii search response into readable text.

    When `query` is supplied, the issued search terms are echoed back for
    reproducibility, and a zero-result response carries guidance on why a
    CiNii search can return nothing even when related work exists.
    """
    q_line = ""
    if query:
        shown = {k: v for k, v in query.items() if k not in ("appid", "format")}
        q_line = f"Query: {shown}\n"

    if "error" in data:
        return f"{q_line}Error: {data['error']}"

    items = data.get("items", [])
    total = data.get("opensearch:totalResults", "?")

    if not items:
        return (
            f"{q_line}No {label} results found for this query. "
            "CiNii matches catalogued metadata and treats a multi-word query as a "
            "conjunction (AND), so a compound it has not indexed returns zero even "
            "when related work exists. Treat a zero as a signal to vary the search "
            "and not as proof that the literature is absent: try a single key term, "
            "an alternative Japanese rendering (literal, emic, or combined), or a "
            "broader query, and tell the user which terms were searched. J-STAGE, "
            "which searches full text, may return results for the same string."
        )

    lines = [f"{q_line}**{label}** — {total} total results, showing {len(items)}\n"]
    for i, item in enumerate(items, 1):
        lines.append(f"---\n{i}. {_format_item(item)}")

    return "\n".join(lines)


# ==============================================================================
# Input Models
# ==============================================================================


class ArticleSearchInput(BaseModel):
    """Search CiNii for journal articles."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., description="Search query (Japanese or English)", min_length=1)
    title: Optional[str] = Field(default=None, description="Title search filter")
    author: Optional[str] = Field(default=None, description="Author name filter")
    journal: Optional[str] = Field(default=None, description="Journal name filter")
    from_year: Optional[int] = Field(default=None, description="Start year", ge=1800)
    to_year: Optional[int] = Field(default=None, description="End year")
    data_source: Optional[str] = Field(default=None, description="Data source (e.g., CROSSREF, KAKEN, IRDB)")
    sort: SortOrder = Field(default=SortOrder.RELEVANCE, description="Sort order")
    lang: ResponseLang = Field(default=ResponseLang.ENGLISH, description="Response language")
    count: int = Field(default=20, description="Number of results (1-200)", ge=1, le=200)
    start: int = Field(default=1, description="Start position", ge=1)


class BookSearchInput(BaseModel):
    """Search CiNii for books and monographs."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., description="Search query", min_length=1)
    title: Optional[str] = Field(default=None, description="Title search filter")
    author: Optional[str] = Field(default=None, description="Author name filter")
    publisher: Optional[str] = Field(default=None, description="Publisher name filter")
    isbn: Optional[str] = Field(default=None, description="ISBN filter")
    from_year: Optional[int] = Field(default=None, ge=1800)
    to_year: Optional[int] = Field(default=None)
    lang: ResponseLang = Field(default=ResponseLang.ENGLISH)
    count: int = Field(default=20, ge=1, le=200)
    start: int = Field(default=1, ge=1)


class DissertationSearchInput(BaseModel):
    """Search CiNii for doctoral dissertations."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., description="Search query", min_length=1)
    author: Optional[str] = Field(default=None, description="Author name filter")
    from_year: Optional[int] = Field(default=None, ge=1800)
    to_year: Optional[int] = Field(default=None)
    lang: ResponseLang = Field(default=ResponseLang.ENGLISH)
    count: int = Field(default=20, ge=1, le=200)
    start: int = Field(default=1, ge=1)


class KakenSearchInput(BaseModel):
    """Search KAKEN (Grants-in-Aid for Scientific Research) projects."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., description="Search query for research projects", min_length=1)
    researcher: Optional[str] = Field(default=None, description="Researcher name filter")
    institution: Optional[str] = Field(default=None, description="Institution filter")
    from_year: Optional[int] = Field(default=None, ge=1900)
    to_year: Optional[int] = Field(default=None)
    lang: ResponseLang = Field(default=ResponseLang.ENGLISH)
    count: int = Field(default=20, ge=1, le=200)
    start: int = Field(default=1, ge=1)


class RecordLookupInput(BaseModel):
    """Fetch a single CiNii record by its URL/CRID."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    record_url: str = Field(
        ...,
        description="Full CiNii URL (e.g., 'https://cir.nii.ac.jp/crid/1234567890') or CRID",
        min_length=1,
    )


class CrossSearchInput(BaseModel):
    """Cross-type search across all CiNii content types."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., description="Search query", min_length=1)
    from_year: Optional[int] = Field(default=None, ge=1800)
    to_year: Optional[int] = Field(default=None)
    lang: ResponseLang = Field(default=ResponseLang.ENGLISH)
    count: int = Field(default=20, ge=1, le=200)
    start: int = Field(default=1, ge=1)


class ResearcherSearchInput(BaseModel):
    """Search for researchers in CiNii."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., description="Researcher name or keyword", min_length=1)
    institution: Optional[str] = Field(default=None, description="Institution filter")
    lang: ResponseLang = Field(default=ResponseLang.ENGLISH)
    count: int = Field(default=20, ge=1, le=200)
    start: int = Field(default=1, ge=1)


# ==============================================================================
# Tools
# ==============================================================================


def _build_params(base: dict, extra: dict) -> dict:
    """Build query params, excluding None values."""
    params = {k: v for k, v in {**base, **extra}.items() if v is not None}
    return params


@mcp.tool(
    name="cinii_search_articles",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def cinii_search_articles(params: ArticleSearchInput) -> str:
    """Search CiNii Research for journal articles. Aggregates results from JALC, Crossref, PubMed, IRDB, and other sources. Supports filtering by title, author, journal, year range, and data source.

    Matching logic to weigh when reading the count: CiNii searches catalogued
    metadata and treats a multi-word `query` as a conjunction (AND), so a
    compound it has not indexed returns zero even when related work exists. A
    zero is a signal to vary the search and not proof the literature is absent;
    try a single key term, an alternative Japanese rendering (literal, emic, or
    combined), or a broader query, and report which Japanese terms were
    searched. The same string may behave very differently on J-STAGE, which
    searches full text; the divergence is a property of the platforms.
    """
    qp = _build_params(
        {"q": params.query, "count": params.count, "start": params.start,
         "sortorder": params.sort.value, "lang": params.lang.value},
        {"title": params.title, "creator": params.author, "publicationName": params.journal,
         "from": params.from_year, "until": params.to_year, "dataSourceType": params.data_source},
    )
    data = await _cinii_request("articles", qp)
    return _format_results(data, "CiNii Articles", query=qp)


@mcp.tool(
    name="cinii_search_books",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def cinii_search_books(params: BookSearchInput) -> str:
    """Search CiNii Research for books and monographs. Includes records from NACSIS-CAT, NDL Search, and other union catalogs."""
    qp = _build_params(
        {"q": params.query, "count": params.count, "start": params.start, "lang": params.lang.value},
        {"title": params.title, "creator": params.author, "publisher": params.publisher,
         "isbn": params.isbn, "from": params.from_year, "until": params.to_year},
    )
    data = await _cinii_request("books", qp)
    return _format_results(data, "CiNii Books", query=qp)


@mcp.tool(
    name="cinii_search_dissertations",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def cinii_search_dissertations(params: DissertationSearchInput) -> str:
    """Search CiNii Research for doctoral dissertations from Japanese universities."""
    qp = _build_params(
        {"q": params.query, "count": params.count, "start": params.start, "lang": params.lang.value},
        {"creator": params.author, "from": params.from_year, "until": params.to_year},
    )
    data = await _cinii_request("dissertations", qp)
    return _format_results(data, "CiNii Dissertations", query=qp)


@mcp.tool(
    name="cinii_search_kaken",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def cinii_search_kaken(params: KakenSearchInput) -> str:
    """Search KAKEN (科研費) research projects funded by JSPS Grants-in-Aid for Scientific Research. Useful for finding Japanese-funded research projects, their investigators, and affiliated institutions."""
    qp = _build_params(
        {"q": params.query, "count": params.count, "start": params.start, "lang": params.lang.value},
        {"creator": params.researcher, "affiliation": params.institution,
         "from": params.from_year, "until": params.to_year},
    )
    data = await _cinii_request("projects", qp)
    return _format_results(data, "KAKEN Projects", query=qp)


@mcp.tool(
    name="cinii_search_all",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def cinii_search_all(params: CrossSearchInput) -> str:
    """Cross-type search across all CiNii content: articles, books, dissertations, and KAKEN projects. Useful when you are unsure what type of resource you're looking for."""
    qp = _build_params(
        {"q": params.query, "count": params.count, "start": params.start, "lang": params.lang.value},
        {"from": params.from_year, "until": params.to_year},
    )
    data = await _cinii_request("all", qp)
    return _format_results(data, "CiNii Cross-Search", query=qp)


@mcp.tool(
    name="cinii_search_researchers",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def cinii_search_researchers(params: ResearcherSearchInput) -> str:
    """Search for researchers registered in CiNii. Returns names, affiliations, and links to their publication profiles."""
    qp = _build_params(
        {"q": params.query, "count": params.count, "start": params.start, "lang": params.lang.value},
        {"affiliation": params.institution},
    )
    data = await _cinii_request("researchers", qp)

    if "error" in data:
        return f"Error: {data['error']}"

    items = data.get("items", [])
    total = data.get("opensearch:totalResults", "?")

    if not items:
        return "No researchers found."

    lines = [f"**CiNii Researchers** — {total} total, showing {len(items)}\n"]
    for i, item in enumerate(items, 1):
        name = item.get("dc:title") or item.get("title") or "Unknown"
        if isinstance(name, list):
            name = name[0]
        affil = item.get("jpcoar:affiliationName") or item.get("dc:description") or ""
        if isinstance(affil, list):
            affil = affil[0]
        link = item.get("@id", "")
        lines.append(f"{i}. **{name}** — {affil}\n   {link}")

    return "\n".join(lines)


@mcp.tool(
    name="cinii_get_record",
    annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
)
async def cinii_get_record(params: RecordLookupInput) -> str:
    """Fetch a single CiNii record by its URL or CRID. Returns full metadata for any content type (article, book, dissertation, project)."""
    url = params.record_url
    if not url.startswith("http"):
        url = f"{BASE_URL}/crid/{url}"

    data = await _fetch_record(url)
    if "error" in data:
        return f"Error: {data['error']}"

    return _format_item(data)


# ==============================================================================
# Entry point
# ==============================================================================

if __name__ == "__main__":
    mcp.run()
