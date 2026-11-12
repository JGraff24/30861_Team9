# Phase 5 pack validation

Validated November 10 2026.

The offline validator passed the following artifact checks:

- 59 JSON Schema definitions are structurally valid.
- 26 supplied valid examples satisfy their declared schemas and applicable semantic checks.
- 21 invalid examples are rejected, including own/cross model-profile violations, invalid intervals/dates, malformed fault controls and invalid coverage counts.
- Fixed cadence, leap-year, reminder eligibility/crossing and strict interval vectors agree with independently computed UTC calendar arithmetic.
- All 435 assertion IDs are unique, prerequisites precede their dependents, component/acceptance/operation references resolve, and tier totals agree: 357 hosted HTTP, 57 local integration, 21 manual release.
- All 249 inherited assertion IDs and their ordering are retained; 186 new entries are identified separately.
- All eight inherited Phase 4 files match the reviewed GitHub baseline; the inherited member SHA256 checks pass.
- Final pack hashes and the exact archive member list are verified during packaging.

These checks validate the specification artifacts. They do not establish implemented endpoints, a runnable Phase 5 suite, correct workbook handling, real performance, human fixture approval, pod adoption, deployment, or completed acceptance. The filled team design maps and actual fixture byte hashes remain implementation/adoption inputs.

Reproduce the artifact checks with `python3 validate_pack.py` in the extracted pack. Python 3 and the jsonschema package are required. No network access, credentials or running project service is used.
