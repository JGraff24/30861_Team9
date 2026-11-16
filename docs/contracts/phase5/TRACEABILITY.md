# Phase 5 source and acceptance traceability

## Obligation mapping

| Source obligation | Shared contract sections | Required evidence |
|---|---|---|
| Phase 2 REQ-12 and AC-12 periodic audit | 6, 7, 9, 17 | 1/90-day boundaries, exact due, selected stored version, immutable audits, disable, missed periods, restart |
| Phase 2 REQ-12 and AC-13 reminders | 8, 9, 16, 17 | 90/30/7-day eligibility, clean suppression, dedup, version disclosure, external-change limitation |
| Phase 2 REQ-13 and AC-14 failure handling | 10, 11, 12, 17 | Actual fault envelopes, preserved source, no partial success, retry/recovery evidence |
| Phase 2 REQ-14 and AC-15 authorization | 4, 5, 9, 10, 17 | Owner/granted reviewer/foreign and unauthenticated access matrix, namespace/clock isolation |
| Phase 2 REQ-14 and AC-16 security | 10, 16, 17, 19 | HTTPS, protected hashed credentials, full deletion, backup/restore behavior |
| Phase 2 REQ-15 and AC-17 accessibility | 16, 17 | Keyboard workflow, labels, focus, errors, scanner and contrast evidence |
| Phase 2 REQ-15 and AC-18 performance and repeatability | 9, 12, 17 | Five real-time runs, documented environment, controlled outcomes and stable findings |
| Phase 2 Q5 operations 11 through 14 | 5 through 10 | Typed requests/responses and success/error paths |
| Phase 2 Q6.3 milestone | 2, 17 through 19 | Deployed increment, updated models, security/accessibility/performance evidence, AC-01 through AC-11 regressions |
| Phase 2 Q9 preservation/accuracy risks | 2, 17 | Excel no-repair evidence, five real workbooks and >=90% adjudicated precision |
| Phase 4 ruleset/correction pack | 2, 3 and baseline_phase4/ | Unchanged ruleset 2.0.0, half-up tint vectors, finding identities and supported edits |
| Instructor tester/system setup/run/report | 12 through 14, 19 | Same session ID, preparation first, one run, recovery via report, complete results, correct errors/sources |
| Instructor Phase 5 model coverage | 14 and team-design-map.template.json | Own-team required model, cross-team omission, full design denominator, version/link and actual assertion mapping |

## Decisions completing previously open details

Sections 6 through 10 choose newest-version selection, cadence anchoring, UTC thresholds, no reminder backfill, durable deduplication, virtual-clock isolation, atomic due processing and deletion recovery. These are proposed shared implementation decisions to adopt consistently, not claims that Phase 2 already spelled out every detail. They preserve the agreed capabilities and make independent implementations comparable.

The schedule-read operation and selected versionId are explicit interface additions for inspectability. The expanded test fault object is a pod-defined starting-state extension inside system setup, not a change to instructor tester setup. The instructor requirement overrides old outcome classification. No legal deadline is determined by this specification; an institutional date is supplied and examples are synthetic calendar values.

## Reviewed Team 9 baseline

The source review was pinned to ca6becbefc32ed1462be2dfee54a8230c7361e8a. Existing implementation paths relevant to integration include app/api/routes.py, app/contracts/models.py and phase4.py, app/services/projects.py, app/jobs/worker.py, app/storage/sqlite.py and blobs.py, app/test_interface/, app/ui/, tester/suite/runner.py and manifest.py, tester/service.py and store.py, and deployment/CI configuration. These references identify existing foundations; they do not prescribe Team 12 internals or establish any Phase 5 implementation result.

Team 9's current SuiteCoverage forbids the new model key; manifest/catalog validation is fixed to Phase 4; and the Phase 4 polling helper classifies unexpected PROCESSING_FAILED as CheckFailed. Both report generation and abort/recovery paths therefore need coordinated review. Existing source and finalized reports must survive that migration unchanged.

## Final pre-submission alignment recheck

The last code commit is 328669902efc1094facb86bb05db1cc713a1487d, merged as ca6becb in PR 15 on November 1. These commits have the same final tree. The preceding main merge, PR 14, updates README only. PR 15 has no review/discussion comments recording an additional rule-policy change.

| Observed final change | Phase 5 treatment |
|---|---|
| Two worker slots, separate housekeeping and graceful draining | Existing delivered Team 9 foundation; preserve while adding scheduled work; no requirement for identical Team 12 internals |
| Queue/worker failure distinction and diagnostic stage | Preserve the existing 15/60/180-second bounds and correlated real-time diagnosis |
| Renewable protection of remote sessions between requests | Preserve; review the existing 1,900-second lease/wait settings against the new 2,400-second suite and overhead |
| Drain accepted boundary/preparation work and unexpectedly accepted negative requests | Explicit tester requirement; preserve original failed assertion, no replay/replacement and no preparation coverage credit |
| Formula-link target discussion | PR 15 explicitly preserves engine targets and independent finding oracles; it does not implement a switch to Team 12's cell targets |
| HSL tint expectations | Corrected pack 2.1.1 remains unchanged and included byte-for-byte |

The latest Phase 4 submission reports that the formula-link interpretation was settled without changing finding identities. The earlier PR incident note retains the then-open interpretation question. Neither source justifies inventing a production target change; any later agreement beyond the unchanged semantics needs its actual evidence before altering the shared oracle.
