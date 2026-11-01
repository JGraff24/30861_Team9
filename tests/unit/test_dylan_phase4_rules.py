"""Ruleset 2.0.0 detection: the six rules, review context and shared policy vectors.

Expected results are written from each synthetic seed and from the companion policy
vectors in docs/contracts/phase4, never copied from audit() output.
"""

import json
from fractions import Fraction
from pathlib import Path

import pytest

from app.audit.color import contrast_ratio, is_large_text, passes
from app.audit.engine import OoxmlWorkbookEngine
from app.audit.identity import finding_id
from app.audit.rules import is_generic_worksheet_name
from app.audit.rules_v2 import ambiguous_label
from app.contracts.errors import ProjectException
from app.contracts.models import AuditEvaluation, Finding, ProcessingLimits
from app.contracts.phase4 import RULE_IDS
from app.services.reports import validate_evaluation, validate_finding_ids
from app.workbooks.formulas import parse_hyperlink, rewrite_sheet_references, scan_formula
from app.workbooks.styles import apply_tint, hex_rgb, rgb_hex
from tests.unit.phase4_builder import (
    Cell,
    Font,
    Link,
    Sheet,
    TableDef,
    Workbook,
    formula,
    number,
    picture_drawing,
    text,
)

ENGINE = OoxmlWorkbookEngine()
VECTORS = json.loads(
    (Path(__file__).parents[2] / "docs/contracts/phase4/phase4-policy-vectors.json").read_text()
)
FIXTURES = Path(__file__).parents[1] / "fixtures/synthetic"


def audit(data: bytes) -> AuditEvaluation:
    validated = ENGINE.validate(data, "book.xlsx", ProcessingLimits())
    evaluation = ENGINE.audit(data, validated, "2.0.0")
    # The backend's own coherence checks must accept every engine result.
    validate_evaluation(evaluation, "2.0.0")
    import hashlib

    validate_finding_ids(evaluation, hashlib.sha256(data).hexdigest(), "2.0.0")
    return evaluation


def tuples(evaluation: AuditEvaluation, rule: str | None = None) -> list[tuple[str, str, str]]:
    return [
        (f.worksheet, f.target, f.classification.value)
        for f in evaluation.findings
        if rule is None or f.ruleId == rule
    ]


def context_for(evaluation: AuditEvaluation, finding: Finding) -> dict[str, object] | None:
    assert evaluation.review_context is not None
    item = next(i for i in evaluation.review_context if i.findingId == finding.findingId)
    return item.currentValue


# -- shared policy vectors ------------------------------------------------------------


@pytest.mark.parametrize("vector", VECTORS["identity_vectors"])
def test_identity_vectors(vector: dict[str, object]) -> None:
    assert finding_id(*vector["input"]) == vector["expected"]  # type: ignore[arg-type,misc]


@pytest.mark.parametrize("vector", VECTORS["generic_names"])
def test_generic_name_vectors(vector: dict[str, object]) -> None:
    assert is_generic_worksheet_name(str(vector["name"])) is vector["triggers"]


@pytest.mark.parametrize("vector", VECTORS["ambiguous_labels"])
def test_ambiguous_label_vectors(vector: dict[str, object]) -> None:
    assert ambiguous_label(str(vector["label"]), str(vector["destination"])) is vector["triggers"]


@pytest.mark.parametrize("vector", VECTORS["contrast"])
def test_contrast_vectors(vector: dict[str, object]) -> None:
    font, fill = hex_rgb(str(vector["font_rgb"])), hex_rgb(str(vector["fill_rgb"]))
    assert font is not None and fill is not None
    ratio = contrast_ratio(font, fill)
    assert ratio == pytest.approx(vector["ratio"], abs=1e-12)
    assert passes(ratio, False) is vector["passes_normal"]
    assert passes(ratio, True) is vector["passes_large"]


def test_large_text_thresholds() -> None:
    assert is_large_text(Fraction(18), False)
    assert is_large_text(Fraction(14), True)
    assert not is_large_text(Fraction(14), False)
    assert not is_large_text(Fraction(27, 2), True)


