"""Dylan: ALT_TEXT inventory and policy. Expected targets are authored from the seeds."""

import hashlib

import pytest

from app.audit.engine import OoxmlWorkbookEngine
from app.contracts.models import AuditEvaluation, Finding, ProcessingLimits
from tests.unit.ooxml_builder import (
    REL,
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
    rels_name,
    rels_xml,
    shape,
)

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


def test_group_container_without_metadata_has_no_finding_of_its_own() -> None:
    """Pod contract 1.1 §7: group children are checked individually, not the container."""
    evaluation = audit_objects(
        anchor(group(cnvpr("30", "Group 1"), pic(cnvpr("31", "C", title="t", descr="d"))))
    )
    assert evaluation.findings == []
    evaluation = audit_objects(anchor(group(cnvpr("30", "Group 1"), pic(cnvpr("31", "C")))))
    assert summary(evaluation) == [("ALT_TEXT", WS_A, f"{D1}#id=31", INPUT)]


def test_decorative_group_needs_confirmation() -> None:
    evaluation = audit_objects(
        anchor(group(cnvpr("30", "G", decorative="1"), pic(cnvpr("31", "C"))))
    )
    assert summary(evaluation) == [
        ("ALT_TEXT", WS_A, f"{D1}#id=30", REVIEW),
        ("ALT_TEXT", WS_A, f"{D1}#id=31", INPUT),
    ]


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

P1 = "/wsDr[1]/twoCellAnchor[1]/pic[1]"
P2 = "/wsDr[1]/twoCellAnchor[2]/pic[1]"


def test_duplicate_ids_are_manual_review_at_indexed_object_paths() -> None:
    """Pod contract 1.1 §7: ambiguous IDs are MANUAL_REVIEW at the object's #xml= path."""
    evaluation = audit_objects(
        anchor(pic(cnvpr("4", "First"))),
        anchor(pic(cnvpr("4", "Second", title="t", descr="d"))),
        anchor(pic(cnvpr("5", "Unique"))),
    )
    assert summary(evaluation) == [
        ("ALT_TEXT", WS_A, f"{D1}#id=5", INPUT),
        ("ALT_TEXT", WS_A, f"{D1}#xml={P1}", REVIEW),
        ("ALT_TEXT", WS_A, f"{D1}#xml={P2}", REVIEW),
    ]
    assert any("missing or duplicate id" in item for item in evaluation.limitations)


def test_duplicate_id_between_group_and_child_is_disambiguated() -> None:
    evaluation = audit_objects(anchor(group(cnvpr("8", "G"), shape(cnvpr("8", "S")))))
    assert summary(evaluation) == [
        ("ALT_TEXT", WS_A, f"{D1}#xml=/wsDr[1]/twoCellAnchor[1]/grpSp[1]", REVIEW),
        ("ALT_TEXT", WS_A, f"{D1}#xml=/wsDr[1]/twoCellAnchor[1]/grpSp[1]/sp[1]", REVIEW),
    ]


@pytest.mark.parametrize("object_id", [None, ""])
def test_missing_id_is_manual_review_even_when_described(object_id: str | None) -> None:
    evaluation = audit_objects(anchor(pic(cnvpr(object_id, "P", title="t", descr="d"))))
    finding = only(evaluation)
    assert (finding.target, finding.classification.value) == (f"{D1}#xml={P1}", REVIEW)


def test_non_numeric_unique_id_is_still_a_stable_target() -> None:
    assert only(audit_objects(anchor(pic(cnvpr("x7", "P"))))).target == f"{D1}#id=x7"


# -- alternate content and unsupported representations ---------------------------------


def test_alternate_content_is_one_manual_review_at_its_element() -> None:
    """Pod contract 1.1 §7: alternative representations are ambiguous; neither is inspected."""
    choice = chart(cnvpr("40", "Slicer-like", title="Region filter", descr="Filters region"))
    fallback = shape(cnvpr("41", "Fallback rectangle"))
    finding = only(audit_objects(anchor(alternate(choice, fallback))))
    assert (finding.target, finding.classification.value) == (
        f"{D1}#xml=/wsDr[1]/twoCellAnchor[1]/AlternateContent[1]",
        REVIEW,
    )


def test_alternate_content_without_choice_is_still_manual_review() -> None:
    xml = drawing_xml(
        anchor(
            '<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
            f"<mc:Fallback>{pic(cnvpr('41', 'Fallback pic'))}</mc:Fallback></mc:AlternateContent>"
        )
    )
    evaluation = audit_workbook(
        Workbook([Sheet(WS_A, "xl/worksheets/sheet1.xml", "1", drawing=Drawing(D1, xml))])
    )
    finding = only(evaluation)
    assert finding.target == f"{D1}#xml=/wsDr[1]/twoCellAnchor[1]/AlternateContent[1]"
    assert finding.classification.value == REVIEW


