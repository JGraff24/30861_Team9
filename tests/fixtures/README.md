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

Generated at run time by `tester/suite/workbooks.py`, not committed: the 25,000,000 / 25,000,001-byte
uploads (P3-INPUT-BYTES-AT / -OVER), 50 / 51 worksheets (P3-INPUT-SHEETS-AT / -OVER), 250,000 /
250,001 nonempty cells (P3-INPUT-CELLS-AT / -OVER) and a password-protected structure
(P3-INPUT-PROTECTED). `manifest.json` also holds the 14-operation catalog used by run records.

All thirteen committed files are **provisional synthetic fixtures**; the original three are
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