@pytest.mark.parametrize("vector", VECTORS["tint"])
def test_tint_vectors_follow_exact_half_up_rule(vector: dict[str, object]) -> None:
    base = hex_rgb(str(vector["base_rgb"]))
    assert base is not None
    expected = str(vector["expected_rgb"])
    assert rgb_hex(apply_tint(base, Fraction(str(vector["tint"])))) == expected


def header_workbook(cells: dict[str, object]) -> bytes:
    seeded: dict[str, Cell | str | float] = {
        ref: text(value) if isinstance(value, str) else number(float(value))  # type: ignore[arg-type]
        for ref, value in cells.items()
    }
    return Workbook([Sheet("Data", seeded)]).build()


@pytest.mark.parametrize("vector", VECTORS["header_components"])
def test_header_component_vectors(vector: dict[str, object]) -> None:
    evaluation = audit(header_workbook(vector["cells"]))  # type: ignore[arg-type]
    found = tuples(evaluation, "TABLE_HEADER")
    if "expected_table_findings" in vector:
        assert found == []
    else:
        assert found == [
            (
                "Data",
                "xl/worksheets/sheet1.xml" + str(vector["expected_target_suffix"]),
                str(vector["expected_classification"]),
            )
        ]


def test_repeated_id_targets_match_contract() -> None:
    evaluation = audit((FIXTURES / "repeated_ids.xlsx").read_bytes())
    assert [f.target for f in evaluation.findings] == VECTORS["duplicate_id_targets"]
    assert {f.classification.value for f in evaluation.findings} == {"MANUAL_REVIEW"}
    assert len({f.findingId for f in evaluation.findings}) == 2


# -- whole-audit shape -----------------------------------------------------------------


def test_clean_workbook_has_all_six_checks_and_no_findings() -> None:
    data = Workbook([Sheet("Expenses", {"A1": "Monthly expenses"})]).build()
    evaluation = audit(data)
    assert evaluation.findings == []
    assert evaluation.review_context == []
    assert evaluation.checks_performed == RULE_IDS


def test_repeat_audits_are_identical() -> None:
    data = (FIXTURES / "dylan/seeded_issues.xlsx").read_bytes()
    results = [audit(data) for _ in range(5)]
    assert all(result == results[0] for result in results)


def test_legacy_ruleset_keeps_phase3_behavior() -> None:
    data = (FIXTURES / "dylan/seeded_issues.xlsx").read_bytes()
    validated = ENGINE.validate(data, "x.xlsx", ProcessingLimits())
    legacy = ENGINE.audit(data, validated, "1.0.0")
    assert legacy.review_context is None and legacy.checks_performed is None
    assert {f.ruleId for f in legacy.findings} == {"ALT_TEXT", "WORKSHEET_NAME"}
    assert "2.0.0" in ENGINE.supported_rulesets and "1.0.0" in ENGINE.supported_rulesets


def test_findings_sorted_and_contexts_aligned() -> None:
    evaluation = audit((FIXTURES / "dylan/seeded_issues.xlsx").read_bytes())
    keys = [(f.ruleId, f.worksheet, f.target) for f in evaluation.findings]
    assert keys == sorted(keys)
    assert evaluation.review_context is not None
    assert [i.findingId for i in evaluation.review_context] == [
        f.findingId for f in evaluation.findings
    ]


# -- MERGED_CELLS --------------------------------------------------------------------


def test_merged_cells_screen_only_populated_rectangles() -> None:
    data = Workbook(
        [
            Sheet(
                "Report",
                {"A1": "Quarterly report", "D5": 4},
                merges=["A1:C1", "$E$1:F1", "A1:C1", "D5:D5", "D4:E5", "H1:I2"],
            )
        ]
    ).build()
    evaluation = audit(data)
    assert tuples(evaluation, "MERGED_CELLS") == [
        ("Report", "xl/worksheets/sheet1.xml#merge=A1:C1", "MANUAL_REVIEW"),
        ("Report", "xl/worksheets/sheet1.xml#merge=D4:E5", "MANUAL_REVIEW"),
    ]


