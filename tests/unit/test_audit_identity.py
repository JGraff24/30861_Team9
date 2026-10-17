"""Dylan: finding identity golden vectors, authored independently of app.audit.identity.

Literal digests were computed once with a standalone `python3 -I` stdlib one-liner;
`independent_digest` below re-derives them without importing production code.
"""

import hashlib
import json

import pytest

from app.audit.identity import (
    finding_id,
    format_xml_path,
    object_target,
    worksheet_target,
    xml_target,
)

ZERO = "0" * 64
VECTORS = [
    # Handoff fixed example (must match exactly).
    (
        (ZERO, "1.0.0", "ALT_TEXT", "Expenses", "xl/drawings/drawing1.xml#id=7"),
        "502d6624551c9da5c9a80d7efcc04be1119dad4a16f55171487949600a177714",
    ),
    # Bootstrap Unicode vectors: precomposed vs combining accent are different identities.
    (
        (ZERO, "1.0.0", "ALT_TEXT", "Dépenses 📊", "xl/drawings/drawing1.xml#id=7"),
        "65c48a7e1beb35c5e0068830b884d5d6f658793be5bcf21a1189a134f592ddc3",
    ),
    (
        (ZERO, "1.0.0", "ALT_TEXT", "De\u0301penses 📊", "xl/drawings/drawing1.xml#id=7"),
        "780ab5b340613108fa0dd216616b987cf1929a01b527495cbf3d9e63057fd9e7",
    ),
    # Case and whitespace are preserved, so they change the identity.
    (
        (ZERO, "1.0.0", "ALT_TEXT", "expenses", "xl/drawings/drawing1.xml#id=7"),
        "92f09d0c4f62a50eaff4c9ce07b40c83d8a6e5442418f8ab9300e8b5288408a9",
    ),
    (
        (ZERO, "1.0.0", "ALT_TEXT", " Expenses", "xl/drawings/drawing1.xml#id=7"),
        "72afc2ed151dbb3f300241f7e98aa57a3c7633518f0f28afc74eefea9dcffbaa",
    ),
    (
        (ZERO, "1.0.0", "WORKSHEET_NAME", "Sheet1", "xl/worksheets/sheet1.xml#sheetId=1"),
        "4c6a6e5903c478a165efc25d427b4838cba53abe3dd575242239b357995cf8cc",
    ),
    (
        (
            "f" * 64,
            "1.0.0",
            "ALT_TEXT",
            "Données\u3000",
            "xl/drawings/Drawing1.xml#xml=/wsDr[1]/twoCellAnchor[2]/pic[1]/nvPicPr[1]/cNvPr[1]",
        ),
        "6ee7c36a609b35897a15cc90a923dd829d791f82f128d88596c900cc9c4e2893",
    ),
    # JSON escaping of quote and backslash.
    (
        (ZERO, "1.0.0", "ALT_TEXT", '表"1\\', "xl/drawings/drawing1.xml#id=7"),
        "332a83a7f38bcd7decff68ce8b924fe29cab08b1f76cadb5c6dbe5a2a124fdf5",
    ),
]


def independent_digest(values: tuple[str, str, str, str, str]) -> str:
    text = "[" + ",".join(json.dumps(value, ensure_ascii=False) for value in values) + "]"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("values,expected", VECTORS)
def test_golden_vectors(values: tuple[str, str, str, str, str], expected: str) -> None:
    assert independent_digest(values) == expected
    assert finding_id(*values) == expected
    assert len(expected) == 64 and expected == expected.lower()


def test_any_component_change_changes_identity() -> None:
    base = (ZERO, "1.0.0", "ALT_TEXT", "Expenses", "xl/drawings/drawing1.xml#id=7")
    variants = [
        ("1" + ZERO[1:], *base[1:]),
        (base[0], "1.0.1", *base[2:]),
        (*base[:2], "WORKSHEET_NAME", *base[3:]),
        (*base[:4], "xl/drawings/drawing1.xml#id=8"),
        (*base[:4], "xl/drawings/Drawing1.xml#id=7"),
    ]
    digests = {finding_id(*base)} | {finding_id(*variant) for variant in variants}
    assert len(digests) == 6


def test_target_formats() -> None:
    assert object_target("xl/drawings/drawing1.xml", "7") == "xl/drawings/drawing1.xml#id=7"
    assert worksheet_target("xl/worksheets/sheet1.xml", "4") == "xl/worksheets/sheet1.xml#sheetId=4"
    path = (("wsDr", 1), ("twoCellAnchor", 2), ("pic", 1), ("nvPicPr", 1), ("cNvPr", 1))
    assert format_xml_path(path) == "/wsDr[1]/twoCellAnchor[2]/pic[1]/nvPicPr[1]/cNvPr[1]"
    assert (
        xml_target("xl/d.xml", path)
        == "xl/d.xml#xml=/wsDr[1]/twoCellAnchor[2]/pic[1]/nvPicPr[1]/cNvPr[1]"
    )
