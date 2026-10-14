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

Generated at run time, not committed: the 25,000,000-byte boundary workbook (P3-10) and the
25,000,001-byte over-limit upload (P3-09).

All three committed files are **synthetic stand-ins**. Team 12's shared `clean.xlsx`,
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
files in Excel without repair warnings: **not yet checked** (openpyxl loads them). Additional inputs
named in the contract (protected files, ZIP/XML attacks, Unicode whitespace variants, repeated IDs,
multiple drawings, controlled faults) are not yet in the deployed suite.