def test_overlapping_merges_need_manual_review_and_limitation() -> None:
    data = Workbook([Sheet("Report", {}, merges=["A1:B2", "B2:C3"])]).build()
    evaluation = audit(data)
    assert [t for _, t, _ in tuples(evaluation, "MERGED_CELLS")] == [
        "xl/worksheets/sheet1.xml#merge=A1:B2",
        "xl/worksheets/sheet1.xml#merge=B2:C3",
    ]
    assert any("overlapping merged ranges" in item for item in evaluation.limitations)


# -- TABLE_HEADER ----------------------------------------------------------------------


def test_disabled_formal_header_matching_columns_is_confirmable() -> None:
    table = TableDef("A1:B3", ["Name", "Score"], header_rows="0")
    data = Workbook(
        [
            Sheet(
                "Scores",
                {"A1": "Name", "B1": "Score", "A2": "Ada", "B2": 9, "A3": "Al", "B3": 7},
                tables=[table],
            )
        ]
    ).build()
    evaluation = audit(data)
    [finding] = evaluation.findings
    assert (finding.target, finding.classification.value) == (
        "xl/tables/table1.xml#tableId=1",
        "REQUIRES_USER_INPUT",
    )
    assert context_for(evaluation, finding) == {"headerRow": 1}


@pytest.mark.parametrize(
    ("cells", "columns"),
    [
        ({"A1": "Name", "B1": "name "}, ["Name", "name "]),  # duplicate after trim/casefold
        ({"A1": "Name"}, ["Name", "Score"]),  # blank header cell
        ({"A1": "Name", "B1": formula('"Sc"&"ore"', "Score", "str")}, ["Name", "Score"]),
        ({"A1": "Name", "B1": "Total"}, ["Name", "Score"]),  # metadata mismatch
    ],
)
def test_formal_header_problems_are_manual(cells: dict[str, object], columns: list[str]) -> None:
    body: dict[str, Cell | str | float] = {"A2": "Ada", "B2": 9}
    body.update(cells)  # type: ignore[arg-type]
    data = Workbook([Sheet("Scores", body, tables=[TableDef("A1:B2", columns)])]).build()
    assert tuples(audit(data), "TABLE_HEADER") == [
        ("Scores", "xl/tables/table1.xml#tableId=1", "MANUAL_REVIEW")
    ]


def test_valid_formal_table_has_no_finding_and_excludes_its_cells() -> None:
    data = Workbook(
        [
            Sheet(
                "Scores",
                {"A1": "Name", "B1": "Score", "A2": "Ada", "B2": 9},
                tables=[TableDef("A1:B2", ["Name", "Score"])],
            )
        ]
    ).build()
    assert tuples(audit(data), "TABLE_HEADER") == []


def test_duplicate_table_ids_use_indexed_root_target() -> None:
    tables = [TableDef("A1:B2", ["N", "S"], name="T1"), TableDef("D1:E2", ["N", "S"], name="T2")]
    cells: dict[str, Cell | str | float] = {
        "A1": "N",
        "B1": "S",
        "A2": 1,
        "B2": 2,
        "D1": "N",
        "E1": "S",
        "D2": 1,
        "E2": 2,
    }
    evaluation = audit(Workbook([Sheet("Data", cells, tables=tables)]).build())
    assert tuples(evaluation, "TABLE_HEADER") == [
        ("Data", "xl/tables/table1.xml#xml=/table[1]", "MANUAL_REVIEW"),
        ("Data", "xl/tables/table2.xml#xml=/table[1]", "MANUAL_REVIEW"),
    ]


def test_merged_or_numeric_header_candidates_are_manual() -> None:
    merged = Workbook(
        [Sheet("Data", {"A1": "Name", "B1": "Score", "A2": "Ada", "B2": 9}, merges=["A2:B2"])]
    ).build()
    numeric = header_workbook({"A1": 2024, "B1": 2025, "A2": 1, "B2": 2})
    for data in (merged, numeric):
        assert [c for _, _, c in tuples(audit(data), "TABLE_HEADER")] == ["MANUAL_REVIEW"]


