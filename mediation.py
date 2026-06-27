"""
mediation.py — unified response envelope for cinii-mcp and jstage-mcp (v2.0.0).

Implements the envelope documented in RESPONSE_SCHEMA.md / response-schema.json.
Pure standard library; no third-party imports, so it can be unit-tested in
isolation and vendored identically into both single-purpose repos.

Design rule: every interpretation key is a typed field, not prose. The output
is deterministic — typed facts and typed diagnostics only, never a
server-composed summary or relevance score. A tool may show its choices; it may
not make them on the scholar's behalf.

This file is vendored (an identical copy lives in each repo) so that each server
remains a server.py + mediation.py pair with no cross-repo dependency. Keep the
two copies in sync; they are byte-identical by intent.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA_VERSION = "2.0.0"

# Unicode blocks used for script detection.
_HIRA = (0x3040, 0x309F)
_KATA = (0x30A0, 0x30FF)
_KATA_HALF = (0xFF66, 0xFF9D)
_HAN = ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF))


def detect_script(s: Optional[str]) -> str:
    """Classify the dominant script of the query actually sent to the API.

    Returns one of: 'latin', 'kana', 'han', 'han_kana', 'mixed'. Digits,
    whitespace, and punctuation are ignored. An empty string is reported as
    'latin' (the n/a case). This is the field that makes the English-to-Japanese
    rendering visible: a 'latin' value on a Japanese-corpus query is the romaji
    trap, and the server raises SCRIPT_LATIN_QUERY for it.
    """
    if not s:
        return "latin"
    has_latin = has_hira = has_kata = has_han = False
    for ch in s:
        o = ord(ch)
        if "a" <= ch.lower() <= "z":
            has_latin = True
        elif _HIRA[0] <= o <= _HIRA[1]:
            has_hira = True
        elif (_KATA[0] <= o <= _KATA[1]) or (_KATA_HALF[0] <= o <= _KATA_HALF[1]):
            has_kata = True
        elif any(lo <= o <= hi for lo, hi in _HAN):
            has_han = True
        # everything else (digits, spaces, punctuation, ・, etc.) is ignored
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


def classify_breadth(total: int) -> str:
    """Graduated set-size signal, set on mid-sized sets and not only at extremes.

    Thresholds are deliberately low so the dangerous middle case (a few hundred
    full-text hits that look like a literature) is marked 'broad' rather than
    passing through clean. The matching_mode field tells the scholar how to read
    the number: a 'broad' metadata_conjunction set is precise-but-large, a
    'broad' full_text_broad set is likely noisy.
    """
    if total <= 0:
        return "none"
    if total <= 50:
        return "narrow"
    if total <= 1000:
        return "broad"
    return "very_broad"


def diag(level: str, code: str, message: str, hint: Optional[str] = None) -> dict:
    """Build one typed diagnostic. `code` must be in the closed registry."""
    return {"level": level, "code": code, "message": message, "hint": hint}


def make_item(
    *,
    title_ja: Optional[str] = None,
    title_en: Optional[str] = None,
    title_romanized: Optional[str] = None,
    authors: Optional[list[dict]] = None,
    journal_ja: Optional[str] = None,
    journal_en: Optional[str] = None,
    volume: Optional[str] = None,
    issue: Optional[str] = None,
    pages: Optional[str] = None,
    year: Optional[int] = None,
    doi: Optional[str] = None,
    crid: Optional[str] = None,
    naid: Optional[str] = None,
    url_ja: Optional[str] = None,
    url_en: Optional[str] = None,
    matched_in: str = "unknown",
    record_type: str = "article",
) -> dict:
    """Build one schema-conformant item record."""
    return {
        "title": {"ja": title_ja, "en": title_en, "romanized": title_romanized},
        "authors": authors or [],
        "source": {
            "journal_ja": journal_ja,
            "journal_en": journal_en,
            "volume": volume,
            "issue": issue,
            "pages": pages,
            "year": year,
        },
        "ids": {
            "doi": doi,
            "crid": crid,
            "naid": naid,
            "url_ja": url_ja,
            "url_en": url_en,
        },
        "matched_in": matched_in,
        "record_type": record_type,
    }


def make_receipt(normalized: str, params: dict, items: list[dict]) -> dict:
    """A loggable, citable receipt: stable hash + the record identifiers."""
    basis = json.dumps(
        {"q": normalized, "params": params}, ensure_ascii=False, sort_keys=True
    )
    qhash = "sha256:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()
    ids = []
    for it in items:
        ident = it["ids"].get("doi") or it["ids"].get("crid") or it["ids"].get("naid")
        if ident:
            ids.append(ident)
    return {
        "issued_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "query_hash": qhash,
        "result_ids": ids,
    }


def build_envelope(
    *,
    server: str,
    operation: str,
    input_terms: str,
    normalized: str,
    params: dict,
    matching_mode: str,
    total: int,
    start: int,
    items: list[dict],
    diagnostics: list[dict],
    attribution: str,
    coverage_note: Optional[str] = None,
    suggestions: Optional[list[dict]] = None,
) -> dict:
    """Assemble the full response envelope. `items` may be empty (zero result)."""
    try:
        total_i = int(total)
    except (TypeError, ValueError):
        total_i = 0
    env: dict[str, Any] = {
        "server": server,
        "operation": operation,
        "query": {
            "input_terms": input_terms,
            "normalized": normalized,
            "script": detect_script(normalized),
            "params": params,
        },
        "matching_mode": matching_mode,
        "result": {
            "total": total_i,
            "returned": len(items),
            "start": int(start),
            "breadth": classify_breadth(total_i),
        },
        "items": items,
        "diagnostics": diagnostics
        or [diag("info", "OK", f"{len(items)} record(s) returned.", None)],
        "coverage_note": coverage_note,
        "receipt": make_receipt(normalized, params, items),
        "attribution": attribution,
    }
    if suggestions:
        env["suggestions"] = suggestions
    return env


def dumps(envelope: dict) -> str:
    """Serialize an envelope deterministically for return to the client."""
    return json.dumps(envelope, ensure_ascii=False, indent=2)
