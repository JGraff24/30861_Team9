"""Dylan: ALT_TEXT inventory and policy. Expected targets are authored from the seeds."""

import hashlib

import pytest
from ooxml_builder import (
    Drawing,
    Sheet,
    Workbook,
    alternate,
    anchor,
    chart,
    cnvpr,
    connector,
    drawing_xml,
    group,
    pic,
    shape,
)

from app.audit.engine import OoxmlWorkbookEngine
from app.contracts.models import AuditEvaluation, Finding, ProcessingLimits

ENGINE = OoxmlWorkbookEngine()
D1 = "xl/drawings/drawing1.xml"
WS_A = "Expenses"


def audit_objects(*anchors: str, hidden: bool = False) -> AuditEvaluation:
    sheet = Sheet(
        WS_A,
        "xl/worksheets/sheet1.xml",
        "1",
        state="hidden" if hidden else None,
        drawing=Drawing(D1, drawing_xml(*anchors)),
    )
    return audit_workbook(Workbook([sheet]))


def audit_workbook(wb: Workbook) -> AuditEvaluation:
    data = wb.build()
    before = hashlib.sha256(data).hexdigest()
    validated = ENGINE.validate(data, "book.xlsx", ProcessingLimits())
    evaluation = ENGINE.audit(data, validated, "1.0.0")
    assert hashlib.sha256(data).hexdigest() == before
    return evaluation


def summary(evaluation: AuditEvaluation) -> list[tuple[str, str, str, str]]:
    return [(f.ruleId, f.worksheet, f.target, f.classification.value) for f in evaluation.findings]


def only(evaluation: AuditEvaluation) -> Finding:
    assert len(evaluation.findings) == 1, summary(evaluation)
    return evaluation.findings[0]


INPUT = "REQUIRES_USER_INPUT"
REVIEW = "MANUAL_REVIEW"


# -- object kinds and missing fields ---------------------------------------------------


@pytest.mark.parametrize(
    "build,label",
    [(pic, "Picture"), (chart, "Chart"), (shape, "Shape"), (connector, "Connector")],
)
def test_each_object_kind_missing_both_fields(build, label: str) -> None:  # type: ignore[no-untyped-def]
    finding = only(audit_objects(anchor(build(cnvpr("7", "Object 7")))))
    assert (finding.ruleId, finding.category.value, finding.classification.value) == (
        "ALT_TEXT",
        "NON_TEXT_CONTENT",
        INPUT,
    )
    assert finding.target == f"{D1}#id=7"
    assert finding.worksheet == WS_A
    assert (
        finding.message
        == f'{label} "Object 7" is missing its alternative text title and description.'
    )
    assert finding.recommendation.startswith(
        "Provide an alternative text title and a short description."
    )


def test_missing_only_title_or_only_description_gives_one_finding_each() -> None:
    evaluation = audit_objects(
        anchor(pic(cnvpr("2", "No title", descr="A bar chart of sales"))),
        anchor(pic(cnvpr("3", "No descr", title="Sales chart"))),
    )
    assert summary(evaluation) == [
        ("ALT_TEXT", WS_A, f"{D1}#id=2", INPUT),
        ("ALT_TEXT", WS_A, f"{D1}#id=3", INPUT),
    ]
    assert "missing its alternative text title." in evaluation.findings[0].message
    assert "missing its alternative text description." in evaluation.findings[1].message


def test_complete_metadata_produces_no_finding() -> None:
    evaluation = audit_objects(
        anchor(pic(cnvpr("2", "Logo", title="Company logo", descr="Blue square logo"))),
        anchor(chart(cnvpr("3", "Chart", title="Sales", descr="Sales rose 10% in Q2"))),
        anchor(shape(cnvpr("4", "Box", title="Note", descr="Totals exclude tax"), "Visible")),
    )
    assert evaluation.findings == []
    assert evaluation.limitations == []


@pytest.mark.parametrize(
    "blank",
    [
        "",
        " ",
        "\t\n\r",
        "\u00a0",
        "\u1680",
        "\u2000\u200a",
        "\u2028\u2029",
        "\u202f\u205f",
        "\u3000",
        "\u0085",
    ],
)
def test_unicode_white_space_values_are_blank(blank: str) -> None:
    finding = only(audit_objects(anchor(pic(cnvpr("2", "P", title=blank, descr="Real text")))))
    assert finding.classification.value == INPUT
    assert "title." in finding.message


def test_xml_illegal_white_space_cannot_reach_the_rule() -> None:
    """U+000B/U+000C are White_Space but illegal in XML 1.0; such parts fail validation."""
    from app.contracts.errors import WorkbookValidationError

    with pytest.raises(WorkbookValidationError) as caught:
        audit_objects(anchor(pic(cnvpr("2", "P", title="\u000b\u000c", descr="Real text"))))
    assert caught.value.details == {"reason": "malformed_xml"}