# -- LINK_TEXT -------------------------------------------------------------------------


def link_book(*links: Link, cells: dict[str, Cell | str | float] | None = None) -> bytes:
    return Workbook([Sheet("Links", cells or {}, links=list(links))]).build()


def test_simple_ambiguous_cell_link_is_user_input() -> None:
    data = link_book(Link("B2", url="https://example.edu/report"), cells={"B2": "Click here"})
    evaluation = audit(data)
    [finding] = evaluation.findings
    assert (finding.target, finding.classification.value) == (
        "xl/worksheets/sheet1.xml#cell=B2",
        "REQUIRES_USER_INPUT",
    )
    assert context_for(evaluation, finding) == {"text": "Click here"}


def test_descriptive_and_internal_links_do_not_trigger() -> None:
    data = link_book(
        Link("A1", url="https://example.edu"),
        Link("A3", location="Budget!A1"),
        cells={"A1": "Annual accessibility report", "A3": "Budget summary"},
    )
    assert tuples(audit(data), "LINK_TEXT") == []


def test_raw_address_and_empty_labels_trigger() -> None:
    data = link_book(
        Link("A1", url="https://example.edu"),
        Link("A3", url="https://example.edu"),
        cells={"A1": "https://example.edu", "A3": Cell(None, "empty")},
    )
    assert [(t, c) for _, t, c in tuples(audit(data), "LINK_TEXT")] == [
        ("xl/worksheets/sheet1.xml#cell=A1", "REQUIRES_USER_INPUT"),
        ("xl/worksheets/sheet1.xml#cell=A3", "REQUIRES_USER_INPUT"),
    ]


def test_unsupported_link_representations_are_manual_at_xml_paths() -> None:
    data = link_book(
        Link("A1:B1", url="https://example.edu"),
        Link("C1", url="https://example.edu"),
        Link("C1", url="https://example.org"),
        Link("D1", url="https://example.edu"),
        cells={"A1": "here", "C1": "more", "D1": Cell("link", rich_runs=True)},
    )
    assert tuples(audit(data), "LINK_TEXT") == [
        (
            "Links",
            "xl/worksheets/sheet1.xml#xml=/worksheet[1]/hyperlinks[1]/hyperlink[1]",
            "MANUAL_REVIEW",
        ),
        (
            "Links",
            "xl/worksheets/sheet1.xml#xml=/worksheet[1]/hyperlinks[1]/hyperlink[2]",
            "MANUAL_REVIEW",
        ),
        (
            "Links",
            "xl/worksheets/sheet1.xml#xml=/worksheet[1]/hyperlinks[1]/hyperlink[3]",
            "MANUAL_REVIEW",
        ),
        (
            "Links",
            "xl/worksheets/sheet1.xml#xml=/worksheet[1]/hyperlinks[1]/hyperlink[4]",
            "MANUAL_REVIEW",
        ),
    ]


def test_hyperlink_formulas_are_screened_but_never_editable() -> None:
    data = link_book(
        cells={
            "A1": formula('HYPERLINK("https://example.edu","Read more.")', "Read more.", "str"),
            "A2": formula(
                '_xlfn.HYPERLINK("https://example.edu","Course catalog")', "Course catalog", "str"
            ),
            "A3": formula('HYPERLINK(B1,"here")', "here", "str"),
        }
    )
    evaluation = audit(data)
    assert tuples(evaluation, "LINK_TEXT") == [
        (
            "Links",
            "xl/worksheets/sheet1.xml#xml=/worksheet[1]/sheetData[1]/row[1]/c[1]",
            "MANUAL_REVIEW",
        ),
        (
            "Links",
            "xl/worksheets/sheet1.xml#xml=/worksheet[1]/sheetData[1]/row[3]/c[1]",
            "MANUAL_REVIEW",
        ),
    ]
    assert any("HYPERLINK formulas with computed" in item for item in evaluation.limitations)


