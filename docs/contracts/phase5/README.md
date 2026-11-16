# Phase 5 pod specification pack

Pod E Excel Accessibility Hub | Specification and pack 3.0.0 | Audit ruleset 2.0.0

This pack gives Teams 9 and 12 a common, testable Phase 5 implementation target. Read Phase5_Shared_Specification.md first, then CHANGELOG.md, the companion schemas/examples/vectors and the assertion inventory. The baseline_phase4 directory preserves all eight adopted Phase 4 pack files byte for byte; its correction note remains authoritative for unchanged tint vectors.

## Files

| File | Purpose |
|---|---|
| Phase5_Shared_Specification.md | Canonical shared behavior, API, lifecycle, isolation, failure and acceptance rules |
| phase5-wire-schemas.json | Full 59-definition schema collection, including own/cross suite report profiles and retained/new audit reports |
| phase5-examples.json | 26 valid formatting examples and 21 invalid schema/semantic examples; no actual run results |
| phase5-policy-vectors.json | Fixed cadence, UTC/leap-year reminder, eligibility, profile and outcome oracles |
| phase5-test-plan.json | 435 normative assertion entries: 249 inherited and 186 new; not executable tests |
| team-design-map.template.json | Blank configuration scaffold for each team's own design coverage; not an approved map |
| TRACEABILITY.md | Phase 2 obligations, Phase 4 baseline and instructor requirements mapped to the new contract |
| CHANGELOG.md | Explicit semantic choices and compatibility changes |
| ADOPTION.md | Unfilled pod adoption and deployment evidence record |
| validate_pack.py | Offline artifact consistency and vector/schema checks; requires Python 3 and jsonschema |
| PACK_VALIDATION.md | Actual artifact validation result and its limits |
| SHA256SUMS.txt | Hashes of every other pack member |
| baseline_phase4/ | Unchanged Phase 4 specification 2.1.0 and corrected context pack 2.1.1 |

The shared suite target is 3.0.0. Each team's runnable manifest may expand grouped assertions into more individually reported cases; record its actual version and fixture hashes. The counts above are an inventory, not a claim that 435 tests are implemented or have passed. Some inherited/new entries overlap acceptance evidence and can share execution resources while retaining their qualifying assertions.

## Required reading order

1. Phase5_Shared_Specification.md sections 1 through 5 for authority, scope and wire compatibility.
2. Sections 6 through 12 for schedule, reminder, clock, deletion, faults and time budgets.
3. Sections 13 through 18 for instructor endpoints, conditional model coverage and acceptance.
4. Inherited Phase 4 sections for all unchanged audit/editor/report behavior; do not implement older superseded string-valued decisions.
5. TRACEABILITY.md and ADOPTION.md before freezing implementation expectations.

## Artifact verification

Run `python3 validate_pack.py` from this directory or pass its path from elsewhere. It validates examples, required negative cases, calendar vectors, test IDs/catalog references and SHA256 hashes. It performs no network calls, service mutations, implementation tests, workbook inspection or adoption updates. A successful result establishes pack consistency only.

The model coverage key is required for an own-team tester run and forbidden for cross-team runs. The complete team design requirement list is separate from the eighteen shared acceptance criteria. Prepare actual team design maps and configure trusted target identities; do not send team flags or credentials in the grader setup body.

This pack contains no production credentials, independently approved workbook byte pack, deployed service or completed human acceptance evidence. Both teams keep their own independent complete fixture collections, while using the same shared policies and alignment vectors.
