# Suite manifest and fixtures

`manifest.json` is the tester's planned Phase 3 suite: fixed test order, the C1-C6 component
catalog, the eight Phase 3 AC portions, and the components/ACs each test's qualifying assertion
counts toward. `FIXTURE_MANIFEST_PATH` points here. `expectations.json` holds raw SHA256, ruleset,
expected targets/classifications and rejection status for every fixture.

## Inventory

| Key | File | Origin | Expectation |
|---|---|---|---|
| clean | `synthetic/clean.xlsx` | Synthetic (Dixon) | 202, `AUDIT_COMPLETE`, no findings |
| seeded_issues | `synthetic/seeded_issues.xlsx` | Synthetic (Dixon) | 202, `REVIEW_REQUIRED`, six findings |
| corrupt | `synthetic/corrupt.xlsx` | Synthetic (Dixon) | 422, project envelope, no IDs |
| unicode | `synthetic/unicode.xlsx` | Synthetic | Full legal XML White_Space sequence, leading-zero/trimmed sheet name, zero-width non-whitespace exclusion |
| repeated_ids | `synthetic/repeated_ids.xlsx` | Synthetic | Two duplicate IDs use distinct indexed XML targets; limitation disclosed |
| multiple_drawings | `synthetic/multiple_drawings.xlsx` | Synthetic | Visible picture and hidden-sheet chart both audited |
| zip_traversal / zip_duplicate | Matching synthetic files | Synthetic | Unsafe ZIP paths / duplicate entries → 422 |
| xml_dtd / macro_parts | Matching synthetic files | Synthetic | Entity/DTD declaration / macro part in .xlsx → 422 |
| protected_sheet / protected_structure | Matching synthetic files | Synthetic | Worksheet-only and workbook-structure-only protection rejected separately |
| encrypted_zip | `synthetic/encrypted_zip.xlsx` | Synthetic | Genuine ZipCrypto-encrypted OOXML ZIP → 422; fixed fixture-only password |
| in_cell_picture | `synthetic/in_cell_picture.xlsx` | Synthetic (pod contract 1.1) | `vm` cell → sheetData `#xml=` review; each `xl/richData/*` part and `xl/cellimages.xml` → `__WORKBOOK__` `<part>#xml=/` review |
| image_formula | `synthetic/image_formula.xlsx` | Synthetic (pod contract 1.1) | `_xlfn.IMAGE()` cell → sheetData `#xml=` review; a SUM cell is not |
| form_controls | `synthetic/form_controls.xlsx` | Synthetic (pod contract 1.1) | Control and OLE objects nested in `mc:AlternateContent` → sheet `#xml=` reviews; drawing `mc:AlternateContent` at root and inside an anchor → one review each; undescribed group container → no finding |
| vml_comments | `synthetic/vml_comments.xlsx` | Synthetic (pod contract 1.1) | Two comment-note VML shapes → `vmlDrawing1.vml#id=<id>` reviews; shapetype ignored |
| inactive_protection | `synthetic/inactive_protection.xlsx` | Synthetic (pod contract 1.1) | Stored sheet password with `sheet="0"` → accepted, no findings |

Generated at run time by `tester/suite/workbooks.py`, not committed: the 25,000,000 / 25,000,001-byte
uploads (P3-INPUT-BYTES-AT / -OVER), 50 / 51 worksheets (P3-INPUT-SHEETS-AT / -OVER), 250,000 /
250,001 nonempty cells (P3-INPUT-CELLS-AT / -OVER), 50 worksheets plus one chartsheet
(P3-INPUT-SHEETS-CHARTSHEET) and a password-protected structure
(P3-INPUT-PROTECTED). `manifest.json` also holds the 14-operation catalog used by run records.

All eighteen committed files are **provisional synthetic fixtures**; the original three are
stand-ins. Team 12's shared `clean.xlsx`,
`seeded_issues.xlsx` and `corrupt.xlsx` have not been delivered. When they arrive, add them under
`shared/` with `"origin": "team12"` entries and their own independently reviewed expectations; do
not overwrite the synthetic files.

## How expectations were authored

`tester/suite/workbooks.py` writes each worksheet, drawing object ID, title/description field and
decorative marker explicitly. Expectations were written by hand from those definitions and the
audit policy in `docs/contracts/phase3.md`. They were never derived by running the system's engine.
The tester computes expected `findingId` values with its own implementation of the contract hash
(`tester/suite/identity.py`), checked against the golden vector.

Regenerate or verify the committed bytes (stored ZIP entries, so output is identical on every
platform):

```sh
uv run python -m scripts.make_synthetic_fixtures --check
```

If a fixture changes, update its SHA256 in `expectations.json` and re-review its expected findings.

## Review status

Manual expected-result review by two pod members: **pending, not performed**. Opening the synthetic
files in Excel without repair warnings: **not yet checked**. The original valid fixtures were
previously loaded with openpyxl; that does not establish Excel preservation. ZIP/XML attacks,
encrypted ZIP packages, Unicode whitespace variants, repeated
IDs and multiple drawings now have committed fixtures. Controlled faults are exercised
through local fake responses in `tests/unit/test_tester.py`; they do not prove a real system
handles injected storage/engine failures. Office CFB/Agile encryption, malformed decorative
metadata and legacy unsupported representations still need additional independently reviewed
fixtures. The encrypted ZIP test is not evidence of coverage for Office CFB encryption.


The manifest is snapshotted at setup. Unknown/mismatched resources and repeated audits each
have their own stable result IDs. Later evaluable cases still execute after an earlier
failure. `P3-AUDIT-FAILURE-ENVELOPE` is an observation case: it skips on a healthy target and
asserts AC-14 only when a real `PROCESSING_FAILED` audit was observed. Ordinary corrupt-file
rejection exercises AC-02. The healthy fake therefore reports partial system coverage 7/8.

To use Team 12 files, retain the synthetic directory, point canonical `clean`, `seeded_issues`
and `corrupt` expectation entries to `shared/` files with `origin: team12`, and author their
hashes/targets/classes independently. Configure a copy of the manifest if both fixture sets
need separate runs. No expectation may be derived from the target engine. The six component
and eight AC catalogs remain Team 9 defaults awaiting pod confirmation.


Manifest 1.5.0 has 64 cases, including `P3-VERSION-REVIEWER` and `P3-AUDIT-REVIEWER`, and
eleven pod contract 1.1 cases (`P3-INPUT-SHEETS-CHARTSHEET` plus an upload/audit pair for each
of the five fixtures above).
The configured reviewer must be explicitly granted access to userA and authorized for the test
session on the target; reviewer role alone is not access. Expected findings and repeatability
are independent: any valid correctly identified completed seeded audit supplies the comparison
baseline, even if its detection accuracy fails the oracle. Invalid/missing/failed audits still
cause repeatability skips. The healthy local fake yields 63 passes and one observation skip.
