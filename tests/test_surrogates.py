"""A lone surrogate in any string must not reach the wire.

Vendored byte-identical across the server family, like mediation.py itself.

A JSON response can carry an escape such as "\\ud800", which json.loads turns
into a lone UTF-16 surrogate: legal in a Python str, not encodable as UTF-8.
Left in an envelope it reached the MCP SDK's UTF-8 serializer, which raised;
on cinii-mcp the server process exited and answered nothing afterwards
(stress test, 12 October 2026). The same character in a tool argument made
make_receipt() raise. mediation now replaces lone surrogates with U+FFFD before
anything is hashed, deposited or returned.
"""
import importlib
import json
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
_PKG = next(p.name for p in _SRC.iterdir() if (p / "mediation.py").is_file())
M = importlib.import_module(f"{_PKG}.mediation")

LONE = "bad\ud800x"
FIXED = "bad\ufffdx"


def _wire(text: str) -> bytes:
    return text.encode("utf-8")  # what the SDK does; raises on a lone surrogate


def _env(**over):
    kw = dict(server="test", operation="search", input_terms="x", normalized="x",
              params={}, matching_mode="literal", total=1, start=1,
              items=[{"title": LONE, "ids": {"doi": "10.1/" + LONE}}],
              diagnostics=[M.diag("info", "OK", "msg " + LONE, None)],
              attribution="attr")
    kw.update(over)
    return M.build_envelope(**kw)


def test_upstream_surrogate_in_items_is_replaced():
    env = _env()
    out = M.dumps(env)
    _wire(out)
    assert json.loads(out)["items"][0]["title"] == FIXED
    assert json.loads(out)["receipt"]["result_ids"] == ["10.1/" + FIXED]


def test_surrogate_in_tool_input_does_not_raise():
    env = _env(input_terms=LONE, normalized=LONE, params={"q": LONE})
    _wire(M.dumps(env))
    assert env["query"]["normalized"] == FIXED
    assert env["receipt"]["query_hash"] == _env(
        input_terms=FIXED, normalized=FIXED, params={"q": FIXED})["receipt"]["query_hash"]


def test_text_added_after_build_is_scrubbed_by_emit(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_RECEIPT_DIR", str(tmp_path))
    monkeypatch.delenv("MCP_RECEIPT_STRICT", raising=False)
    env = _env()
    env["diagnostics"].append(M.diag("error", "API_ERROR", "upstream said " + LONE, None))
    env["receipt"]["result_ids"].append("crid/" + LONE)  # a field the ledger records
    out = M.emit(env)
    _wire(out)
    codes = [d["code"] for d in json.loads(out)["diagnostics"]]
    assert "RECEIPT_WRITE_FAILED" not in codes, "the deposit must not fail on the surrogate"
    _wire((tmp_path / "test.jsonl").read_text(encoding="utf-8"))


def test_dumps_scrubs_any_mapping():
    _wire(M.dumps({LONE: [LONE, {"k": LONE}], "n": 1}))


def test_valid_text_is_untouched():
    text = "暴走族 𠮷野家 😀 한국 é"
    assert M.scrub(text) is text
    env = _env(items=[{"title": text, "ids": {}}])
    assert json.loads(M.dumps(env))["items"][0]["title"] == text
