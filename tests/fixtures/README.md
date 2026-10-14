# Fixture delivery is pending

No workbooks, independently reviewed expectations, or suite manifest are included in bootstrap.
Dixon maintains the vetted manifest; Team 12 delivery of `clean.xlsx`, `seeded_issues.xlsx` and
`corrupt.xlsx` remains unconfirmed. Do not claim they exist or derive an independent oracle by
running the engine being tested. Additional synthetic fixtures must be labeled separately.
`FIXTURE_MANIFEST_PATH` is a configuration input, not a working suite catalog yet.

The schema tests use small synthetic JSON payloads and the supplied standard-library hash vector.
They provide contract evidence only, not AC-01/02/03/04/05/14/15/18 completion or workbook coverage.
