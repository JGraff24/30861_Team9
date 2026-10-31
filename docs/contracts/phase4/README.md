# Phase 4 shared specification pack

Pack revision 2.1.1 • Specification 2.1.0 • Ruleset 2.0.0 • Suite target 2.1.1

Read `CORRECTIONS.md` first. This pack replaces the earlier pack for tint vectors and the suite target version. It contains the baseline specification 2.1.0 unchanged, plus a mandatory tint-vector correction and suite target 2.1.1. Where the baseline names manifest 2.1.0, use 2.1.1 for newly frozen suites based on this corrected pack. The six-rule policy and business wire schemas are unchanged; ruleset remains 2.0.0.

`Phase4_Shared_Specification.md` remains the baseline shared contract. `CORRECTIONS.md` governs the limited pack correction; it does not change unrelated requirements. Historical audits and retained reports keep their original versions and observations.

`SHA256SUMS.txt` records the SHA256 of every other member of this pack. Record the actual runnable suite/fixture hashes separately when those artifacts exist. This pack is agent context, not executable tests or implementation verification.

- `phase4-wire-schemas.json` contains 44 named JSON Schema 2020-12 definitions, including the isolated test-fault setup. Ownership, state semantics, ordering and other cross-record checks remain required application validation.
- `phase4-examples.json` contains 18 validated formatting examples and a computed finding-ID vector. They are examples, not actual deployment or suite results.
- `phase4-policy-vectors.json` freezes text, color, table-region, finding-ID and duplicate-object location vectors.
- `phase4-test-plan.json` contains 215 hosted assertion entries, 24 local integration entries, and 10 human evidence entries. It specifies required checks rather than implementing an executable tester. Grouped vectors need separately reported result IDs in a final runnable manifest; the executable count may be larger.

The instructor testing envelope and report standards stay unchanged. The test-only fault option is confined to the existing system setup object, with trusted enablement, test-admin authorization and a fresh isolated namespace. It does not authorize failures in ordinary data.