@pytest.mark.parametrize(
    ("expression", "label"),
    [
        ('HYPERLINK("https://a.example","Report")', "Report"),
        ('=hyperlink( "https://a.example" )', "https://a.example"),
        ('_xlfn.HYPERLINK("x","say ""hi"", ok")', 'say "hi", ok'),
    ],
)
def test_literal_hyperlink_parsing(expression: str, label: str) -> None:
    call = parse_hyperlink(expression)
    assert call is not None and call.literal and call.label == label


@pytest.mark.parametrize(
    "expression", ['HYPERLINK("a";"b")', "HYPERLINK(A1)", 'IF(1,HYPERLINK("a","b"))']
)
def test_unsupported_hyperlink_forms(expression: str) -> None:
    call = parse_hyperlink(expression)
    assert call is not None and not call.literal


def test_plain_urls_without_links_are_not_links() -> None:
    assert parse_hyperlink('"HYPERLINK(""x"")"') is None
    assert tuples(audit(link_book(cells={"A1": "https://example.edu"})), "LINK_TEXT") == []


# -- COLOR_CONTRAST ------------------------------------------------------------------


def color_book(
    fonts: list[Font],
    fills: list[str | None],
    formats: list[tuple[int, int]],
    cells: dict[str, Cell | str | float],
    **extra: object,
) -> bytes:
    return Workbook(
        [Sheet("Colors", cells, **extra)], fonts=fonts, fills=fills, formats=formats
    ).build()  # type: ignore[arg-type]


def test_default_black_on_white_does_not_trigger_and_is_disclosed() -> None:
    evaluation = audit(color_book([Font()], [None, "gray125"], [(0, 0)], {"A1": "Plain"}))
    assert tuples(evaluation, "COLOR_CONTRAST") == []
    assert any("screened as white" in item for item in evaluation.limitations)


def test_low_contrast_grey_and_color_meaning_are_combined_per_cell() -> None:
    fonts = [
        Font('theme="1"'),
        Font('rgb="FF777777"'),
        Font('rgb="FFFF0000"'),
        Font('rgb="FF777777"', 18),
    ]
    formats = [(0, 0), (1, 0), (2, 0), (3, 0)]
    cells: dict[str, Cell | str | float] = {
        "A1": text("Low", 1),
        "A2": text("Red", 2),
        "A3": text("Large grey", 3),
        "A4": text("Theme black", 0),
    }
    evaluation = audit(color_book(fonts, [None, "gray125"], formats, cells))
    found = {
        f.target.rpartition("=")[2]: f for f in evaluation.findings if f.ruleId == "COLOR_CONTRAST"
    }
    assert sorted(found) == ["A1", "A2"]
    assert "4.48:1, below 4.5:1" in found["A1"].message
    assert "below" in found["A2"].message and "convey meaning" in found["A2"].message


def test_solid_fill_theme_tint_and_unsupported_colors() -> None:
    fonts = [Font('theme="1"'), Font('auto="1"'), Font('indexed="64"')]
    fills = [None, "gray125", 'theme="4" tint="-0.5"', 'rgb="FFEEEEEE"']
    formats = [(0, 0), (0, 2), (1, 0), (2, 0), (0, 3)]
    cells: dict[str, Cell | str | float] = {
        "A1": text("Accent fill", 1),
        "A2": text("Auto font", 2),
        "A3": text("System index", 3),
        "A4": text("Light grey fill", 4),
    }
    evaluation = audit(color_book(fonts, fills, formats, cells))
    by_cell = {f.target.rpartition("=")[2]: f.message for f in evaluation.findings}
    assert "#1A334D" in by_cell["A1"]  # accent1 336699 with -0.5 tint, exact half-up
    assert "could not be calculated" in by_cell["A2"]
    assert "could not be calculated" in by_cell["A3"]
    assert "A4" not in by_cell  # neutral and sufficient contrast


