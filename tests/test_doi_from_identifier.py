"""CiNii search results carry the DOI in dc:identifier, not prism:doi.

Shape taken from a live record, 27 Nov 2026 (CRID 1390307811297230848).
"""
from cinii_mcp import server as S

RAW = {
    "@id": "https://cir.nii.ac.jp/crid/1390307811297230848",
    "title": "教育経験の越境とアイデンティティの再構成―在日コリアンのオーラル・ヒストリーに基づく質的分析―",
    "dc:identifier": [{"@type": "cir:DOI", "@value": "10.69231/eastasianstudies.35.0_1"}],
}


def test_search_item_reads_doi_from_dc_identifier():
    item = S._item_from_cinii(RAW, "article")
    assert item["ids"]["doi"] == "10.69231/eastasianstudies.35.0_1"
    assert item["ids"]["crid"] == "1390307811297230848"


def test_prism_doi_still_wins_when_present():
    raw = dict(RAW, **{"prism:doi": "10.1/x"})
    assert S._item_from_cinii(raw, "article")["ids"]["doi"] == "10.1/x"


def test_no_doi_anywhere_stays_none():
    raw = {k: v for k, v in RAW.items() if k != "dc:identifier"}
    assert S._item_from_cinii(raw, "article")["ids"]["doi"] is None
