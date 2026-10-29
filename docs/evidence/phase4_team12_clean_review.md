# Phase 4 review of Team 12's `clean.xlsx`

Received from Logan Hanson (Team 12) on 2026-10-29 as "the clean workbook from team 12".
Reviewed by Dixon (Team 9) against specification 2.1.0 / ruleset 2.0.0 (`docs/contracts/phase4/`).
This is one reviewer's hand review from the package XML. It is **not** the two-member
cross-team approval required by §15.2, and it was not produced by running any audit engine.

| Field | Value |
|---|---|
| Stored at | `tests/fixtures/phase4/received/team12/clean.xlsx` (bytes unchanged) |
| Raw SHA256 | `20feed4783615c2ef5f5579a53b25524fc75a7316c108e1fc25bf1b5278aeca2` |
| Size | 57,564 bytes, 14 package entries |
| Worksheets | `Budget` (sheetId 1, `xl/worksheets/sheet1.xml`), `Notes` (sheetId 2, `xl/worksheets/sheet2.xml`) |
| Expectations file | None received (no hash record, expected findings or limitations) |
| Adopted into a suite | **No.** Kept separate under `received/`, as §15.2 requires |

## Result

Under ruleset 2.0.0 this workbook does not meet the §15.2 definition of clean ("no triggered
findings under all six rules"). The independent reading below expects **six findings** and
completion status `REMEDIATION_AVAILABLE`, not `AUDIT_COMPLETE`.

| Rule | Clean? | Reason |
|---|---|---|
| ALT_TEXT | Yes | Picture id 2, shape id 3 and chart frame id 4 each have nonblank `title` and `descr` |
| WORKSHEET_NAME | Yes | `Budget` and `Notes` are not `Sheet<digits>` names |
| TABLE_HEADER | **No** | `Budget!A1:B5` is a dense 5×2 component with distinct plain-text first-row labels and no formal table (§7.4) |
| MERGED_CELLS | Yes | No `mergeCells` |
| LINK_TEXT | Yes | No hyperlink elements or `HYPERLINK` formulas |
| COLOR_CONTRAST | **No** | Five populated cells use a nonneutral solid fill (§7.7 color-meaning screen), even though their contrast passes |

Contrast is not the problem. White on `#1F4E78` is 8.68:1 and black on `#DDEBF7` is 17.3:1.
§7.7 still emits a `MANUAL_REVIEW` color-meaning finding for any nonneutral effective font or
fill, meaning one whose RGB channels are not all equal.

### Expected tuples (independently computed finding IDs)

Sorted by `(ruleId, worksheet, target)`. IDs use the source SHA256 above, ruleset `2.0.0`, and
`tester/suite/identity.py` (checked against the pack's identity vectors).

| ruleId | worksheet | target | classification | findingId |
|---|---|---|---|---|
| COLOR_CONTRAST | Budget | `xl/worksheets/sheet1.xml#cell=A1` | MANUAL_REVIEW | `5449e0121c4349ba51e0391e9f2fd90b26a7a82b642e7e8222d027643c36c454` |
| COLOR_CONTRAST | Budget | `xl/worksheets/sheet1.xml#cell=A5` | MANUAL_REVIEW | `6375b1207192a98521c04610c086bf9cf492de5e7436839054afeb2090709d34` |
| COLOR_CONTRAST | Budget | `xl/worksheets/sheet1.xml#cell=B1` | MANUAL_REVIEW | `d1d45381e70ede3e62286c8048908922a35e73c3700900ae516ece73637cea79` |
| COLOR_CONTRAST | Budget | `xl/worksheets/sheet1.xml#cell=B5` | MANUAL_REVIEW | `6d4a36bd516ad3f93e81c49d87daabaf5f0fbb12f8bd6240a8fc570b234428db` |
| COLOR_CONTRAST | Notes | `xl/worksheets/sheet2.xml#cell=A1` | MANUAL_REVIEW | `2f073754b48546c2209ad57fe46e94bfd98fcf1343d08f3672ba9436305425a3` |
| TABLE_HEADER | Budget | `xl/worksheets/sheet1.xml#range=A1:B5` | REQUIRES_USER_INPUT | `2a16f9be70f327b658d5d9dfe6c25b52b191655d501decf360e98cdf374dbc33` |

Expected limitation facts: `Notes!A1:A2` is a one-column populated component, which needs the
"not an inferred table" scope disclosure. Cells without a fill use the missing-fill-means-white
display assumption.

## Suggested fix for Team 12

1. Use neutral styling (black, white or gray, with equal RGB channels) for header and total rows.
2. Make `Budget!A1:B5` a formal table (`headerRowCount` 1, `tableColumns` named `Item` and `Amount`).
   Alternatively, remove the dense two-dimensional block.

Re-send the bytes with an expectations record (§15.2 fields) so two Team 9 members can review it.

## Separate note from the same email

Logan stated that Team 12's tester expects `P3-SESSION-DUPLICATE` to return 409 with exactly
`message` and `source`. That agrees with §14 and the `TestEndpointError` schema
(`additionalProperties: false`). Team 9's tester already validates that strict two-field shape.
