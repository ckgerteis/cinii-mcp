"""
mediation.py — unified response envelope for the whole server family (v2.2.0).

One file, vendored byte-identical into cinii-mcp, jstage-mcp, ndl-mcp and
korea-scholarship-mcp. Until 19 Aug 2026 there were **two** files both calling
themselves 2.1.0 and neither carrying the other's work:

  * the Japanese copy had emit() — ledger persistence — and classified Hangul as
    'latin', so a correct Korean query raised the romanisation warning;
  * the Korean copy knew Hangul and the CJK extensions but had no emit(), so
    Korean queries never reached the hash-chained deposit that every Japanese
    query entered.

2.2.0 is the union. Everything is additive: a consumer reading `.ja` or calling
dumps() is unaffected, so the Japanese servers can adopt it without migration.

Design rule, unchanged: every interpretation key is a typed field, not prose.
The output is deterministic — typed facts and typed diagnostics only, never a
server-composed summary or relevance score. A tool may show its choices; it may
not make them on the scholar's behalf.

Standard library only, except for the optional ledger import at the foot of the
file, which degrades to a no-op when ledger.py is not vendored alongside.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA_VERSION = "2.2.0"

# Unicode blocks used for script detection.
_HIRA = (0x3040, 0x309F)
_KATA = (0x30A0, 0x30FF)
_KATA_HALF = (0xFF66, 0xFF9D)
_HAN = (
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0x3400, 0x4DBF),    # Extension A
    (0xF900, 0xFAFF),    # Compatibility Ideographs
    (0x20000, 0x2A6DF),  # Extension B
    (0x2A700, 0x2EBEF),  # Extensions C-F
    (0x2F800, 0x2FA1F),  # Compatibility Supplement
    (0x30000, 0x3134F),  # Extension G
)
# The supplementary planes are not decoration. 고서 holdings — which OAK
# aggregates in bulk — are exactly where rare and variant ideographs occur, and
# without these ranges such a title is classified 'latin' and routed to the
# English slot: the same failure the Hangul addition below was written to fix.
_HANGUL = (
    (0xAC00, 0xD7A3),   # Hangul syllables
    (0x1100, 0x11FF),   # Jamo
    (0x3130, 0x318F),   # Compatibility jamo
    (0xA960, 0xA97F),   # Jamo Extended-A
    (0xD7B0, 0xD7FF),   # Jamo Extended-B
    (0xFFA0, 0xFFDC),   # Halfwidth jamo
)


def detect_script(s: Optional[str]) -> str:
    """Classify the dominant script of the query actually sent to the API.

    Returns one of: 'latin', 'kana', 'han', 'han_kana', 'hangul',
    'han_hangul', 'mixed'. Digits, whitespace and punctuation are ignored. An
    empty string is reported as 'latin' (the n/a case).

    This is the field that makes the English-to-Korean rendering visible. A
    'latin' value on a Korean-corpus query is the romanisation trap — the
    Revised Romanisation of a Korean title is not a string KCI or OAK holds,
    so a Latin query searches the English-title field only, silently. Servers
    raise SCRIPT_LATIN_QUERY for it.

    'han' on a Korean corpus is not an error: Korean records from before the
    Hangul-only convention, and 고서 (old book) holdings, carry Hanja titles.
    """
    if not s:
        return "latin"
    has_latin = has_hira = has_kata = has_han = has_hangul = False
    for ch in s:
        o = ord(ch)
        if "a" <= ch.lower() <= "z":
            has_latin = True
        elif _HIRA[0] <= o <= _HIRA[1]:
            has_hira = True
        elif (_KATA[0] <= o <= _KATA[1]) or (_KATA_HALF[0] <= o <= _KATA_HALF[1]):
            has_kata = True
        elif any(lo <= o <= hi for lo, hi in _HANGUL):
            has_hangul = True
        elif any(lo <= o <= hi for lo, hi in _HAN):
            has_han = True
        # everything else (digits, spaces, punctuation, ·, etc.) is ignored
    has_kana = has_hira or has_kata
    if has_latin and (has_han or has_kana or has_hangul):
        return "mixed"
    if has_kana and has_hangul:
        return "mixed"
    if has_latin:
        return "latin"
    if has_han and has_hangul:
        return "han_hangul"
    if has_hangul:
        return "hangul"
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
    hits that look like a literature) is marked 'broad' rather than passing
    through clean. The matching_mode field tells the scholar how to read the
    number: a 'broad' metadata_conjunction set is precise-but-large, a 'broad'
    harvest_window_filter set is an artefact of the window, not of the corpus.
    """
    if total <= 0:
        return "none"
    if total <= 50:
        return "narrow"
    if total <= 1000:
        return "broad"
    return "very_broad"


