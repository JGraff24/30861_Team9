# Dylan's synthetic workbook fixtures

These are **locally generated synthetic fixtures**, not Team 12's shared `clean.xlsx`,
`seeded_issues.xlsx` or `corrupt.xlsx` (still pending). They exist so the workbook engine
can be developed and regression-tested before the shared files arrive.

| File | Purpose | Expected outcome |
|---|---|---|
| `clean.xlsx` | Picture, chart and shape with nonblank title and description; descriptive and hidden sheets | 0 findings, 0 limitations |
| `seeded_issues.xlsx` | Missing one/both fields, whitespace-only fields, native decorative marker, group child, hidden sheet, generic names `Sheet1`/`sheet02` | 8 findings |
| `edge_cases.xlsx` | Duplicate and missing object IDs, mc:AlternateContent, legacy VML note and button | 3 findings, 4 limitations |
| `corrupt.xlsx` | First half of `clean.xlsx` (truncated ZIP) | 422 `INVALID_WORKBOOK` |

- `build_fixtures.py` rebuilds the files from explicit seeds:
  `uv run python tests/fixtures/synthetic/dylan/build_fixtures.py`
- `expectations.json` was **written by hand from those seeds**; it was not produced by
  running `audit()`. Finding IDs are re-derived independently in
  `tests/unit/test_synthetic_fixtures.py` from the recorded source SHA256.
- Rebuilding with a different zlib can change bytes and hashes. Update the recorded `sha256`
  values only after confirming the change was intended.
- Two-member manual review of these expectations: **pending** (not yet performed).
- Manual opening in Excel without repair warnings: **pending** (not yet performed).
