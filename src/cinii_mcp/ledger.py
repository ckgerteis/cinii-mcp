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

    MCP_RECEIPT_DIR      path to a receipts FOLDER. Each server writes its own
                         file, <dir>/<server>.jsonl. Preferred.
    MCP_RECEIPT_LOG      path to a single JSONL file. Honoured for compatibility
                         and only when MCP_RECEIPT_DIR is unset. See the warning
                         below before pointing several servers at one file.
    MCP_RECEIPT_SESSION  free-text label written into every line (a project or
                         article slug). The slug is what groups a project's
                         queries, so it should be the same across every server
                         working on one piece of research.
    MCP_RECEIPT_STRICT   set to 1 to make a logging failure raise instead of
                         passing silently. Default is to swallow: a search
                         matters more than the record of it.

BOTH UNSET MEANS OFF — nothing is written and nothing fails.

Every line carries the SHA-256 of the line before it, so a deposited log is
tamper-evident. Verify one file with `<dist>-ledger verify <path>`, or a whole
folder with `<dist>-ledger verify-dir <dir>`.

One writer per file. Appending is read-the-last-hash-then-write, and the lock
around it is a threading lock, which holds within one process and not between
several. Six servers are six processes: point them at one file and two of them
answering at the same moment will both read the same predecessor and both claim
it. Measured, not theorised — six processes writing 150 lines to one file
produced 14 forks. Hence MCP_RECEIPT_DIR and a file per server. verify_chain()
reports a fork as a fork rather than as tampering, because the two mean entirely
different things about a deposit.

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

LEDGER_VERSION = "1.1.0"

_LOCK = threading.Lock()
_GENESIS = "0" * 64

_SECRET_KEYS = {
    "appid", "api_key", "apikey", "key", "token", "access_token",
    "x-api-key", "mailto", "email", "password", "secret",
}


# ---------------------------------------------------------------- helpers

def enabled() -> bool:
    return bool(os.environ.get("MCP_RECEIPT_DIR") or os.environ.get("MCP_RECEIPT_LOG"))


def _slug(server: str) -> str:
    """A filename for a server name. Conservative: anything outside
    [A-Za-z0-9._-] becomes a hyphen, so a name from a caller cannot escape the
    receipts folder or collide with a path separator."""
    out = "".join(c if (c.isalnum() or c in "._-") else "-" for c in (server or "unknown"))
    out = out.strip("-.") or "unknown"
    return out.lower()


def receipts_dir() -> Optional[str]:
    d = os.environ.get("MCP_RECEIPT_DIR")
    return d or None


def log_path_for(server: str) -> Optional[str]:
    """Where this server's receipts go, or None if depositing is off."""
    d = receipts_dir()
    if d:
        return os.path.join(d, _slug(server) + ".jsonl")
    p = os.environ.get("MCP_RECEIPT_LOG")
    return p or None