def diag(level: str, code: str, message: str, hint: Optional[str] = None) -> dict:
    """Build one typed diagnostic. `code` must be in the closed registry
    documented in the README."""
    return {"level": level, "code": code, "message": message, "hint": hint}


def make_item(
    *,
    title_ko: Optional[str] = None,
    title_ja: Optional[str] = None,
    title_en: Optional[str] = None,
    title_romanized: Optional[str] = None,
    authors: Optional[list[dict]] = None,
    journal_ko: Optional[str] = None,
    journal_ja: Optional[str] = None,
    journal_en: Optional[str] = None,
    volume: Optional[str] = None,
    issue: Optional[str] = None,
    pages: Optional[str] = None,
    year: Optional[int] = None,
    doi: Optional[str] = None,
    crid: Optional[str] = None,
    naid: Optional[str] = None,
    kci_id: Optional[str] = None,
    uci: Optional[str] = None,
    issn: Optional[str] = None,
    oai_id: Optional[str] = None,
    url_ko: Optional[str] = None,
    url_ja: Optional[str] = None,
    url_en: Optional[str] = None,
    fulltext_url: Optional[str] = None,
    matched_in: str = "unknown",
    record_type: str = "article",
    holding_org: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    """Build one schema-conformant item record.

    `title_romanized` stays None unless the *source* supplies a romanisation.
    Neither KCI nor OAK does, and this server will not generate one: Revised
    Romanisation of a Korean name requires knowing the name, and a
    machine-transliterated string presented as bibliographic data is a
    fabrication with the shape of a fact.
    """
    return {
        "title": {
            "ko": title_ko,
            "ja": title_ja,
            "en": title_en,
            "romanized": title_romanized,
        },
        "authors": authors or [],
        "source": {
            "journal_ko": journal_ko,
            "journal_ja": journal_ja,
            "journal_en": journal_en,
            "volume": volume,
            "issue": issue,
            "pages": pages,
            "year": year,
            "holding_org": holding_org,
        },
        "ids": {
            "doi": doi,
            "crid": crid,
            "naid": naid,
            "kci_id": kci_id,
            "uci": uci,
            "issn": issn,
            "oai_id": oai_id,
            "url_ko": url_ko,
            "url_ja": url_ja,
            "url_en": url_en,
            "fulltext_url": fulltext_url,
        },
        "matched_in": matched_in,
        "record_type": record_type,
        "extra": extra or {},
    }


def make_receipt(normalized: str, params: dict, items: list[dict]) -> dict:
    """A loggable, citable receipt: stable hash + the record identifiers.

    Credential-bearing parameters must be stripped by the caller before they
    reach this function; the receipt is designed to be pasted into a notebook.
    """
    basis = json.dumps(
        {"q": normalized, "params": params}, ensure_ascii=False, sort_keys=True
    )
    qhash = "sha256:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()
    ids = []
    for it in items:
        d = it["ids"]
        ident = (
            d.get("doi")
            or d.get("kci_id")
            or d.get("crid")
            or d.get("naid")
            or d.get("oai_id")
        )
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
    """Assemble the full response envelope. `items` may be empty (zero result).

    `total` is whatever the *source* reported. Where the source reports no
    total — OAK sends no completeListSize — the caller passes the number of
    records the window actually yielded and raises OAI_WINDOW_TRUNCATED, so
    that a returned count is never mistaken for a corpus count.
    """
    try:
        total_i = int(total)
    except (TypeError, ValueError):
        total_i = 0
    env: dict[str, Any] = {
        "server": server,
        "schema_version": SCHEMA_VERSION,
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


# ---------------------------------------------------------------------------
# Persistence — the only impure function in this module
# ---------------------------------------------------------------------------

# Two import forms because the family has two layouts: the Japanese servers are
# flat (ledger.py sits beside this file) and korea-scholarship-mcp is a package.
# Getting this wrong is silent — _ledger falls to None, emit() degrades to
# dumps(), and every query goes unrecorded with no error to notice.
try:  # packaged: src/<pkg>/ledger.py
    from . import ledger as _ledger
except ImportError:  # flat: ledger.py vendored alongside
    try:
        import ledger as _ledger
    except Exception:  # pragma: no cover
        _ledger = None
except Exception:  # pragma: no cover
    _ledger = None


def ledger_available() -> bool:
    """Whether receipts can actually be deposited.

    Exposed so a server can report the fact rather than assume it. A missing
    ledger is a legitimate configuration; a missing ledger mistaken for a
    working one is not.
    """
    return _ledger is not None


def emit(envelope: dict) -> str:
    """Record the envelope to the query ledger, then serialize it.

    This is dumps() plus persistence. It exists so that the receipt the envelope
    already carries survives the conversation it was issued in.
    """
    if _ledger is not None:
        _ledger.record_envelope(envelope)
    return dumps(envelope)
