"""A CRID cannot rewrite the request path; the appid does not survive into
diagnostics (stress test, 12 October 2026).

- cinii_get_record put the CRID into the path unencoded: "../opensearch/v2/
  articles?q=x#" fetched a search page as a record, and a CiNii URL carrying
  ?lang=en folded the query string into the CRID.
- A query long enough to break URL construction raised httpx.InvalidURL,
  reported as "Network error reaching CiNii".
- Exception text and the upstream error body were passed through unredacted,
  so an echo of the request URL put the appid into the diagnostic.
"""
import asyncio
import json
from urllib.parse import quote

import httpx
import pytest
from pydantic import ValidationError

from cinii_mcp import server as S

APPID = "SENTINEL/APP+ID="


@pytest.fixture
def mock(monkeypatch):
    urls, box = [], {"status": 200, "body": "{}", "raise": None}

    def handler(req):
        urls.append(req.url)
        if box["raise"]:
            raise box["raise"](str(req.url), request=req)
        return httpx.Response(box["status"], text=box["body"])
    orig = httpx.AsyncClient.__init__

    def init(self, *a, **k):
        k["transport"] = httpx.MockTransport(handler)
        orig(self, *a, **k)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    monkeypatch.setattr(S, "CINII_APPID", APPID)
    return urls, box


def _diag(out):
    return json.loads(out)["diagnostics"][0]


@pytest.mark.parametrize("raw", ["../opensearch/v2/articles?q=x#", "1390/../../opensearch", "a?b", "a#b"])
def test_crid_stays_in_its_segment(mock, raw):
    urls, _ = mock
    asyncio.run(S.cinii_get_record(S.RecordLookupInput(record_url=raw)))
    path = urls[0].raw_path.decode().split("?")[0]
    assert path == "/crid/" + quote(raw, safe="") + ".json", path


def test_url_query_and_fragment_are_not_part_of_the_crid(mock):
    urls, _ = mock
    asyncio.run(S.cinii_get_record(S.RecordLookupInput(
        record_url="https://cir.nii.ac.jp/crid/1390001205128064000?lang=en#x")))
    assert urls[0].raw_path.decode().split("?")[0] == "/crid/1390001205128064000.json"


def test_control_character_in_crid_is_refused():
    with pytest.raises(ValidationError):
        S.RecordLookupInput(record_url="1390\n0001")


@pytest.mark.parametrize("model", [S.ArticleSearchInput, S.BookSearchInput, S.DissertationSearchInput,
                                   S.KakenSearchInput, S.CrossSearchInput, S.ResearcherSearchInput])
def test_overlong_query_is_refused_before_sending(model):
    with pytest.raises(ValidationError):
        model(query="x" * 70_000)


def test_unsendable_request_is_not_called_a_network_error(mock):
    data, err = asyncio.run(S._run("arti\x00cles", {"q": "q"}))
    assert data is None and err["code"] == "TRANSPORT_ERROR"
    assert "Network error" not in err["message"]


@pytest.mark.parametrize("form", [APPID, quote(APPID, safe=""), quote(APPID)])
def test_appid_echoed_in_error_body_is_redacted(mock, form):
    _, box = mock
    box.update(status=400, body="x" * 290 + form)
    _, err = asyncio.run(S._run("articles", {"q": "q"}))
    assert "SENTINEL" not in err["message"]


def test_appid_in_exception_text_is_redacted_on_search(mock):
    _, box = mock
    box["raise"] = httpx.ConnectError
    _, err = asyncio.run(S._run("articles", {"q": "q"}))
    assert err["code"] == "TRANSPORT_ERROR" and "SENTINEL" not in err["message"]


def test_appid_in_exception_text_is_redacted_on_get_record(mock):
    _, box = mock
    box["raise"] = httpx.ConnectError
    out = asyncio.run(S.cinii_get_record(S.RecordLookupInput(record_url="1390001205128064000")))
    assert _diag(out)["code"] == "TRANSPORT_ERROR" and "SENTINEL" not in out