def test_content_part_is_disclosed_manual_review() -> None:
    ink = '<xdr:contentPart xmlns:r14="r14" r:id="rIdInk"/>'
    evaluation = audit_objects(anchor(ink))
    finding = only(evaluation)
    assert (finding.target, finding.classification.value) == (
        f"{D1}#xml=/wsDr[1]/twoCellAnchor[1]/contentPart[1]",
        REVIEW,
    )
    assert evaluation.limitations == [
        f"Ink or content part object in {D1}: alt text is not inspected."
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


def test_every_legacy_vml_shape_is_manual_review_by_unique_id() -> None:
    """Pod contract 1.1 §7: legacy VML (comment notes included) is MANUAL_REVIEW, #id= targets."""
    vml_part = "xl/drawings/vmlDrawing1.vml"
    wb = Workbook([Sheet(WS_A, "xl/worksheets/sheet1.xml", "1", vml_part=vml_part, vml_xml=VML)])
    evaluation = audit_workbook(wb)
    assert summary(evaluation) == [
        ("ALT_TEXT", WS_A, f"{vml_part}#id=_x0000_s1025", REVIEW),
        ("ALT_TEXT", WS_A, f"{vml_part}#id=_x0000_s1026", REVIEW),
    ]


def test_vml_shapes_without_unique_ids_use_paths_and_empty_vml_reviews_the_reference() -> None:
    vml_part = "xl/drawings/vmlDrawing1.vml"
    shapes = VML.replace('id="_x0000_s1026"', 'id="_x0000_s1025"')
    wb = Workbook([Sheet(WS_A, "xl/worksheets/sheet1.xml", "1", vml_part=vml_part, vml_xml=shapes)])
    assert [f.target for f in audit_workbook(wb).findings] == [
        f"{vml_part}#xml=/xml[1]/shape[1]",
        f"{vml_part}#xml=/xml[1]/shape[2]",
    ]
    empty = '<xml xmlns:v="urn:schemas-microsoft-com:vml"><v:rect id="r1"/></xml>'
    wb = Workbook([Sheet(WS_A, "xl/worksheets/sheet1.xml", "1", vml_part=vml_part, vml_xml=empty)])
    assert (
        only(audit_workbook(wb)).target
        == "xl/worksheets/sheet1.xml#xml=/worksheet[1]/legacyDrawing[1]"
    )


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


# -- pod contract 1.1 §7 unsupported representations (aligned with Team 12) -------------


def test_non_chart_graphic_frame_is_manual_review_by_id() -> None:
    smart_art = (
        f"<xdr:graphicFrame><xdr:nvGraphicFramePr>{cnvpr('2', 'Diagram', 'T', 'D')}"
        "<xdr:cNvGraphicFramePr/></xdr:nvGraphicFramePr><xdr:xfrm/><a:graphic>"
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/diagram">'
        '<dgm:relIds xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" '
        'r:dm="rIdChart"/></a:graphicData></a:graphic></xdr:graphicFrame>'
    )
    finding = only(audit_objects(anchor(smart_art)))
    assert (finding.target, finding.classification.value) == (f"{D1}#id=2", REVIEW)


def test_object_with_external_content_is_manual_review_by_id() -> None:
    rels = rels_xml(
        f'<Relationship Id="rIdImg" Type="{REL}/image" Target="../media/image1.png"/>'
        f'<Relationship Id="rIdChart" Type="{REL}/chart" Target="../charts/chart1.xml"/>'
        f'<Relationship Id="rIdExt" Type="{REL}/image" Target="https://example.invalid/x.png" '
        'TargetMode="External"/>'
    )
    sheet = Sheet(
        WS_A,
        "xl/worksheets/sheet1.xml",
        "1",
        drawing=Drawing(D1, drawing_xml(anchor(pic(cnvpr("2", "P", "T", "D"), "rIdExt")))),
    )
    wb = Workbook([sheet], extra_parts={rels_name(D1): rels})
    finding = only(audit_workbook(wb))
    assert (finding.target, finding.classification.value) == (f"{D1}#id=2", REVIEW)


def test_form_controls_and_ole_objects_are_manual_review_wherever_nested() -> None:
    tail = (
        '<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
        '<mc:Choice Requires="x14"><controls><control shapeId="1" r:id="rIdC" name="Check 1"/>'
        "</controls></mc:Choice></mc:AlternateContent>"
        '<oleObjects><oleObject progId="Word.Document.12" shapeId="2" r:id="rIdO"/></oleObjects>'
    )
    wb = Workbook([Sheet(WS_A, "xl/worksheets/sheet1.xml", "1", tail=tail)])
    root = "xl/worksheets/sheet1.xml#xml=/worksheet[1]"
    assert summary(audit_workbook(wb)) == [
        ("ALT_TEXT", WS_A, f"{root}/AlternateContent[1]/Choice[1]/controls[1]/control[1]", REVIEW),
        ("ALT_TEXT", WS_A, f"{root}/oleObjects[1]/oleObject[1]", REVIEW),
    ]
