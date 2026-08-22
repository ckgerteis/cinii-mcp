# Changelog

Versions are the thing to cite. A count produced under one release is not
reproducible against another, so the release actually used should be named in
the text and, where a version DOI exists, cited by it.

Releases earlier than those below are on the repository's releases page; this
file begins where the record is precise enough to be worth writing down.

## 2.3.0 — 2026-08-22

- `mediation.py` 2.3.0. `emit()` now reports whether the deposit happened:
  `RECEIPT_NOT_DEPOSITED` (info) when `MCP_RECEIPT_LOG` is unset,
  `RECEIPT_WRITE_FAILED` (warning) when it is set and the write did not land.
  `deposit_enabled()` exposed beside `ledger_available()`.
- Additive. No field removed or renamed; `response-schema.json` unchanged.

## 2.2.0 — 2026-08-21

- `mediation.py` unified at 2.2.0 and vendored byte-identically across the
  server family: Hangul and CJK supplementary-plane script detection, ledger
  persistence via `emit()`, and the `searched_for` headline restored.
- `response-schema.json` published, with a Response format section in the README.
