"""
ledger.py — query receipts, persisted.

The receipt already exists. mediation.make_receipt() issues one with every
envelope: a stable query hash and the identifiers of the records returned. What
has been missing is persistence — the receipt lived in the response and then in
a conversation transcript, which is not a deposit. This module writes it to an
append-only file at the moment the query is answered.

Two entry points, because the four servers are not alike:

    record_envelope(env)   for cinii-mcp and jstage-mcp, which build the unified
                           envelope; the envelope carries script, matching_mode,
                           breadth and diagnostics, so the mediation itself is
                           recorded, not merely the string sent.

    record_request(...)    for openalex-mcp and semantic-scholar-mcp, which have
                           no envelope; called from inside the HTTP helper, so
                           what is recorded is what was actually sent to the API.

Configuration is entirely by environment:

    MCP_RECEIPT_LOG      path to the JSONL file. UNSET MEANS OFF — nothing is
                         written and nothing fails.
    MCP_RECEIPT_SESSION  free-text label written into every line (a project or
                         article slug).
    MCP_RECEIPT_STRICT   set to 1 to make a logging failure raise instead of
                         passing silently. Default is to swallow: a search
                         matters more than the record of it.

Every line carries the SHA-256 of the line before it, so a deposited log is
tamper-evident. Verify with `python ledger.py verify <path>`.

Secrets are never written. Any parameter whose name matches _SECRET_KEYS is
replaced with the string "[redacted]" before the line is composed, because these
logs are written to be deposited.

Pure standard library. Vendored byte-identical into each server package.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

LEDGER_VERSION = "1.0.0"

_LOCK = threading.Lock()
_GENESIS = "0" * 64

_SECRET_KEYS = {
    "appid", "api_key", "apikey", "key", "token", "access_token",
    "x-api-key", "mailto", "email", "password", "secret",
}


# ---------------------------------------------------------------- helpers

def enabled() -> bool:
    return bool(os.environ.get("MCP_RECEIPT_LOG"))


def _path() -> Optional[str]:
    p = os.environ.get("MCP_RECEIPT_LOG")
    return p or None


def redact(params: Optional[Mapping[str, Any]]) -> dict:
    """Copy a parameter mapping with credential-bearing values removed."""
    if not params:
        return {}
    out = {}
    for k, v in params.items():
        out[k] = "[redacted]" if str(k).strip().lower() in _SECRET_KEYS else v
    return out


def _stable_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _last_hash(path: str) -> str:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return _GENESIS
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            buf = b""
            while end > 0:
                start = max(0, end - 4096)
                fh.seek(start)
                buf = fh.read(end - start) + buf
                if len([l for l in buf.split(b"\n") if l.strip()]) >= 2 or start == 0:
                    break
                end = start
        lines = [l for l in buf.splitlines() if l.strip()]
        if not lines:
            return _GENESIS
        return json.loads(lines[-1].decode("utf-8")).get("hash", _GENESIS)
    except Exception:
        return _GENESIS


def _write(line: dict) -> Optional[dict]:
    path = _path()
    if not path:
        return None
    try:
        with _LOCK:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            line["prev_hash"] = _last_hash(path)
            line["hash"] = _stable_hash(line)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False, sort_keys=True, default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        return line
    except Exception:
        if os.environ.get("MCP_RECEIPT_STRICT") == "1":
            raise
        return None


def _base(kind: str, server: str, operation: str) -> dict:
    return {
        "ledger_version": LEDGER_VERSION,
        "receipt_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session": os.environ.get("MCP_RECEIPT_SESSION", ""),
        "kind": kind,
        "server": server,
        "operation": operation,
    }


# ---------------------------------------------------------------- envelope

def record_envelope(env: Mapping[str, Any]) -> Optional[dict]:
    """Persist one unified envelope. Returns the written line, or None if off.

    Reads the envelope rather than re-deriving anything: the script, matching
    mode, breadth and diagnostic codes recorded here are the ones the server
    reported to the client, so log and answer cannot drift apart.
    """
    if not enabled():
        return None
    try:
        q = env.get("query") or {}
        r = env.get("result") or {}
        rec = env.get("receipt") or {}
        line = _base("envelope", str(env.get("server", "")), str(env.get("operation", "")))
        line.update({
            "input_terms": q.get("input_terms"),
            "term": q.get("normalized"),
            "script": q.get("script"),
            "matching_mode": env.get("matching_mode"),
            "params": redact(q.get("params")),
            "total": r.get("total"),
            "returned": r.get("returned"),
            "start": r.get("start"),
            "breadth": r.get("breadth"),
            "diagnostics": [d.get("code") for d in (env.get("diagnostics") or []) if isinstance(d, dict)],
            "coverage_note": bool(env.get("coverage_note")),
            "query_hash": rec.get("query_hash"),
            "issued_at": rec.get("issued_at"),
            "result_ids": rec.get("result_ids") or [],
        })
        return _write(line)
    except Exception:
        if os.environ.get("MCP_RECEIPT_STRICT") == "1":
            raise
        return None


# ---------------------------------------------------------------- request

def _count_from(response: Any) -> tuple[Optional[int], Optional[int]]:
    """Best-effort (total, returned) for servers without an envelope."""
    if isinstance(response, list):
        return (len(response), len(response))
    if not isinstance(response, Mapping):
        return (None, None)
    if "error" in response:
        return (None, None)
    total = None
    meta = response.get("meta")
    if isinstance(meta, Mapping) and isinstance(meta.get("count"), int):
        total = meta["count"]
    elif isinstance(response.get("total"), int):
        total = response["total"]
    returned = None
    for key in ("results", "data", "recommendedPapers", "items"):
        v = response.get(key)
        if isinstance(v, list):
            returned = len(v)
            break
    if returned is None and (response.get("paperId") or response.get("id")):
        returned = 1
    if total is None:
        total = returned
    return (total, returned)


def record_request(
    *,
    server: str,
    operation: str,
    endpoint: str,
    params: Optional[Mapping[str, Any]] = None,
    response: Any = None,
    error: Optional[str] = None,
    term: Optional[str] = None,
) -> Optional[dict]:
    """Persist one outbound API call for a server that has no envelope."""
    if not enabled():
        return None
    try:
        clean = redact(params)
        total, returned = _count_from(response)
        api_error = error
        if api_error is None and isinstance(response, Mapping) and "error" in response:
            api_error = str(response.get("error"))[:300]
        line = _base("request", server, operation)
        term_value = term
        if term_value is None:
            for key in ("search", "query", "q", "text", "term", "name", "material",
                        "article", "author", "kanji", "reading", "components", "filter"):
                v = clean.get(key)
                if isinstance(v, str) and v:
                    term_value = v
                    break
        line.update({
            "term": term_value,
            "script": detect_script(term_value) if term_value else None,
            "endpoint": endpoint,
            "params": clean,
            "total": total,
            "returned": returned,
            "error": api_error,
            "query_hash": "sha256:" + _stable_hash({"endpoint": endpoint, "params": clean}),
        })
        return _write(line)
    except Exception:
        if os.environ.get("MCP_RECEIPT_STRICT") == "1":
            raise
        return None


def detect_script(s: Optional[str]) -> str:
    """Same classification as mediation.detect_script, duplicated so that the
    two servers without a mediation module do not need one."""
    if not s:
        return "latin"
    has_latin = has_hira = has_kata = has_han = False
    for ch in str(s):
        o = ord(ch)
        if "a" <= ch.lower() <= "z":
            has_latin = True
        elif 0x3040 <= o <= 0x309F:
            has_hira = True
        elif (0x30A0 <= o <= 0x30FF) or (0xFF66 <= o <= 0xFF9D):
            has_kata = True
        elif (0x4E00 <= o <= 0x9FFF) or (0x3400 <= o <= 0x4DBF) or (0xF900 <= o <= 0xFAFF):
            has_han = True
    has_kana = has_hira or has_kata
    if has_latin and (has_han or has_kana):
        return "mixed"
    if has_latin:
        return "latin"
    if has_han and has_kana:
        return "han_kana"
    if has_han:
        return "han"
    if has_kana:
        return "kana"
    return "latin"


# ---------------------------------------------------------------- reading

def read_log(path: str) -> Iterable[dict]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def verify_chain(path: str) -> dict:
    """Recompute the chain. {'ok', 'lines', 'break_at', 'reason'}."""
    prev = _GENESIS
    n = 0
    for n, rec in enumerate(read_log(path), start=1):
        stored = rec.pop("hash", None)
        if rec.get("prev_hash") != prev:
            return {"ok": False, "lines": n, "break_at": n, "reason": "prev_hash mismatch"}
        if _stable_hash(rec) != stored:
            return {"ok": False, "lines": n, "break_at": n, "reason": "hash mismatch"}
        prev = stored
    return {"ok": True, "lines": n, "break_at": None, "reason": None}


def to_csv(path: str, out: str) -> int:
    import csv
    cols = ["ts", "session", "kind", "server", "operation", "term", "script",
            "matching_mode", "total", "returned", "breadth", "diagnostics",
            "error", "query_hash", "receipt_id"]
    n = 0
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for rec in read_log(path):
            row = dict(rec)
            if isinstance(row.get("diagnostics"), list):
                row["diagnostics"] = ";".join(str(x) for x in row["diagnostics"])
            w.writerow(row)
            n += 1
    return n


def summary(path: str) -> dict:
    """Counts by server, operation and script — for the deposit description."""
    by_server: dict = {}
    by_script: dict = {}
    n = 0
    first = last = None
    for rec in read_log(path):
        n += 1
        first = first or rec.get("ts")
        last = rec.get("ts")
        by_server[rec.get("server")] = by_server.get(rec.get("server"), 0) + 1
        by_script[rec.get("script")] = by_script.get(rec.get("script"), 0) + 1
    return {"lines": n, "first": first, "last": last,
            "by_server": by_server, "by_script": by_script}


def _cli() -> None:
    """Console-script entry point (`<dist>-ledger`).

    Exposed as a function rather than left in the __main__ block: the package
    __init__ imports server, which imports this module, so `python -m
    <pkg>.ledger` re-executes an already-imported module and runpy warns.
    """
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "verify" and len(sys.argv) > 2:
        print(json.dumps(verify_chain(sys.argv[2]), indent=2))
    elif cmd == "summary" and len(sys.argv) > 2:
        print(json.dumps(summary(sys.argv[2]), indent=2, ensure_ascii=False))
    elif cmd == "csv" and len(sys.argv) > 3:
        print(f"{to_csv(sys.argv[2], sys.argv[3])} rows")
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