@pytest.mark.parametrize("visible", ["\u200b", "\u2060", "\ufeff", "x", "\u00ad"])
def test_non_white_space_characters_count_as_present(visible: str) -> None:
    """U+200B/U+2060/U+FEFF are not Unicode White_Space, so the presence rule accepts them."""
    assert audit_objects(anchor(pic(cnvpr("2", "P", title=visible, descr=visible)))).findings == []


def test_visible_chart_title_and_shape_text_do_not_satisfy_alt_text() -> None:
    evaluation = audit_objects(
        anchor(chart(cnvpr("3", "Quarterly totals"))),  # chart part has visible title text
        anchor(shape(cnvpr("4", "Callout"), "Sales rose 10%", text_box=True)),
    )
    assert summary(evaluation) == [
        ("ALT_TEXT", WS_A, f"{D1}#id=3", INPUT),
        ("ALT_TEXT", WS_A, f"{D1}#id=4", INPUT),
    ]
    assert evaluation.findings[1].message.startswith('Text box "Callout"')


def test_object_name_does_not_satisfy_alt_text() -> None:
    finding = only(audit_objects(anchor(pic(cnvpr("2", "Revenue by region map")))))
    assert finding.classification.value == INPUT


# -- decorative ------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true"])
def test_decorative_without_text_is_one_manual_review(value: str) -> None:
    finding = only(audit_objects(anchor(pic(cnvpr("2", "Divider", decorative=value)))))
    assert (finding.target, finding.classification.value) == (f"{D1}#id=2", REVIEW)
    assert "marked decorative" in finding.message


def test_decorative_with_text_is_still_one_manual_review() -> None:
    finding = only(
        audit_objects(anchor(pic(cnvpr("2", "Divider", title="T", descr="D", decorative="1"))))
    )
    assert finding.classification.value == REVIEW


@pytest.mark.parametrize("value", ["0", "false"])
def test_explicitly_not_decorative_follows_missing_field_rule(value: str) -> None:
    finding = only(audit_objects(anchor(pic(cnvpr("2", "P", decorative=value)))))
    assert finding.classification.value == INPUT


@pytest.mark.parametrize("value,attr", [("yes", True), ("TRUE", True), ("", True), (None, False)])
def test_ambiguous_decorative_metadata_is_manual_review_with_limitation(
    value: str | None, attr: bool
) -> None:
    props = cnvpr("2", "P", title="T", descr="D", decorative=value or "x", decorative_attr=attr)
    if value is not None:
        props = props.replace('val="x"', f'val="{value}"')
    evaluation = audit_objects(anchor(pic(props)))
    finding = only(evaluation)
    assert finding.classification.value == REVIEW
    assert "unrecognized decorative metadata" in finding.message
    assert evaluation.limitations == [
        f"Decorative metadata for {D1}#id=2 has an unrecognized value; reviewed manually."
    ]


def test_decorative_on_group_does_not_apply_to_children() -> None:
    evaluation = audit_objects(
        anchor(group(cnvpr("10", "Group", decorative="1"), pic(cnvpr("11", "Child"))))
    )
    assert summary(evaluation) == [
        ("ALT_TEXT", WS_A, f"{D1}#id=10", REVIEW),
        ("ALT_TEXT", WS_A, f"{D1}#id=11", INPUT),
    ]


# -- groups, hidden sheets, multiple drawings, anchors ---------------------------------


def test_group_and_nested_children_are_inspected() -> None:
    evaluation = audit_objects(
        anchor(
            group(
                cnvpr("20", "Outer", title="Org chart", descr="Three teams"),
                shape(cnvpr("21", "Box A")),
                group(
                    cnvpr("22", "Inner", title="Sub", descr="Sub group"),
                    pic(cnvpr("23", "Photo", title="Photo", descr="Team photo")),
                    connector(cnvpr("24", "Link")),
                ),
            )
        )
    )
    assert summary(evaluation) == [
        ("ALT_TEXT", WS_A, f"{D1}#id=21", INPUT),
        ("ALT_TEXT", WS_A, f"{D1}#id=24", INPUT),
    ]


def test_group_missing_metadata_is_its_own_finding() -> None:
    evaluation = audit_objects(
        anchor(group(cnvpr("30", "Group 1"), pic(cnvpr("31", "C", title="t", descr="d"))))
    )
    assert summary(evaluation) == [("ALT_TEXT", WS_A, f"{D1}#id=30", INPUT)]
    assert evaluation.findings[0].message.startswith('Group "Group 1"')


