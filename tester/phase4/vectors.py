"""Team 9's provisional rule-vector fixture packs for the P4-RULE-* cases (§7.4-§7.7).

One worksheet per case, so each hosted case compares only its own worksheet's findings.
Cells that should be screened in isolation are separated by empty rows/columns. Several
vectors are deliberately discriminating: a wrong reading (theme index order, ignored tint,
ignored workbook palette, unsuppressed conditional range) changes the expected findings.
Expected findings are hand-written in tests/fixtures/expectations-phase4.json.
"""

from xml.sax.saxutils import escape

from tester.phase4.workbooks import (
    MAIN,
    XML_DECLARATION,
    Package,
    formula,
    number,
    relationships,
    rows,
    table,
    text,
    worksheet,
)

THEME_CT = "application/vnd.openxmlformats-officedocument.theme+xml"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _sheet_with_tables(
    package: Package, part: str, data: str, tables: list[tuple[str, str]]
) -> None:
    """Add a worksheet and its table parts; tables are (rId, table part name) pairs."""
    package.add(part, worksheet(data, tables=[rid for rid, _ in tables]))
    rels_name = part.replace("worksheets/", "worksheets/_rels/") + ".rels"
    package.add(
        rels_name,
        relationships([(rid, "table", f"../tables/{name}") for rid, name in tables]),
    )


def table_header_vectors() -> bytes:
    """Formal and unstructured table-header vectors. REMEDIATION_AVAILABLE.

    Declared (sheet1): table1 A1:B3 headerRowCount omitted, table2 D1:E3 headerRowCount 1,
      both with matching plain labels -> no finding.
    Blank (sheet2): table3 A1:B3, header B1 is whitespace-only -> table3 MANUAL_REVIEW.
    Duplicate (sheet3): table4 A1:B3, header cells "Total" and " total " (duplicate after trim
      and casefold; column names Total/Total2) -> table4 MANUAL_REVIEW.
    Formula (sheet4): table5 A1:B3, B1 is a formula with cached "Cost" -> table5 MANUAL_REVIEW.
    Sparse (sheet5): A1,B1,A2 (sparse A1:B2) -> MANUAL_REVIEW #range=A1:B2; dense D4:E5 with
      labels -> REQUIRES_USER_INPUT #range=D4:E5; C3 touches D4 only diagonally (not joined).
    Titles (sheet6): row A1:C1, column E1:E3, isolated G5 -> no finding, scope limitation.
    Hidden (sheet7, hidden): table6 A1:B3 headerRowCount 0 with matching first row
      -> table6 REQUIRES_USER_INPUT.
    """
    package = Package("table_header_vectors")
    for index, name in enumerate(
        ["Declared", "Blank", "Duplicate", "Formula", "Sparse", "Titles", "Hidden"], 1
    ):
        package.sheet(
            name,
            index,
            f"xl/worksheets/sheet{index}.xml",
            state="hidden" if name == "Hidden" else None,
        )

    def body(a: str, b: str, *, b_cell: str | None = None) -> str:
        return rows(
            text("A1", a),
            b_cell or text("B1", b),
            text("A2", "Pens"),
            number("B2", 4),
            text("A3", "Ink"),
            number("B3", 9),
        )

    declared = rows(
        text("A1", "Item"),
        text("B1", "Cost"),
        text("D1", "Region"),
        text("E1", "Units"),
        text("A2", "Pens"),
        number("B2", 4),
        text("D2", "North"),
        number("E2", 12),
        text("A3", "Ink"),
        number("B3", 9),
        text("D3", "South"),
        number("E3", 4),
    )
    _sheet_with_tables(
        package,
        "xl/worksheets/sheet1.xml",
        declared,
        [("rId1", "table1.xml"), ("rId2", "table2.xml")],
    )
    package.add("xl/tables/table1.xml", table(1, "Declared1", "A1:B3", ["Item", "Cost"]), "table")
    package.add(
        "xl/tables/table2.xml",
        table(2, "Declared2", "D1:E3", ["Region", "Units"], header_rows=1),
        "table",
    )
    _sheet_with_tables(
        package, "xl/worksheets/sheet2.xml", body("Item", " "), [("rId1", "table3.xml")]
    )
    package.add("xl/tables/table3.xml", table(3, "Blank3", "A1:B3", ["Item", "Column2"]), "table")
    _sheet_with_tables(
        package, "xl/worksheets/sheet3.xml", body("Total", " total "), [("rId1", "table4.xml")]
    )
    package.add(
        "xl/tables/table4.xml", table(4, "Duplicate4", "A1:B3", ["Total", "Total2"]), "table"
    )
    _sheet_with_tables(
        package,
        "xl/worksheets/sheet4.xml",
        body("Item", "", b_cell=formula("B1", '"Cost"', "Cost", string=True)),
        [("rId1", "table5.xml")],
    )
    package.add("xl/tables/table5.xml", table(5, "Formula5", "A1:B3", ["Item", "Cost"]), "table")
    sparse = rows(
        text("A1", "Name"),
        text("B1", "Score"),
        text("A2", "Ada"),
        text("C3", "note"),
        text("D4", "Item"),
        text("E4", "Qty"),
        text("D5", "Pens"),
        number("E5", 3),
    )
    package.add("xl/worksheets/sheet5.xml", worksheet(sparse))
    titles = rows(
        text("A1", "Q1"),
        text("B1", "Q2"),
        text("C1", "Q3"),
        text("E1", "Jan"),
        text("E2", "Feb"),
        text("E3", "Mar"),
        text("G5", "Draft"),
    )
    package.add("xl/worksheets/sheet6.xml", worksheet(titles))
    _sheet_with_tables(
        package, "xl/worksheets/sheet7.xml", body("Item", "Cost"), [("rId1", "table6.xml")]
    )
    package.add(
        "xl/tables/table6.xml",
        table(6, "Hidden6", "A1:B3", ["Item", "Cost"], header_rows=0),
        "table",
    )
    return package.build()


