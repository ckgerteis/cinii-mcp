"""A receipts file whose last line is unreadable must stop the chain, not fork it.

Vendored byte-identical across the server family, like ledger.py itself.

An interrupted write leaves a partial last line with no newline. The ledger
used to read that tail, fail to parse it, fall back to the genesis hash, and
append: the new line was glued onto the partial one, and the chain silently
restarted mid-file. verify_chain then raised JSONDecodeError instead of saying
what was wrong. These tests pin the repaired behaviour: the deposit is refused
and the file left as found, and verify reports the line as unreadable.
"""
import importlib
import json
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
_PKG = next(p.name for p in _SRC.iterdir() if (p / "ledger.py").is_file())
ledger = importlib.import_module(f"{_PKG}.ledger")

_ENV = {
    "server": "test", "operation": "search",
    "query": {"input_terms": "x", "normalized": "x", "script": "latin", "params": {}},
    "result": {"total": 0, "returned": 0, "start": 1, "breadth": "none"},
    "diagnostics": [], "receipt": {"query_hash": "sha256:0", "result_ids": []},
}


@pytest.fixture
def receipts(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_RECEIPT_LOG", raising=False)
    monkeypatch.delenv("MCP_RECEIPT_STRICT", raising=False)
    monkeypatch.setenv("MCP_RECEIPT_DIR", str(tmp_path))
    return tmp_path / "test.jsonl"


def _deposit(n):
    for _ in range(n):
        assert ledger.record_envelope(_ENV) is not None


def test_healthy_file_keeps_one_chain(receipts):
    _deposit(3)
    v = ledger.verify_chain(str(receipts))
    assert v["ok"] and v["lines"] == 3


@pytest.mark.parametrize("tail", [
    b'{"hash": "abc", "prev_ha',          # interrupted mid-line, no newline
    b'{"hash": "abc", "prev_ha\n',        # interrupted, newline survived
    b'{"no_hash_here": 1}\n',             # parses, but is not a receipt line
    b'\xff\xfe not utf-8\n',              # undecodable bytes
])
def test_unreadable_tail_refuses_the_deposit(receipts, tail):
    _deposit(2)
    with open(receipts, "ab") as fh:
        fh.write(tail)
    before = receipts.read_bytes()
    assert ledger.record_envelope(_ENV) is None
    assert receipts.read_bytes() == before, "the file must be left exactly as found"


def test_complete_last_line_without_newline_is_refused(receipts):
    """A write cut exactly before its newline parses, but appending to it would
    still glue the next line on."""
    _deposit(2)
    receipts.write_bytes(receipts.read_bytes().rstrip(b"\n"))
    before = receipts.read_bytes()
    assert ledger.record_envelope(_ENV) is None
    assert receipts.read_bytes() == before


def test_unreadable_tail_raises_when_strict(receipts, monkeypatch):
    _deposit(1)
    with open(receipts, "ab") as fh:
        fh.write(b'{"hash": "abc", "prev_ha')
    monkeypatch.setenv("MCP_RECEIPT_STRICT", "1")
    with pytest.raises(ledger.LedgerUnreadable):
        ledger.record_envelope(_ENV)


def test_verify_reports_unreadable_line(receipts):
    _deposit(2)
    with open(receipts, "ab") as fh:
        fh.write(b'{"hash": "abc", "prev_ha')
    v = ledger.verify_chain(str(receipts))
    assert v["ok"] is False
    assert v["kind"] == "unreadable"
    assert v["break_at"] == 3


def test_verify_dir_reports_rather_than_raises(receipts):
    _deposit(2)
    with open(receipts, "ab") as fh:
        fh.write(b"not json\n")
    d = ledger.verify_dir(str(receipts.parent))
    assert d["ok"] is False
    (f,) = d["files"]
    assert f["kind"] == "unreadable" and f["lines"] == 2


def test_old_behaviour_would_have_forked(receipts):
    """The torn line, once removed, leaves a chain that still verifies: the
    refusal kept the file recoverable, where the old fallback to genesis
    appended a second chain after the damage."""
    _deposit(2)
    good = receipts.read_bytes()
    with open(receipts, "ab") as fh:
        fh.write(b'{"hash": "abc", "prev_ha')
    assert ledger.record_envelope(_ENV) is None
    receipts.write_bytes(good)
    _deposit(1)
    v = ledger.verify_chain(str(receipts))
    assert v["ok"] and v["lines"] == 3
    assert json.loads(receipts.read_text().splitlines()[-1])["prev_hash"] != "0" * 64