def test_conditional_color_ranges_and_suppression() -> None:
    conditional = (
        '<conditionalFormatting sqref="A1:A3 C1"><cfRule type="colorScale" priority="1">'
        '<colorScale><cfvo type="min"/><cfvo type="max"/><color rgb="FFFF0000"/>'
        '<color rgb="FF00FF00"/></colorScale></cfRule></conditionalFormatting>'
        '<conditionalFormatting sqref="E1"><cfRule type="cellIs" dxfId="0" priority="2" '
        'operator="greaterThan"><formula>5</formula></cfRule></conditionalFormatting>'
    )
    fonts = [Font(), Font('rgb="FFFF0000"')]
    data = Workbook(
        [Sheet("Colors", {"A1": text("Red", 1), "E1": 7}, conditional=conditional)],
        fonts=fonts,
        formats=[(0, 0), (1, 0)],
        dxfs=['<dxf><font><color rgb="FF9C0006"/></font></dxf>'],
    ).build()
    assert [t for _, t, _ in tuples(audit(data), "COLOR_CONTRAST")] == [
        "xl/worksheets/sheet1.xml#conditional=A1:A3",
        "xl/worksheets/sheet1.xml#conditional=C1:C1",
        "xl/worksheets/sheet1.xml#conditional=E1:E1",
    ]


def test_styled_table_overlay_is_manual() -> None:
    data = Workbook(
        [
            Sheet(
                "Data",
                {"A1": "N", "B1": "S", "A2": "x", "B2": 1},
                tables=[TableDef("A1:B2", ["N", "S"], style="TableStyleMedium2")],
            )
        ]
    ).build()
    evaluation = audit(data)
    assert len(tuples(evaluation, "COLOR_CONTRAST")) == 4
    assert any("table style overlay" in item for item in evaluation.limitations)


# -- WORKSHEET_NAME and ALT_TEXT classification --------------------------------------


def test_rename_is_manual_when_references_cannot_be_proven() -> None:
    data = Workbook([Sheet("Sheet1", {"A1": formula('INDIRECT("Sheet1!B1")', 0)})]).build()
    evaluation = audit(data)
    assert tuples(evaluation, "WORKSHEET_NAME") == [
        ("Sheet1", "xl/worksheets/sheet1.xml#sheetId=1", "MANUAL_REVIEW")
    ]
    assert any("INDIRECT" in item for item in evaluation.limitations)


def test_alt_text_context_offers_existing_values() -> None:
    data = Workbook([Sheet("Charts", {}, drawing=picture_drawing("4", title="Revenue"))]).build()
    evaluation = audit(data)
    [finding] = evaluation.findings
    assert finding.classification.value == "REQUIRES_USER_INPUT"
    assert context_for(evaluation, finding) == {"title": "Revenue", "description": None}


def test_engine_rejects_audits_of_other_bytes_for_corrections() -> None:
    data = Workbook([Sheet("Sheet1", {"A1": "x"})]).build()
    other = Workbook([Sheet("Sheet2", {"A1": "x"})]).build()
    from tests.unit.test_dylan_phase4_corrections import record

    evaluation = audit(other)
    with pytest.raises(ProjectException) as error:
        ENGINE.validate_decisions(
            data, ENGINE.validate(data, "x.xlsx", ProcessingLimits()), record(evaluation), []
        )
    assert error.value.code == "SOURCE_INTEGRITY_FAILED"


# -- formula scanning ----------------------------------------------------------------


def test_sheet_reference_scanning() -> None:
    scan = scan_formula(
        "SUM(Sheet1!A1:B2,'My ''Q'' data'!$C$3,Sheet1:Sheet3!A1,[1]Sheet1!A1)&\"Sheet1!A1\""
    )
    assert [token.names for token in scan.sheet_tokens] == [
        ("Sheet1",),
        ("My 'Q' data",),
        ("Sheet1", "Sheet3"),
        ("Sheet1",),
    ]
    assert [token.external for token in scan.sheet_tokens] == [False, False, False, True]
    assert scan.functions == ("SUM",)


def test_rename_rewrite_respects_literals_and_similar_names() -> None:
    result = rewrite_sheet_references(
        "Sheet1!A1+'Sheet1'!B2+Sheet10!C3+sheet1!$D$4&\"Sheet1!A1\"", "Sheet1", "Budget's"
    )
    assert result.text == (
        "'Budget''s'!A1+'Budget''s'!B2+Sheet10!C3+'Budget''s'!$D$4&\"Sheet1!A1\""
    )