def merge_vectors() -> bytes:
    """Merged-cell vectors. REVIEW_REQUIRED.

    Populated (sheet1): A1:C1 title merge with text; E3:F4 with a value only in F4 (inside,
      not the anchor); H1:H2 holding numeric zero -> three MERGED_CELLS MANUAL_REVIEW.
    Empty (sheet2): A1:B2 with no cells; D1:E1 whose only cell is formatting-only -> none.
    """
    package = Package("merge_vectors")
    package.sheet("Populated", 1, "xl/worksheets/sheet1.xml")
    package.sheet("Empty", 2, "xl/worksheets/sheet2.xml")
    populated = rows(text("A1", "Annual report"), number("H1", 0), text("F4", "Note"))
    package.add(
        "xl/worksheets/sheet1.xml", worksheet(populated, merges=["A1:C1", "E3:F4", "H1:H2"])
    )
    package.add(
        "xl/worksheets/sheet2.xml", worksheet(rows('<c r="D1" s="3"/>'), merges=["A1:B2", "D1:E1"])
    )
    return package.build()


def _link_rels(targets: list[str]) -> str:
    return relationships(
        [(f"rId{i}", "hyperlink", target) for i, target in enumerate(targets, 1)],
        external=[f"rId{i}" for i in range(1, len(targets) + 1)],
    )


