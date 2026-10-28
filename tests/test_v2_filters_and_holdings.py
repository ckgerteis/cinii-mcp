"""v2 filters, the data and projectsAndProducts searches, and the NACSIS-CAT
holdings and library endpoints (28 October 2026).

What is pinned here, and why:

- Each new input reaches the wire under CiNii's own parameter name. The
  articles `journal` field was sent as publicationName, which CiNii does not
  read: every journal-filtered search before this release came back
  unfiltered (青年 + 教育学研究: 69,977 records either way; 518 with
  publicationTitle, live, 28 Oct 2026).
- A sort CiNii would ignore for the search type, an unknown dataSourceType
  and a malformed language code are refused here, because upstream each one
  produces a plausible page (unsorted, or empty) rather than an error.
- An unchanged call sends exactly the parameters it sent before, so its
  receipt hash does not move between releases.
- The holdings and library channels arrive wrapped in @graph; a zero
  holdings list is HOLDINGS_NONE_REGISTERED, never ZERO_CONJUNCTION.
- An HTML page served with 200 is API_ERROR, not "Network error".
"""
import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import ValidationError

from cinii_mcp import server as S

PAGE = {"opensearch:totalResults": 2, "items": [
    {"@id": "https://cir.nii.ac.jp/crid/1", "title": "青年団", "dc:type": "Project"},
    {"@id": "https://cir.nii.ac.jp/crid/2", "title": "青年団史", "dc:type": "Book"},
]}


def _graph(items, total=None):
    return {"@graph": [{"@type": "channel", "opensearch:totalResults": len(items) if total is None else total,
                        "items": items}]}


def _lib(fano, name, cat=None):
    d = {"@id": f"https://ci.nii.ac.jp/library/{fano}", "title": name,
         "dc:identifier": [{"@type": "cir:FANO", "@value": fano}]}
    if cat:
        d["cir:catalogUrl"] = {"@id": cat}
    return d


@pytest.fixture
def wire(monkeypatch):
    box = {"body": json.dumps(PAGE), "status": 200, "ctype": "application/json", "urls": []}
    orig = httpx.AsyncClient.__init__

    def handler(req):
        box["urls"].append(req.url)
        return httpx.Response(box["status"], content=box["body"].encode(),
                              headers={"content-type": box["ctype"]})

    def init(self, *a, **k):
        k["transport"] = httpx.MockTransport(handler)
        orig(self, *a, **k)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    monkeypatch.setattr(S, "CINII_APPID", "TEST")
    return box


def _sent(box):
    u = box["urls"][-1]
    return u.path, {k: v[0] for k, v in parse_qs(urlsplit(str(u)).query).items()}


def _run(coro):
    env = json.loads(asyncio.run(coro))
    return env, [d["code"] for d in env["diagnostics"]]


# ---- parameter names on the wire -------------------------------------------

def test_article_filters_reach_the_wire(wire):
    _run(S.cinii_search_articles(S.ArticleSearchInput(
        query="青年", journal="教育学研究", language="JA,en", has_fulltext=True,
        resource_type="journal article", data_source="jalc,irdb", researcher_id="DA1938362X")))
    path, q = _sent(wire)
    assert path == "/opensearch/v2/articles"
    assert q["publicationTitle"] == "教育学研究" and "publicationName" not in q
    assert q["languageType"] == "ja,en" and q["hasLinkToFullText"] == "true"
    assert q["resourceType"] == "journal article" and q["dataSourceType"] == "JALC,IRDB"
    assert q["researcherId"] == "DA1938362X"


def test_book_filters_and_query_free_ncid_lookup(wire):
    env, _ = _run(S.cinii_search_books(S.BookSearchInput(
        ncid="BC08431161", category="379", exact_title=False, title="Mobilizing")))
    path, q = _sent(wire)
    assert path == "/opensearch/v2/books" and "q" not in q
    assert q["ncid"] == "BC08431161" and q["category"] == "379" and q["isFullTitle"] == "false"
    assert env["query"]["input_terms"].startswith("title=Mobilizing")
    assert "SCRIPT_LATIN_QUERY" not in [d["code"] for d in env["diagnostics"]]


