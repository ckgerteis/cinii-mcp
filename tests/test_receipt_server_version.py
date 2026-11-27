"""Every receipt names the server release that issued it (schema 2.4.0).

The version is stamped from the package's own __version__, and it stays out of
the query hash, so a hash issued under one release still matches the same query
under another.
"""
import importlib
import json
from pathlib import Path

M = importlib.import_module("cinii_mcp.mediation")
S = importlib.import_module("cinii_mcp.server")


def test_receipt_carries_server_version():
    r = M.make_receipt("暴走族", {"count": 20}, [])
    assert r["server_version"] == S.__version__


def test_server_version_is_outside_the_hash():
    before = M.SERVER_VERSION
    try:
        M.SERVER_VERSION = "0.0.1"
        a = M.make_receipt("暴走族", {"count": 20}, [])["query_hash"]
        M.SERVER_VERSION = "9.9.9"
        b = M.make_receipt("暴走族", {"count": 20}, [])["query_hash"]
    finally:
        M.SERVER_VERSION = before
    assert a == b


def test_schema_admits_server_version():
    schema = json.loads((Path(__file__).resolve().parents[1] / "response-schema.json").read_text(encoding="utf-8"))
    assert "server_version" in schema["$defs"]["receipt"]["properties"]