def link_vectors() -> bytes:
    """Hyperlink label vectors. REMEDIATION_AVAILABLE.

    Generic (sheet1), simple cell links: A1 " click NBSP HERE! " (trigger), A3 "Read more."
      (trigger), A5 "Learn  more" (collapsed, trigger), A7 "More!!" (two marks, no trigger),
      A9 "Website" (trigger), A11 "Annual accessibility report" (no trigger).
    Raw (sheet2): A1 "https://example.edu/a", A3 "www.example.edu",
      A5 "mailto:staff@example.edu" (prefix triggers); A7 "Raw!A1" with location "Raw!A1"
      (label equals destination, trigger); A9 "Accessibility report (PDF)" (no trigger).
    Display (sheet3): A1 text "here" with display "Annual report" (trigger: cell text wins);
      A3 text "Annual report" with display "here" (no trigger).
    FormulaLiteral (sheet4), never modified so MANUAL_REVIEW at the indexed c element:
      A1 =HYPERLINK("https://example.edu","click here") (row[1]/c[1], trigger);
      A3 =HYPERLINK("https://example.edu/report") one argument, label is the URL (row[2]/c[1]);
      A5 =HYPERLINK("https://example.edu","Annual report") (no trigger);
      A7 =_xlfn.HYPERLINK("https://example.edu","here") (row[4]/c[1], trigger).
    FormulaDynamic (sheet5): A1 =HYPERLINK(B1,"Report") computed argument (row[1]/c[1]);
      A3 =HYPERLINK("https://example.edu";"here") semicolon syntax (row[2]/c[1]); both
      MANUAL_REVIEW with a limitation.
    RangeDup (sheet6): hyperlink[1] ref A1:B1 (range link); hyperlink[2] and hyperlink[3] both
      ref A3 (duplicate declarations) -> three MANUAL_REVIEW at the hyperlink element paths.
    """
    package = Package("link_vectors")
    names = ["Generic", "Raw", "Display", "FormulaLiteral", "FormulaDynamic", "RangeDup"]
    for index, name in enumerate(names, 1):
        package.sheet(name, index, f"xl/worksheets/sheet{index}.xml")
    generic = [
        " click HERE! ",
        "Read more.",
        "Learn  more",
        "More!!",
        "Website",
        "Annual accessibility report",
    ]
    refs = [f"A{1 + 2 * i}" for i in range(len(generic))]
    package.add(
        "xl/worksheets/sheet1.xml",
        worksheet(
            rows(*(text(ref, label) for ref, label in zip(refs, generic, strict=True))),
            hyperlinks="".join(
                f'<hyperlink ref="{ref}" r:id="rId{i}"/>' for i, ref in enumerate(refs, 1)
            ),
        ),
    )
    package.add(
        "xl/worksheets/_rels/sheet1.xml.rels",
        _link_rels([f"https://example.edu/g{i}" for i in range(1, 7)]),
    )
    raw = rows(
        text("A1", "https://example.edu/a"),
        text("A3", "www.example.edu"),
        text("A5", "mailto:staff@example.edu"),
        text("A7", "Raw!A1"),
        text("A9", "Accessibility report (PDF)"),
    )
    package.add(
        "xl/worksheets/sheet2.xml",
        worksheet(
            raw,
            hyperlinks=(
                '<hyperlink ref="A1" r:id="rId1"/><hyperlink ref="A3" r:id="rId2"/>'
                '<hyperlink ref="A5" r:id="rId3"/><hyperlink ref="A7" location="Raw!A1"/>'
                '<hyperlink ref="A9" r:id="rId4"/>'
            ),
        ),
    )
    package.add(
        "xl/worksheets/_rels/sheet2.xml.rels",
        _link_rels(
            [
                "https://example.edu/a",
                "https://www.example.edu/",
                "mailto:staff@example.edu",
                "https://example.edu/report.pdf",
            ]
        ),
    )
    package.add(
        "xl/worksheets/sheet3.xml",
        worksheet(
            rows(text("A1", "here"), text("A3", "Annual report")),
            hyperlinks=(
                '<hyperlink ref="A1" r:id="rId1" display="Annual report"/>'
                '<hyperlink ref="A3" r:id="rId2" display="here"/>'
            ),
        ),
    )
    package.add(
        "xl/worksheets/_rels/sheet3.xml.rels",
        _link_rels(["https://example.edu/annual", "https://example.edu/annual2"]),
    )
    literal = rows(
        formula("A1", 'HYPERLINK("https://example.edu","click here")', "click here", string=True),
        formula(
            "A3",
            'HYPERLINK("https://example.edu/report")',
            "https://example.edu/report",
            string=True,
        ),
        formula(
            "A5", 'HYPERLINK("https://example.edu","Annual report")', "Annual report", string=True
        ),
        formula("A7", '_xlfn.HYPERLINK("https://example.edu","here")', "here", string=True),
    )
    package.add("xl/worksheets/sheet4.xml", worksheet(literal))
    dynamic = rows(
        formula("A1", 'HYPERLINK(B1,"Report")', "Report", string=True),
        text("B1", "https://example.edu/report"),
        formula("A3", 'HYPERLINK("https://example.edu";"here")', "here", string=True),
    )
    package.add("xl/worksheets/sheet5.xml", worksheet(dynamic))
    package.add(
        "xl/worksheets/sheet6.xml",
        worksheet(
            rows(text("A1", "Report"), text("B1", "Data"), text("A3", "Policy")),
            hyperlinks=(
                '<hyperlink ref="A1:B1" r:id="rId1"/><hyperlink ref="A3" r:id="rId2"/>'
                '<hyperlink ref="A3" r:id="rId3"/>'
            ),
        ),
    )
    package.add(
        "xl/worksheets/_rels/sheet6.xml.rels",
        _link_rels(["https://example.edu/r", "https://example.edu/p1", "https://example.edu/p2"]),
    )
    return package.build()