def test_dissertation_kaken_researcher_filters(wire):
    _run(S.cinii_search_dissertations(S.DissertationSearchInput(
        query="青年", award_institution="東京大学", degree="博士（教育学）")))
    _, q = _sent(wire)
    assert q["awardInstitution"] == "東京大学" and q["degree"] == "博士（教育学）"
    _run(S.cinii_search_kaken(S.KakenSearchInput(project_id="18K00968", researcher_id="00779902")))
    path, q = _sent(wire)
    assert path == "/opensearch/v2/projects" and q["projectId"] == "18K00968" and q["researcherId"] == "00779902"
    _run(S.cinii_search_researchers(S.ResearcherSearchInput(query="青年", sort="20", researcher_id="00779902")))
    _, q = _sent(wire)
    assert q["sortorder"] == "20" and q["researcherId"] == "00779902"


def test_unchanged_calls_send_what_they_sent_before(wire):
    """No new key on a call that uses none of the new inputs: the receipt hash
    is taken over these params."""
    _run(S.cinii_search_books(S.BookSearchInput(query="青年")))
    _, q = _sent(wire)
    assert set(q) == {"q", "count", "start", "lang", "appid", "format"}
    _run(S.cinii_search_articles(S.ArticleSearchInput(query="青年")))
    _, q = _sent(wire)
    assert set(q) == {"q", "count", "start", "sortorder", "lang", "appid", "format"}


# ---- refusals ---------------------------------------------------------------

@pytest.mark.parametrize("model,kw", [
    (S.ArticleSearchInput, {"query": "x", "sort": "5"}),        # researcher sort on articles
    (S.BookSearchInput, {"query": "x", "sort": "10"}),          # citations: articles only
    (S.ArticleSearchInput, {"query": "x", "sort": "40"}),       # CRID: cross search only
    (S.ResearcherSearchInput, {"query": "x", "sort": "1"}),     # OLDEST: not researchers
    (S.ArticleSearchInput, {"query": "x", "data_source": "CID"}),  # removed 9 Dec 2024
    (S.ArticleSearchInput, {"query": "x", "language": "jpn"}),
    (S.ArticleSearchInput, {"query": "x", "language": "ja,"}),
    (S.ArticleSearchInput, {}),                                  # no criterion at all
    (S.BookSearchInput, {"sort": "0", "count": 5}),              # paging is not a criterion
])
def test_refused(model, kw):
    with pytest.raises(ValidationError):
        model(**kw)


@pytest.mark.parametrize("model,code", [
    (S.ResearcherSearchInput, "5"), (S.ResearcherSearchInput, "31"),
    (S.CrossSearchInput, "40"), (S.ArticleSearchInput, "10"), (S.DataSearchInput, "0"),
])
def test_sorts_in_scope_accepted(model, code):
    assert model(query="x", sort=code).sort.value == code


def test_researchers_take_no_language_filter():
    with pytest.raises(ValidationError):
        S.ResearcherSearchInput(query="x", language="ja")


# ---- mixed-type searches ----------------------------------------------------

@pytest.mark.parametrize("call,path", [
    (lambda: S.cinii_search_data(S.DataSearchInput(query="青年", dataset_format="csv")), "/opensearch/v2/data"),
    (lambda: S.cinii_search_projects_and_products(S.ProjectsAndProductsSearchInput(researcher_id="00779902")),
     "/opensearch/v2/projectsAndProducts"),
    (lambda: S.cinii_search_all(S.CrossSearchInput(query="青年団")), "/opensearch/v2/all"),
])
def test_mixed_searches_type_each_record(wire, call, path):
    env, codes = _run(call())
    assert _sent(wire)[0] == path and codes[0] == "OK"
    assert [i["record_type"] for i in env["items"]] == ["project", "book"]


def test_dataset_type_maps(wire):
    wire["body"] = json.dumps({"opensearch:totalResults": 1, "items": [
        {"@id": "https://cir.nii.ac.jp/crid/3", "title": "青年会資料", "dc:type": "Dataset"}]})
    env, _ = _run(S.cinii_search_data(S.DataSearchInput(query="青年")))
    assert env["items"][0]["record_type"] == "dataset"


# ---- non-JSON 200 ---------------------------------------------------------

