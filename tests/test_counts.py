"""A total that is not a finite, non-negative integer is not a total (stress
test, 12 October 2026).

opensearch:totalResults of 1e309 parses as infinity and int() raised
OverflowError out of every search tool; a negative total was reported as the
corpus size.
"""
import asyncio
import json

import httpx
import pytest

BAD = ["1e309", "-1e309", "-7", "-7.0", "12.5", "true", "\"abc\"", "{}", "[]"]


@pytest.fixture
def body(monkeypatch):
    box = {"text": ""}

    def handler(req):
        return httpx.Response(200, text=box["text"], headers={"content-type": "application/json"})
    orig = httpx.AsyncClient.__init__

    def init(self, *a, **k):
        k["transport"] = httpx.MockTransport(handler)
        orig(self, *a, **k)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    return box


def _total(out):
    env = json.loads(out)
    t = env["result"]["total"]
    assert isinstance(t, int) and not isinstance(t, bool) and t >= 0, t
    return env

from cinii_mcp import server as S

ITEM = {"title": "x", "link": {"@id": "https://cir.nii.ac.jp/crid/1390001205128064000"}}


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(S, "CINII_APPID", "")


@pytest.mark.parametrize("bad", BAD)
def test_bad_total_falls_back_to_returned_count(body, bad):
    body["text"] = '{"opensearch:totalResults": %s, "items": [%s, %s]}' % (bad, json.dumps(ITEM), json.dumps(ITEM))
    env = _total(asyncio.run(S.cinii_search_articles(S.ArticleSearchInput(query="x"))))
    assert env["result"]["total"] == 2


@pytest.mark.parametrize("good", ["94", '"94"', "94.0"])
def test_a_good_total_passes_through(body, good):
    body["text"] = '{"opensearch:totalResults": %s, "items": [%s]}' % (good, json.dumps(ITEM))
    assert _total(asyncio.run(S.cinii_search_articles(S.ArticleSearchInput(query="x"))))["result"]["total"] == 94