# Fonts and fills for the contrast vectors. Index comments are referenced by cellXfs below.
CONTRAST_FONTS = [
    '<font><sz val="11"/><name val="Calibri"/></font>',  # 0 default (no color)
    '<font><sz val="11"/><color rgb="FF767676"/><name val="Calibri"/></font>',  # 1
    '<font><sz val="11"/><color rgb="FF777777"/><name val="Calibri"/></font>',  # 2
    '<font><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>',  # 3 white
    '<font><sz val="18"/><color rgb="FF777777"/><name val="Calibri"/></font>',  # 4
    '<font><b/><sz val="14"/><color rgb="FF777777"/><name val="Calibri"/></font>',  # 5
    '<font><sz val="18"/><color rgb="FF959595"/><name val="Calibri"/></font>',  # 6
    '<font><sz val="18"/><color rgb="FF949494"/><name val="Calibri"/></font>',  # 7
    '<font><sz val="14"/><color rgb="FF777777"/><name val="Calibri"/></font>',  # 8
    '<font><sz val="11"/><color theme="1"/><name val="Calibri"/></font>',  # 9 dk1
    '<font><sz val="11"/><color theme="1" tint="0.5"/><name val="Calibri"/></font>',  # 10
    '<font><sz val="11"/><color theme="4"/><name val="Calibri"/></font>',  # 11 accent1
    '<font><sz val="11"/><color indexed="10"/><name val="Calibri"/></font>',  # 12
    '<font><sz val="11"/><color indexed="8"/><name val="Calibri"/></font>',  # 13
    '<font><sz val="11"/><color rgb="FF0000FF"/><name val="Calibri"/></font>',  # 14 blue
    '<font><sz val="11"/><color auto="1"/><name val="Calibri"/></font>',  # 15 automatic
    '<font><sz val="11"/><color rgb="FFFF0000"/><name val="Calibri"/></font>',  # 16 red
]
CONTRAST_FILLS = [
    '<fill><patternFill patternType="none"/></fill>',  # 0
    '<fill><patternFill patternType="gray125"/></fill>',  # 1
    '<fill><patternFill patternType="solid"><fgColor rgb="FFF2F2F2"/></patternFill></fill>',  # 2
    '<fill><patternFill patternType="solid"><fgColor rgb="FFFFFF00"/></patternFill></fill>',  # 3
    '<fill><gradientFill degree="90"><stop position="0"><color rgb="FFFFFFFF"/></stop>'
    '<stop position="1"><color rgb="FF000000"/></stop></gradientFill></fill>',  # 4 gradient
    '<fill><patternFill patternType="darkGray"><fgColor rgb="FF000000"/>'
    '<bgColor rgb="FFFFFFFF"/></patternFill></fill>',  # 5 pattern
    '<fill><patternFill patternType="solid"><fgColor rgb="FFFFFFFF"/></patternFill></fill>',  # 6
]
# cellXfs index -> (fontId, fillId)
CONTRAST_XFS = [
    (0, 0),  # 0 default
    (1, 0),  # 1 767676
    (2, 0),  # 2 777777
    (3, 6),  # 3 white on solid white
    (4, 0),  # 4 777777 18pt
    (5, 0),  # 5 777777 14pt bold
    (6, 0),  # 6 959595 18pt
    (7, 0),  # 7 949494 18pt
    (8, 0),  # 8 777777 14pt not bold
    (9, 0),  # 9 theme dk1
    (10, 0),  # 10 theme dk1 tint 0.5
    (11, 0),  # 11 theme accent1
    (12, 0),  # 12 indexed 10 (overridden palette)
    (13, 0),  # 13 indexed 8
    (14, 0),  # 14 blue text
    (0, 3),  # 15 black on yellow fill
    (0, 4),  # 16 gradient fill
    (0, 5),  # 17 pattern fill
    (15, 0),  # 18 automatic font color
    (16, 0),  # 19 red text
    (0, 2),  # 20 black on F2F2F2
]
STANDARD_PALETTE_HEAD = [
    "000000",
    "FFFFFF",
    "FF0000",
    "00FF00",
    "0000FF",
    "FFFF00",
    "FF00FF",
    "00FFFF",
]