def test_html_page_with_200_is_api_error(wire):
    wire["body"] = "<!doctype html><html><head><title>CiNii Research - ただいまご利用いただけません</title>"
    wire["ctype"] = "text/html"
    env, codes = _run(S.cinii_search_kaken(S.KakenSearchInput(researcher_id="1420845751146689664")))
    assert codes[0] == "API_ERROR" and "TRANSPORT_ERROR" not in codes
    assert "ただいまご利用いただけません" in env["diagnostics"][0]["message"]


# ---- holdings and libraries -------------------------------------------------

def test_holdings_unwraps_graph_and_sends_filters(wire):
    wire["body"] = json.dumps(_graph([
        _lib("FA018520", "ハイデルベルク大学 CATS図書館 東アジア部門"),
        _lib("FA02831X", "神奈川大学 みなとみらい図書館", "http://opac.example/?ncid=BC08431161"),
    ]))
    env, codes = _run(S.cinii_get_holdings(S.HoldingsInput(
        ncid="bc08431161", japan_us_ill="A", copy_service="C", continuing=True)))
    path, q = _sent(wire)
    assert path == "/opensearch/holder"
    assert q["ncid"] == "BC08431161" and q["ill_oclc"] == "A" and q["ill_copys"] == "C" and q["cont"] == "1"
    assert codes[0] == "OK" and env["operation"] == "get_holdings" and "searched_for" not in env
    assert env["matching_mode"] == "identifier_exact" and env["coverage_note"]
    it = env["items"][1]
    assert it["record_type"] == "holding" and it["source"]["holding_org"].startswith("神奈川大学")
    assert it["extra"] == {"library_id": "FA02831X", "catalog_url": "http://opac.example/?ncid=BC08431161"}


def test_holdings_window_is_cut_here_total_is_cinii(wire):
    wire["body"] = json.dumps(_graph([_lib(f"FA0000{i:02d}", f"図書館{i}") for i in range(30)]))
    env, _ = _run(S.cinii_get_holdings(S.HoldingsInput(ncid="BA49258744", start=26, count=10)))
    assert env["result"]["total"] == 30 and env["result"]["returned"] == 5 and env["result"]["start"] == 26
    assert env["items"][0]["extra"]["library_id"] == "FA000025"


def test_zero_holdings_is_not_an_absence(wire):
    wire["body"] = json.dumps(_graph([]))
    env, codes = _run(S.cinii_get_holdings(S.HoldingsInput(ncid="BC08431161", library_id="fa013006")))
    assert _sent(wire)[1]["fano"] == "FA013006"
    assert codes[0] == "HOLDINGS_NONE_REGISTERED" and "ZERO_CONJUNCTION" not in codes
    assert "under these filters" in env["diagnostics"][0]["message"]


@pytest.mark.parametrize("bad", ["null", "{}", '{"@graph": []}', '{"@graph": [{"items": []}]}'])
def test_holdings_wrong_shape_is_api_error(wire, bad):
    wire["body"] = bad
    env, codes = _run(S.cinii_get_holdings(S.HoldingsInput(ncid="BC08431161")))
    assert codes[0] == "API_ERROR" and "HOLDINGS_NONE_REGISTERED" not in codes


@pytest.mark.parametrize("kw", [{"ncid": "BC0843116"}, {"ncid": "9781501756313"},
                                {"ncid": "BC08431161", "library_id": "013006"}])
def test_holdings_identifiers_checked(kw):
    with pytest.raises(ValidationError):
        S.HoldingsInput(**kw)


def test_library_search(wire):
    wire["body"] = json.dumps(_graph([_lib("FA013006", "ロンドン大学 SOAS図書館")]))
    env, codes = _run(S.cinii_search_libraries(S.LibrarySearchInput(name="SOAS", sort="4", page=2, count=5)))
    path, q = _sent(wire)
    assert path == "/opensearch/library" and q["name"] == "SOAS" and q["sortorder"] == "4" and q["p"] == "2"
    assert codes[0] == "OK" and env["result"]["start"] == 6
    assert env["items"][0]["extra"]["library_id"] == "FA013006" and env["items"][0]["record_type"] == "library"
    wire["body"] = json.dumps(_graph([]))
    _, codes = _run(S.cinii_search_libraries(S.LibrarySearchInput(name="nowhere")))
    assert codes[0] == "LIBRARY_NONE_MATCHED"