def _path(line: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    return log_path_for(str((line or {}).get("server") or "unknown"))


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
    path = _path(line)
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
    """Recompute the chain.

    {'ok', 'lines', 'break_at', 'kind', 'reason', 'terminal_hash'}

    `kind` separates findings the earlier version reported identically as
    "prev_hash mismatch", and which mean entirely different things:

        'fork'       two or more lines claim the same predecessor. That is the
                     signature of concurrent writers to one file — one process
                     reads the last hash, a second appends before the first
                     does, and both then claim it. The file was written by more
                     than one process at once. Nobody edited anything.
        'missing'    a line's predecessor is nowhere in the file. A line was
                     removed, or the file was truncated from the middle.
        'reordered'  the lines are all present and all self-consistent, and they
                     are not in chain order.
        'tamper'     a line does not hash to its own content. It was edited.

    Only the last is a claim about honesty. A fork is a configuration fault: the
    file is several chains rather than one, and reading it as such recovers
    every line. Reporting the two the same way would invite a reader to treat a
    misconfiguration as evidence of interference, or the reverse.
    """
    records = list(read_log(path))
    n = len(records)
    if n == 0:
        return {"ok": True, "lines": 0, "break_at": None, "kind": None,
                "reason": None, "terminal_hash": _GENESIS}

    # 1. Does every line hash to what it says it does? Independent of order,
    #    and the only test that speaks to editing.
    hashes = []
    for i, rec in enumerate(records, start=1):
        body = dict(rec)
        stored = body.pop("hash", None)
        if _stable_hash(body) != stored:
            return {"ok": False, "lines": n, "break_at": i, "kind": "tamper",
                    "reason": "line does not hash to its own content",
                    "terminal_hash": None}
        hashes.append(stored)

    # 2. A predecessor claimed twice is a fork.
    claims: dict = {}
    for i, rec in enumerate(records, start=1):
        claims.setdefault(rec.get("prev_hash"), []).append(i)
    forks = {h: ls for h, ls in claims.items() if len(ls) > 1}
    if forks:
        return {"ok": False, "lines": n,
                "break_at": min(min(ls[1:]) for ls in forks.values()),
                "kind": "fork",
                "reason": (f"{len(forks)} predecessor(s) claimed by more than one line; "
                           f"{sum(len(ls) - 1 for ls in forks.values())} line(s) branch off. "
                           "Written by concurrent processes, not edited. Give each server "
                           "its own file with MCP_RECEIPT_DIR."),
                "forks": len(forks),
                "branched": sum(len(ls) - 1 for ls in forks.values()),
                "terminal_hash": None}

    # 3. A predecessor that is nowhere in the file at all is a removal. Tested
    #    against every hash present, not against the lines seen so far: a line
    #    that points forward has been moved, not deleted, and step 4 says so.
    present = set(hashes) | {_GENESIS}
    for i, rec in enumerate(records, start=1):
        if rec.get("prev_hash") not in present:
            return {"ok": False, "lines": n, "break_at": i, "kind": "missing",
                    "reason": "predecessor is not in this file; a line was removed",
                    "terminal_hash": None}

    # 4. Everything present and linked: is it in order?
    prev = _GENESIS
    for i, rec in enumerate(records, start=1):
        if rec.get("prev_hash") != prev:
            return {"ok": False, "lines": n, "break_at": i, "kind": "reordered",
                    "reason": "lines are not in chain order",
                    "terminal_hash": None}
        prev = hashes[i - 1]

    return {"ok": True, "lines": n, "break_at": None, "kind": None,
            "reason": None, "terminal_hash": prev}


def verify_dir(directory: str) -> dict:
    """Verify every .jsonl in a receipts folder and describe the deposit whole.

    This is the object a disclosure cites: one manifest over a folder of
    single-writer chains, rather than six separate assertions a reader has to
    reconcile. `ok` is true only if every file verifies.
    """
    files = []
    total = 0
    by_server: dict = {}
    by_script: dict = {}
    sessions: dict = {}
    first = last = None

    names = sorted(f for f in os.listdir(directory) if f.endswith(".jsonl"))
    for name in names:
        path = os.path.join(directory, name)
        v = verify_chain(path)
        s = summary(path)
        files.append({
            "file": name,
            "ok": v["ok"],
            "kind": v.get("kind"),
            "reason": v.get("reason"),
            "break_at": v.get("break_at"),
            "lines": s["lines"],
            "first": s["first"],
            "last": s["last"],
            "servers": sorted(k for k in s["by_server"] if k),
            "terminal_hash": v.get("terminal_hash"),
        })
        total += s["lines"]
        for k, c in s["by_server"].items():
            by_server[k] = by_server.get(k, 0) + c
        for k, c in s["by_script"].items():
            by_script[k] = by_script.get(k, 0) + c
        for rec in read_log(path):
            sess = rec.get("session") or ""
            sessions[sess] = sessions.get(sess, 0) + 1
        if s["first"] and (first is None or s["first"] < first):
            first = s["first"]
        if s["last"] and (last is None or s["last"] > last):
            last = s["last"]

    return {
        "ledger_version": LEDGER_VERSION,
        "directory": os.path.abspath(directory),
        "ok": all(f["ok"] for f in files),
        "files": files,
        "totals": {
            "files": len(files),
            "lines": total,
            "first": first,
            "last": last,
            "by_server": by_server,
            "by_script": by_script,
            "by_session": sessions,
        },
    }


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
        r = verify_chain(sys.argv[2])
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["ok"] else 1)
    elif cmd == "verify-dir" and len(sys.argv) > 2:
        r = verify_dir(sys.argv[2])
        print(json.dumps(r, indent=2, ensure_ascii=False))
        sys.exit(0 if r["ok"] else 1)
    elif cmd == "manifest" and len(sys.argv) > 2:
        d = sys.argv[2]
        out = sys.argv[3] if len(sys.argv) > 3 else os.path.join(d, "manifest.json")
        r = verify_dir(d)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(r, fh, indent=2, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
        print(f"{out}: {r['totals']['lines']} lines across "
              f"{r['totals']['files']} file(s), ok={r['ok']}")
        sys.exit(0 if r["ok"] else 1)
    elif cmd == "summary" and len(sys.argv) > 2:
        print(json.dumps(summary(sys.argv[2]), indent=2, ensure_ascii=False))
    elif cmd == "csv" and len(sys.argv) > 3:
        print(f"{to_csv(sys.argv[2], sys.argv[3])} rows")
    else:
        print(__doc__)
        print("Commands:")
        print("  verify <file>              one chain; exit 1 if it does not verify")
        print("  verify-dir <dir>           every .jsonl in a receipts folder")
        print("  manifest <dir> [out]       write the folder manifest (default <dir>/manifest.json)")
        print("  summary <file>             counts by server and script")
        print("  csv <file> <out>           flatten one log to CSV")


if __name__ == "__main__":
    _cli()