def _contrast_styles(palette: list[str]) -> str:
    fonts = "".join(CONTRAST_FONTS)
    fills = "".join(CONTRAST_FILLS)
    xfs = "".join(
        f'<xf numFmtId="0" fontId="{font}" fillId="{fill}" borderId="0" xfId="0"'
        + (' applyFont="1"' if font else "")
        + (' applyFill="1"' if fill else "")
        + "/>"
        for font, fill in CONTRAST_XFS
    )
    colors = "".join(f'<rgbColor rgb="FF{rgb}"/>' for rgb in palette)
    return (
        XML_DECLARATION + f'<styleSheet xmlns="{MAIN}">'
        f'<fonts count="{len(CONTRAST_FONTS)}">{fonts}</fonts>'
        f'<fills count="{len(CONTRAST_FILLS)}">{fills}</fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
        f'</cellStyleXfs><cellXfs count="{len(CONTRAST_XFS)}">{xfs}</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '<dxfs count="2"><dxf><font><b/></font></dxf>'
        '<dxf><fill><patternFill><bgColor rgb="FFFFC7CE"/></patternFill></fill></dxf></dxfs>'
        f"<colors><indexedColors>{colors}</indexedColors></colors>"
        "</styleSheet>"
    )


THEME = (
    XML_DECLARATION + f'<a:theme xmlns:a="{A}" name="Team9 vectors"><a:themeElements>'
    '<a:clrScheme name="Team9"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
    '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
    '<a:dk2><a:srgbClr val="1F497D"/></a:dk2><a:lt2><a:srgbClr val="EEECE1"/></a:lt2>'
    '<a:accent1><a:srgbClr val="336699"/></a:accent1><a:accent2><a:srgbClr val="C0504D"/>'
    '</a:accent2><a:accent3><a:srgbClr val="9BBB59"/></a:accent3><a:accent4>'
    '<a:srgbClr val="8064A2"/></a:accent4><a:accent5><a:srgbClr val="4BACC6"/></a:accent5>'
    '<a:accent6><a:srgbClr val="F79646"/></a:accent6><a:hlink><a:srgbClr val="0000FF"/>'
    '</a:hlink><a:folHlink><a:srgbClr val="800080"/></a:folHlink></a:clrScheme>'
    '<a:fontScheme name="Team9"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/>'
    '<a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Calibri"/>'
    '<a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>'
    '<a:fmtScheme name="Team9"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/>'
    '</a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill>'
    '<a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst><a:lnStyleLst><a:ln w="9525">'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="25400"><a:solidFill>'
    '<a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="38100"><a:solidFill>'
    '<a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst><a:effectStyleLst>'
    "<a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle>"
    "<a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst>"
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/>'
    '</a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst>'
    "</a:fmtScheme></a:themeElements></a:theme>"
)


