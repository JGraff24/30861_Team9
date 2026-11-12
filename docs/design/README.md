# Team 9 Phase 5 design map (model coverage)

`team9-design-map.json` is the tester's frozen input for `coverage.model` on **own-team** runs
(specification 3.0.0 §14). It validates against `TeamDesignMap` in
`docs/contracts/phase5/phase5-wire-schemas.json`, and the tester refuses setup (500, source
`tester`) if it is missing, invalid, belongs to another team, repeats a requirement ID or names
a test ID that the frozen Phase 5 manifest does not plan.

## Status: draft 0.1.0, pending team confirmation

The 23 requirements were drafted by Dixon from Team 9's delivered architecture (`app/`
modules, the Phase 4 implementation notes and design decisions) and the Phase 5 obligations it
must implement. They are not yet the team's approved milestone design model. Before the first
graded own-team run:

1. Replace or confirm each requirement against Team 9's actual Phase 5 design/UML model, keeping
   stable IDs. Add every design requirement, including ones no hosted test can reach (leave
   `testIds` empty; they stay in the denominator).
2. Bump `designVersion` (SemVer) on any change. The tester freezes the map bytes, SHA256 and
   version with each session, so earlier reports keep their original basis.
3. Keep `mappingUrl` pointing at a stable, accessible copy of this mapping (after merge:
   `https://github.com/JGraff24/30861_Team9/blob/main/docs/design/team9-design-map.json`;
   prefer a tag or commit link for a graded run).
4. Regenerate `docs/testing/phase5-coverage-catalogs.md`
   (`python -m scripts.build_phase5_catalogs`); CI checks it is current.

## Counting rule

A requirement counts once when at least one mapped test executed a qualifying assertion in that
session, even if it failed. Setup, preparation, skips and the drain of unexpectedly accepted
work earn nothing. `total` is the whole list. Cross-team runs omit `coverage.model` entirely and
never read this file.