@pytest.mark.parametrize("kind", ["twoCellAnchor", "oneCellAnchor", "absoluteAnchor"])
def test_all_anchor_kinds(kind: str) -> None:
    assert only(audit_objects(anchor(pic(cnvpr("5", "P")), kind))).target == f"{D1}#id=5"


def test_objects_on_hidden_sheets_are_inspected() -> None:
    assert only(audit_objects(anchor(pic(cnvpr("2", "P"))), hidden=True)).worksheet == WS_A


def test_multiple_drawings_and_same_id_in_different_parts() -> None:
    d2 = "xl/drawings/drawing2.xml"
    wb = Workbook(
        [
            Sheet(
                "Summary",
                "xl/worksheets/sheet1.xml",
                "1",
                drawing=Drawing(D1, drawing_xml(anchor(pic(cnvpr("2", "A"))))),
            ),
            Sheet(
                "Detail",
                "xl/worksheets/sheet2.xml",
                "2",
                state="veryHidden",
                drawing=Drawing(
                    d2,
                    drawing_xml(anchor(chart(cnvpr("2", "B")))),
                    chart_part="xl/charts/chart2.xml",
                ),
            ),
        ]
    )
    evaluation = audit_workbook(wb)
    assert summary(evaluation) == [
        ("ALT_TEXT", "Detail", f"{d2}#id=2", INPUT),
        ("ALT_TEXT", "Summary", f"{D1}#id=2", INPUT),
    ]
    assert evaluation.findings[0].findingId != evaluation.findings[1].findingId


def test_chartsheet_drawing_is_inspected() -> None:
    wb = Workbook(
        [
            Sheet("Data", "xl/worksheets/sheet1.xml", "1"),
            Sheet(
                "Trend",
                "xl/chartsheets/sheet1.xml",
                "2",
                kind="chartsheet",
                drawing=Drawing(
                    D1, drawing_xml(anchor(chart(cnvpr("1", "Chart 1")), "absoluteAnchor"))
                ),
            ),
        ]
    )
    assert summary(audit_workbook(wb)) == [("ALT_TEXT", "Trend", f"{D1}#id=1", INPUT)]


# -- identity fallbacks ----------------------------------------------------------------

P1 = "/wsDr[1]/twoCellAnchor[1]/pic[1]/nvPicPr[1]/cNvPr[1]"
P2 = "/wsDr[1]/twoCellAnchor[2]/pic[1]/nvPicPr[1]/cNvPr[1]"


def test_duplicate_ids_use_indexed_xml_paths_for_every_occurrence() -> None:
    evaluation = audit_objects(
        anchor(pic(cnvpr("4", "First"))),
        anchor(pic(cnvpr("4", "Second"))),
        anchor(pic(cnvpr("5", "Unique"))),
    )
    assert summary(evaluation) == [
        ("ALT_TEXT", WS_A, f"{D1}#id=5", INPUT),
        ("ALT_TEXT", WS_A, f"{D1}#xml={P1}", INPUT),
        ("ALT_TEXT", WS_A, f"{D1}#xml={P2}", INPUT),
    ]
    assert evaluation.limitations == [
        f"Drawing part {D1} has duplicated object IDs; affected objects use XML element path "
        "targets (#xml=)."
    ]


def test_duplicate_id_between_group_and_child_is_disambiguated() -> None:
    evaluation = audit_objects(anchor(group(cnvpr("8", "G"), shape(cnvpr("8", "S")))))
    assert [f.target for f in evaluation.findings] == [
        f"{D1}#xml=/wsDr[1]/twoCellAnchor[1]/grpSp[1]/nvGrpSpPr[1]/cNvPr[1]",
        f"{D1}#xml=/wsDr[1]/twoCellAnchor[1]/grpSp[1]/sp[1]/nvSpPr[1]/cNvPr[1]",
    ]


@pytest.mark.parametrize(
    "object_id,reason", [(None, "missing"), ("", "missing"), ("x7", "invalid")]
)
def test_missing_or_invalid_id_uses_xml_path(object_id: str | None, reason: str) -> None:
    evaluation = audit_objects(anchor(pic(cnvpr(object_id, "P"))))
    assert only(evaluation).target == f"{D1}#xml={P1}"
    assert evaluation.limitations == [
        f"Drawing part {D1} has {reason} object IDs; affected objects use XML element path "
        "targets (#xml=)."
    ]


# -- alternate content and unsupported representations ---------------------------------


def test_alternate_content_inspects_choice_not_fallback() -> None:
    choice = chart(cnvpr("40", "Slicer-like", title="Region filter", descr="Filters region"))
    fallback = shape(cnvpr("40", "Fallback rectangle"))  # would be flagged if inspected
    assert audit_objects(anchor(alternate(choice, fallback))).findings == []


