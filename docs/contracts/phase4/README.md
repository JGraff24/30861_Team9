# Phase 4 shared specification pack

Specification 2.1.0 • Ruleset 2.0.0 • Suite target 2.1.0

`Phase4_Shared_Specification.md` is the shared contract. The formatted document contains the same contract for review.

- `phase4-wire-schemas.json` contains 44 named JSON Schema 2020-12 definitions, including the isolated test-fault setup. Ownership, state semantics, ordering and other cross-record checks remain required application validation.
- `phase4-examples.json` contains 18 validated formatting examples and a computed finding-ID vector. They are examples, not actual deployment or suite results.
- `phase4-policy-vectors.json` freezes text, color, table-region, finding-ID and duplicate-object location vectors.
- `phase4-test-plan.json` contains 215 hosted assertion entries, 24 local integration entries, and 10 human evidence entries. It specifies required checks rather than implementing an executable tester. Grouped vectors need separately reported result IDs in a final runnable manifest; the executable count may be larger.

The instructor testing envelope and report standards stay unchanged. The test-only fault option is confined to the existing system setup object, with trusted enablement, test-admin authorization and a fresh isolated namespace. It does not authorize failures in ordinary data.
