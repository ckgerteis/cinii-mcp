"""One malformed record no longer takes the page with it (stress test,
12 October 2026).

A record with a field of the wrong type (a string where an object belongs)
raised out of the item builder, so the caller got an exception instead of an
envelope and the other records on the page were lost. Such a record is now
skipped and named in a RECORD_UNREADABLE warning; the rest are returned, and an
all-unreadable page is not reported as an absence.
"""
import asyncio
import json

import httpx
import pytest


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


def _codes(env):
    return [d["code"] for d in env["diagnostics"]]


def _unreadable(env):
    return next(d for d in env["diagnostics"] if d["code"] == "RECORD_UNREADABLE")

from cinii_mcp import server as S

GOOD = {"title": "ok", "@id": "https://cir.nii.ac.jp/crid/1", "link": {"@id": "https://cir.nii.ac.jp/crid/1"}}
BADS = [dict(GOOD, **{"@id": "https://cir.nii.ac.jp/crid/2", "dc:creator": {"a": 1}}),
        dict(GOOD, **{"@id": 2}),
        {"title": "no @id", "link": 5},
        "a string where a record belongs"]


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(S, "CINII_APPID", "")


def _page(*recs):
    return json.dumps({"opensearch:totalResults": str(len(recs)), "items": list(recs)})


@pytest.mark.parametrize("bad", BADS)
def test_one_bad_record_is_skipped_and_named(body, bad):
    body["text"] = _page(GOOD, bad, GOOD)
    env = json.loads(asyncio.run(S.cinii_search_articles(S.ArticleSearchInput(query="x"))))
    assert env["result"]["returned"] == 2
    d = _unreadable(env)
    assert "1 of 3" in d["message"] and "#2" in d["message"]
    assert "OK" not in _codes(env)


def test_the_bad_record_is_named_by_id(body):
    body["text"] = _page(GOOD, BADS[0])
    assert "crid/2" in _unreadable(json.loads(asyncio.run(S.cinii_search_articles(S.ArticleSearchInput(query="x")))))["message"]


def test_an_all_unreadable_page_is_not_an_absence(body):
    body["text"] = _page(BADS[0], BADS[1])
    env = json.loads(asyncio.run(S.cinii_search_articles(S.ArticleSearchInput(query="x"))))
    assert "ZERO_CONJUNCTION" not in _codes(env) and "RECORD_UNREADABLE" in _codes(env)