def contrast_vectors(default_palette: list[str]) -> bytes:
    """Color and contrast vectors. REVIEW_REQUIRED (every finding is MANUAL_REVIEW).

    Default (sheet1): A1 default black on white, A3 767676 (4.54:1), A5 black on F2F2F2 -> none.
    Low (sheet2): A1 777777 (4.48:1) and A3 white on solid white (1:1) -> two findings.
    Large (sheet3): A1 777777 18pt and A3 777777 14pt bold pass 3.0 -> none; A5 959595 18pt
      (2.995:1) and A7 777777 14pt not bold (normal threshold) -> two findings.
    Boundary (sheet4): A1 767676 normal (pass), A3 777777 normal (fail), A5 949494 18pt (3.03,
      pass), A7 959595 18pt (2.995, fail) -> A3 and A7.
    Theme (sheet5): theme index 1 is dk1 (black; none at A1); dk1 tint 0.5 is 808080 (3.95:1)
      -> A3; index 4 is accent1 336699, nonneutral -> A5.
    Indexed (sheet6): the workbook palette overrides index 10 to black, so A1 (indexed 10) and
      A3 (indexed 8) are black -> none. Ignoring the override would make A1 red.
    Meaning (sheet7): A1 blue text (passes contrast, nonneutral) and A3 black on yellow -> two.
    Conditional (sheet8): fill dxf on A1:A3 -> #conditional=A1:A3 (static red A1 suppressed);
      color scale on "C1:C2 E1" -> #conditional=C1:C2 and #conditional=E1:E1; bold-only dxf
      on G1 -> none.
    Unsupported (sheet9): A1 gradient fill, A3 mixed-color rich text, A5 darkGray pattern fill,
      A7 automatic font color without RGB -> four findings with limitations.
    Hidden (sheet10, hidden): A1 red text -> one finding.
    HiddenRow (sheet11): row 3 hidden; A3 red text -> one finding.
    """
    palette = list(default_palette)
    palette[10] = "000000"
    package = Package("contrast_vectors")
    package.add("xl/styles.xml", _contrast_styles(palette), "styles")
    package.add("xl/theme/theme1.xml", THEME)
    package.overrides["/xl/theme/theme1.xml"] = THEME_CT
    names = [
        "Default",
        "Low",
        "Large",
        "Boundary",
        "Theme",
        "Indexed",
        "Meaning",
        "Conditional",
        "Unsupported",
        "Hidden",
        "HiddenRow",
    ]
    for index, name in enumerate(names, 1):
        package.sheet(
            name,
            index,
            f"xl/worksheets/sheet{index}.xml",
            state="hidden" if name == "Hidden" else None,
        )

    def sheet(index: int, data: str, **kwargs: str) -> None:
        package.add(f"xl/worksheets/sheet{index}.xml", worksheet(data, **kwargs))

    sheet(1, rows(text("A1", "Plain"), text("A3", "Gray", 1), text("A5", "Shaded", 20)))
    sheet(2, rows(text("A1", "Faint", 2), text("A3", "Invisible", 3)))
    sheet(
        3,
        rows(
            text("A1", "Large", 4),
            text("A3", "Bold", 5),
            text("A5", "Pale", 6),
            text("A7", "Small", 8),
        ),
    )
    sheet(
        4,
        rows(
            text("A1", "Pass", 1),
            text("A3", "Fail", 2),
            text("A5", "Large pass", 7),
            text("A7", "Large fail", 6),
        ),
    )
    sheet(5, rows(text("A1", "Dark one", 9), text("A3", "Tinted", 10), text("A5", "Accent", 11)))
    sheet(6, rows(text("A1", "Palette ten", 12), text("A3", "Palette eight", 13)))
    sheet(7, rows(text("A1", "Blue", 14), text("A3", "Highlighted", 15)))
    conditional = (
        '<conditionalFormatting sqref="A1:A3"><cfRule type="cellIs" dxfId="1" priority="1" '
        'operator="greaterThan"><formula>1</formula></cfRule></conditionalFormatting>'
        '<conditionalFormatting sqref="C1:C2 E1"><cfRule type="colorScale" priority="2">'
        '<colorScale><cfvo type="min"/><cfvo type="max"/><color rgb="FFF8696B"/>'
        '<color rgb="FF63BE7B"/></colorScale></cfRule></conditionalFormatting>'
        '<conditionalFormatting sqref="G1"><cfRule type="cellIs" dxfId="0" priority="3" '
        'operator="greaterThan"><formula>1</formula></cfRule></conditionalFormatting>'
    )
    sheet(
        8,
        rows(
            number("A1", 1, 19),
            number("A2", 2),
            number("A3", 3),
            number("C1", 4),
            number("C2", 5),
            number("E1", 6),
            number("G1", 7),
        ),
        conditional=conditional,
    )
    rich = (
        '<c r="A3" t="inlineStr"><is><r><rPr><color rgb="FFFF0000"/><sz val="11"/>'
        '<rFont val="Calibri"/></rPr><t>Red</t></r><r><rPr><sz val="11"/><rFont val="Calibri"/>'
        f'</rPr><t xml:space="preserve">{escape(" and plain")}</t></r></is></c>'
    )
    sheet(
        9,
        rows(
            text("A1", "Gradient", 16), rich, text("A5", "Pattern", 17), text("A7", "Automatic", 18)
        ),
    )
    sheet(10, rows(text("A1", "Hidden red", 19)))
    package.add(
        "xl/worksheets/sheet11.xml",
        worksheet('<row r="3" hidden="1">' + text("A3", "Hidden row red", 19) + "</row>"),
    )
    package.workbook_relationships.append(("theme", "theme/theme1.xml"))
    return package.build()


def vector_fixtures(default_palette: list[str]) -> dict[str, bytes]:
    return {
        "table_header_vectors.xlsx": table_header_vectors(),
        "merge_vectors.xlsx": merge_vectors(),
        "link_vectors.xlsx": link_vectors(),
        "contrast_vectors.xlsx": contrast_vectors(default_palette),
    }
