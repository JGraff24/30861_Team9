# Phase 5 tester evidence (Dixon, November 12 2026)

All runs below are local. None contacted Team 12, AWS or any hosted deployment. No credentials
are stored in these files.

| File | What it is |
|---|---|
| `real_team9_ca6becb_report.json` | Unchanged stored SuiteReport bytes of one own-team Phase 5 run (tester at `005e921`, clean tree) against the **real** Team 9 API and worker at the Phase 4 baseline `ca6becb`, private temporary state (`scripts.integration_selfcheck --phase5 --allow-failures`). |
| `real_team9_ca6becb_run_record.json` | Its sanitized run record: counts, hashes, profile, per-operation and per-AC status. Every non-pass is `unclassified` for triage. |
| `rename_probe_team9_local.json` | Single-reference rename probe against a private local Team 9 system (`scripts.rename_probe --local-team9`); see `rename_investigation.md`. |
| `rename_investigation.md` | Independent-test-side analysis of the open worksheet-rename discrepancy. |

## Real Team 9 system at the Phase 4 baseline

395 planned: 233 pass, 138 fail, 0 error, 24 skip (12 min 24 s wall clock including startup;
the Phase 5 portion is fast here because the target answers 404). Coverage: code 2,739/3,542
target-measured lines, component 11/12, system 17/18, model 15/23 (draft design 0.1.0).

This is the expected gap report for a system without the Phase 5 work, not a test defect:

- 102 new cases fail on HTTP 404 for operations 11-14 and G (schedule, notifications, clock,
  delete), including the foreign-admin clock case and the main-namespace schedule reads.
- 16 new cases fail because system setup refuses the Phase 5 `clock` key and the six new fault
  keys (400); the tester treats a refused preparation as a failed observation.
- 1 new case fails because a clock call without a namespace returns `NOT_FOUND`, not
  `TEST_CLOCK_DISABLED`; 1 because a failed-audit report still carries specificationVersion
  2.1.0.
- 18 inherited cases fail because new reports still declare specificationVersion 2.1.0 where
  3.0.0 is required (§3); 21 inherited cases skip as dependents of those report failures.
- Inherited behavior otherwise passes (214). The eight fault-control rejection cases that the
  Phase 4 setup can already evaluate pass. The five 250,000-cell AC-18 runs completed in
  41.9-46.5 s end to end (upload 9.2-11.2 s) on this host with identical findings; these are
  tester-observed real times, and target-side queue/engine timing is separate evidence.

The runtime of a run against a system that implements Phase 5 is not measured yet. The new
cases then do real uploads, audits and remediations in about seventy fresh namespaces, so the
2,400 s budget must be confirmed on the deployed system (ADOPTION.md).

## Test-only fake target over real sockets

`scripts.tester_selfcheck --phase5 --profile own` and `--profile cross` both passed (no fail or
error in 395 cases; five acceptance checks including the report-profile check). These runs
prove tester behavior only.
