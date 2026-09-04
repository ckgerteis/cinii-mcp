"""Parsing of CiNii Research single-record JSON-LD, from records captured on
2026-09-04. Until 3.0.1 an untagged Japanese title was reported as its own
English title, the `creator` nodes were not read (no authors), and NAID and
ISSN were dropped. Run with pytest, or directly."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from cinii_mcp.server import _item_from_record

FIXTURES = Path(__file__).parent / "fixtures"


def _load(crid: str) -> dict:
    return _item_from_record(json.loads((FIXTURES / f"crid_{crid}.json").read_text(encoding="utf-8")))


def test_untagged_japanese_title_is_japanese_only():
    item = _load("1050002213762702720")
    assert item["title"]["ja"] == "鈴木文治と大正勞働運動 (中)"
    assert item["title"]["en"] is None
    assert item["source"]["journal_ja"] == "法學研究 : 法律・政治・社会"
    assert item["source"]["journal_en"] is None
    assert item["source"]["volume"] == "32" and item["source"]["issue"] == "2/3"
    assert item["source"]["pages"] == "21-45" and item["source"]["year"] == 1959


def test_creator_nodes_and_identifiers_are_read():
    item = _load("1050002213762702720")
    assert item["authors"] == [{"ja": "中村, 勝範", "en": None}]
    assert item["ids"]["naid"] == "120006816768"
    assert item["ids"]["issn"] == "03890538"
    assert item["ids"]["crid"] == "1050002213762702720"


def test_english_tagged_record_without_title():
    item = _load("1571135649335369984")
    assert item["title"] == {"ko": None, "ja": None, "en": None, "romanized": None}
    assert item["authors"] == [{"ja": None, "en": "Japan-Vitnam Friendship Association."}]
    assert item["source"]["journal_en"] == "Socialist R. of Vietnam."
    assert item["source"]["journal_ja"] is None
    assert item["ids"]["naid"] == "10005672599"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  ", name)
    sys.exit(0)