def test_alternate_content_without_choice_uses_fallback() -> None:
    xml = drawing_xml(
        anchor(
            '<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
            f"<mc:Fallback>{pic(cnvpr('41', 'Fallback pic'))}</mc:Fallback></mc:AlternateContent>"
        )
    )
    evaluation = audit_workbook(
        Workbook([Sheet(WS_A, "xl/worksheets/sheet1.xml", "1", drawing=Drawing(D1, xml))])
    )
    assert only(evaluation).target == f"{D1}#id=41"


def test_content_part_is_disclosed_manual_review() -> None:
    ink = '<xdr:contentPart xmlns:r14="r14" r:id="rIdInk"/>'
    evaluation = audit_objects(anchor(ink))
    finding = only(evaluation)
    assert (finding.target, finding.classification.value) == (
        f"{D1}#xml=/wsDr[1]/twoCellAnchor[1]/contentPart[1]",
        REVIEW,
    )
    assert evaluation.limitations == [
        f"Ink or content part object in {D1}: alt text metadata is not inspected."
    ]


def test_object_without_cnvpr_is_manual_review_not_dropped() -> None:
    broken = "<xdr:pic><xdr:nvPicPr><xdr:cNvPicPr/></xdr:nvPicPr></xdr:pic>"
    finding = only(audit_objects(anchor(broken)))
    assert finding.target == f"{D1}#xml=/wsDr[1]/twoCellAnchor[1]/pic[1]"
    assert finding.classification.value == REVIEW


VML = (
    '<xml xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office"'
    ' xmlns:x="urn:schemas-microsoft-com:office:excel">'
    '<o:shapelayout v:ext="edit"><o:idmap v:ext="edit" data="1"/></o:shapelayout>'
    '<v:shapetype id="_x0000_t202" coordsize="21600,21600" o:spt="202"/>'
    '<v:shape id="_x0000_s1025" type="#_x0000_t202"><x:ClientData ObjectType="Note">'
    "<x:Row>0</x:Row><x:Column>0</x:Column></x:ClientData></v:shape>"
    '<v:shape id="_x0000_s1026" type="#_x0000_t75"><v:imagedata o:relid="rId1"/>'
    '<x:ClientData ObjectType="Pict"/></v:shape>'
    '<v:rect id="_x0000_s1027"/>'
    "</xml>"
)


def test_legacy_vml_objects_are_manual_review_and_notes_disclosed() -> None:
    vml_part = "xl/drawings/vmlDrawing1.vml"
    wb = Workbook([Sheet(WS_A, "xl/worksheets/sheet1.xml", "1", vml_part=vml_part, vml_xml=VML)])
    evaluation = audit_workbook(wb)
    assert summary(evaluation) == [
        ("ALT_TEXT", WS_A, f"{vml_part}#xml=/xml[1]/rect[1]", REVIEW),
        ("ALT_TEXT", WS_A, f"{vml_part}#xml=/xml[1]/shape[2]", REVIEW),
    ]
    assert any("1 cell comment note shape(s)" in item for item in evaluation.limitations)
    assert any("Legacy VML Pict object" in item for item in evaluation.limitations)


def test_unreadable_vml_is_one_part_level_manual_review() -> None:
    vml_part = "xl/drawings/vmlDrawing1.vml"
    wb = Workbook(
        [Sheet(WS_A, "xl/worksheets/sheet1.xml", "1", vml_part=vml_part, vml_xml="<xml><br></xml>")]
    )
    finding = only(audit_workbook(wb))
    assert (finding.target, finding.classification.value) == (vml_part, REVIEW)


def test_background_picture_is_manual_review() -> None:
    wb = Workbook([Sheet(WS_A, "xl/worksheets/sheet1.xml", "1", tail='<picture r:id="rIdBg"/>')])
    finding = only(audit_workbook(wb))
    assert finding.target == "xl/worksheets/sheet1.xml#xml=/worksheet[1]/picture[1]"
    assert finding.classification.value == REVIEW


def test_unresolved_drawing_reference_is_manual_review() -> None:
    wb = Workbook(
        [Sheet(WS_A, "xl/worksheets/sheet1.xml", "1", tail='<drawing r:id="rIdMissing"/>')]
    )
    finding = only(audit_workbook(wb))
    assert finding.target == "xl/worksheets/sheet1.xml#xml=/worksheet[1]/drawing[1]"
    assert finding.classification.value == REVIEW


def test_messages_are_bounded_for_huge_names() -> None:
    finding = only(audit_objects(anchor(pic(cnvpr("2", "N" * 5_000)))))
    assert len(finding.message) <= 500 and len(finding.recommendation) <= 500
