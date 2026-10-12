"""A 200 body that is not a CiNii search page or record is an error, not zero.

Stress test, 12 October 2026: a search answered 200 with null or {} came
back as ZERO_CONJUNCTION ("vary your rendering"), and cinii_get_record
answered with [1], "x", null or {} came back as "Single record resolved."
(an empty record, total 1, for {}).
"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from cinii_mcp import server as S

RECORD = (Path(__file__).parent / "fixtures" / "crid_1050002213762702720.json").read_text(encoding="utf-8")
BAD = ["null", "[]", '"x"', "{}", '{"message": "Service Unavailable"}', '{"items": []}',
       '{"opensearch:totalResults": 1, "items": "x"}']


@pytest.fixture
def body(monkeypatch):
    box = {}
    orig = httpx.AsyncClient.__init__

    def init(self, *a, **k):
        k["transport"] = httpx.MockTransport(lambda req: httpx.Response(
            200, content=box["body"].encode(), headers={"content-type": "application/json"}))
        orig(self, *a, **k)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    monkeypatch.setattr(S, "CINII_APPID", "TEST")
    return box


def _codes(out):
    env = json.loads(out)
    return env, [d["code"] for d in env["diagnostics"]]


@pytest.mark.parametrize("bad", BAD)
def test_search_wrong_shape_is_api_error(body, bad):
    body["body"] = bad
    env, codes = _codes(asyncio.run(S.cinii_search_articles(S.ArticleSearchInput(query="暴走族"))))
    assert "API_ERROR" in codes and "ZERO_CONJUNCTION" not in codes
    assert env["items"] == [] and env["result"]["total"] == 0


@pytest.mark.parametrize("bad", BAD)
def test_record_wrong_shape_is_api_error(body, bad):
    body["body"] = bad
    env, codes = _codes(asyncio.run(S.cinii_get_record(S.RecordLookupInput(record_url="1050002213762702720"))))
    assert codes[0] == "API_ERROR" and "OK" not in codes
    assert env["items"] == [] and env["result"]["total"] == 0


def test_real_record_still_resolves(body):
    body["body"] = RECORD
    env, codes = _codes(asyncio.run(S.cinii_get_record(S.RecordLookupInput(record_url="1050002213762702720"))))
    assert codes[0] == "OK" and env["result"]["total"] == 1


@pytest.mark.parametrize("page", [
    '{"opensearch:totalResults": "0", "items": []}',
    '{"opensearch:totalResults": "0"}',
])
def test_empty_search_page_is_still_zero(body, page):
    body["body"] = page
    _, codes = _codes(asyncio.run(S.cinii_search_articles(S.ArticleSearchInput(query="暴走族"))))
    assert "ZERO_CONJUNCTION" in codes and "API_ERROR" not in codes
