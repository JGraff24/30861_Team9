# Dixon Phase 5 implementation: independent tester and CI

Branch `phase5/dixon-tester`, based on `main` at `ca6becb` (the reviewed Phase 4 baseline).
Target contract: Pod E specification/pack 3.0.0 (ruleset 2.0.0, suite target 3.0.0), copied
byte for byte into `docs/contracts/phase5/` (hashes verified by `SHA256SUMS.txt` and a unit
test). This branch changes the tester, its fixtures, CI, the tester's proxy timeout and the
deploy probe's accepted suites. It does not change the system's API, engine, editor, storage,
scheduler or UI. The shared pack and specification are unchanged.

## Delivered

| Area | Files | What it does |
|---|---|---|
| Wire contract | `tester/phase4/contract.py` | One validator, two frozen packs. A Phase 5 run selects 3.0.0 for the whole run (new reports must be `NewAuditReport`, timestamps at most six fraction digits); Phase 4 runs keep 2.1.0 |
| Calendar oracle | `tester/phase5/policy.py`, `scripts/check_phase5_policy.py` | Strict timestamps/dates/intervals, initial due, grid coalescing, UTC 90/30/7 thresholds, eligibility, crossing. Checked against every vector and all 26 valid / 21 invalid examples |
| Report profiles | `tester/phase5/profile.py`, `tester/config.py`, `tester/service.py`, `app/contracts/models.py` | Trusted own/cross classification (exact URL to team map; never a caller field); own runs require a valid design map at setup (else 500/tester) and get `coverage.model`; cross runs omit the key. Profile and map bytes/hash/version are frozen with the plan, so normal, abort and restart-recovery reports all keep it. `SuiteCoverage.model` is optional and never serialized as null |
| Manifest | `scripts/build_phase5_manifest.py`, `tests/fixtures/manifest-phase5.json`, `tests/fixtures/expectations-phase5.json` | 395 hosted cases: the 253 Phase 4 cases unchanged in ID/order/expansion, then 142 new. Catalogs C1-C12, AC-01..AC-18, operations 1-14 + S/V/R/G, limits 2,400/2,430 s and 30 s DELETE. The 250,000-cell AC-18 workbook is rebuilt by the tester's generator and checked against its frozen SHA256 |
| Checks | `tester/phase5/checks.py` (28 handlers) | Schedules, reminders, isolated clocks, deletion, the new-operation access matrix, seven injected faults, fault-control rejections, report self-checks and five real-time performance runs plus comparison |
| Outcome rule | `tester/phase4/checks.py` (`unexpected_failure`), `tester/suite/runner.py` | Phase 5 runs record an unexpected `PROCESSING_FAILED` of an accepted fixture, remediation or output audit as `error`/`system` (specification 3.0.0 §14 supersedes the Phase 4 fail rule). Phase 4 manifests keep their rule; stored reports are never relabelled |
| Coverage | `tester/suite/runner.py` | 12 components and 18 criteria; C9 and AC-17 never counted over HTTP; model counted once per requirement from executed qualifying assertions only. Code coverage is copied from the target's session report after the last planned test |
| Drain safeguard | `tester/suite/async_work.py`, `tester/target.py` | Retained PR 15 behavior, extended: clock-accepted audits are resolved through owner history and drained with bounded GETs; userB work drains as userB; a completed deletion drops that workbook's pending work. The original failed assertion is kept and the drain earns no coverage. PUT/POST/DELETE are never resent |
| Catalogs | `scripts/build_phase5_catalogs.py`, `docs/testing/phase5-coverage-catalogs.md` | Generated component/system catalogs, design-requirements-to-tests mapping and the placement of every non-hosted plan entry; CI fails if stale |
| Design map | `docs/design/team9-design-map.json`, `docs/design/README.md` | Draft 0.1.0 with 23 requirements (5 not reachable over HTTP), pending team confirmation |
| Rename investigation | `tester/phase5/rename_probe.py`, `scripts/rename_probe.py`, `docs/evidence/phase5/` | Single-reference rename variants and a probe that pinpoints classification/rewrite disagreements; Team 9 baseline recorded |
| CI and operations | `.github/workflows/ci.yml`, `scripts/integration_selfcheck.py`, `scripts/tester_selfcheck.py`, `tests/acceptance/`, `deploy/nginx.conf.template`, `scripts/deploy_host.py`, `.env.example` | Phase 5 policy/manifest/catalog checks, Windows portable tests, live fake self-check, a separate real-system Phase 5 job (evidence, allowed failures), `phase5` live-suite option with expected profile, tester proxy 2,460 s, deploy probe accepts the Phase 5 suite |

## How the new cases are built

- Every case that moves simulated time runs in its **own fresh namespace** (its own durable
  clock), created through ordinary system setup during the run, never in tester setup. The main
  namespace's clock is never advanced, so its effective time stays 2027-01-01T00:00:00Z.
- Preparation (setup, uploads, audits, baseline schedules) earns no coverage. A refused or
  failed preparation is still a fail/error of that case, never a pass.
- Expected instants come from `tester/phase5/policy.py`; expected findings from the reviewed
  fixture expectations and the tester's own finding-ID function.
- Races are asserted only when observed. The busy-version, active-deletion and
  PROCESSING-eligibility cases check the active state on both sides of the call; if the work
  had already finished they skip as unobservable instead of guessing.

## Judgment calls (also in the manifest's `pending_confirmation`)

- `P5-TESTER-*` report-shape self-checks evaluate the tester, not the target, so they earn no
  C6 credit despite the plan's C6 tag; `P5-TESTER-CODE-COPY` reads the target report and does.
- `P5-CLOCK-NOT-READY` and `P5-FAULT-REJECT-DISABLED` cannot be observed over HTTP on a
  deployment with the capabilities enabled; they skip with that reason.
- The ungranted-reviewer cases read a userB workbook, so the configured reviewer must hold a
  grant from userA only. Foreign-admin clock cases need an optional `test-adminB` credential.
- Fault/clock isolation probes write one clean workbook into ordinary data and remove it by
  owner deletion.
- Injected messages for remediation, report and scheduled-audit faults follow the §11 pattern;
  synchronous `TEST_STORAGE_FAILURE` details are not asserted (unspecified).
- AC-18 is tester-observed end-to-end real time; it fails a run beyond 75 s (15 s queue + 60 s
  engine) and accepts a controlled failure as the stated alternative while disclosing it.

## Verification

| Check | Result |
|---|---|
| `pytest -q` (whole repository) | 1,045 passed, 5 skipped (live-tester acceptance needs a configured deployment) |
| `tests/unit/test_phase5_tester.py` | profiles, oracle, manifest/catalog currency, full own and cross runs against the fake, six defect switches, drain preservation, error/system classification, abort/recovery profile, setup forwarding, no-resend |
| `ruff check`, `ruff format --check`, `mypy --platform linux app tester scripts` | pass |
| `scripts.check_phase5_policy`, `build_phase5_manifest --check`, `build_phase5_catalogs --check`, `export_schemas --check` | pass |
| `scripts.tester_selfcheck --phase5 --profile own` and `--profile cross` (real sockets, test-only fake) | pass, no fail/error |
| `scripts.integration_selfcheck --phase5 --allow-failures` (real API + worker at `ca6becb`) | 233 pass / 138 fail / 0 error / 24 skip; `docs/evidence/phase5/` |

### Real system at the Phase 4 baseline (what is still missing on the target side)

The run against the current Team 9 system (no Phase 5 system work merged yet) is the
concrete gap report: operations 11-14 and G return 404, so the new scheduling, reminder,
clock, deletion and fault cases fail; new reports still carry `specificationVersion` 2.1.0, so
18 inherited report-validating cases fail and their dependents skip. Inherited behavior
otherwise passes, and the five 250,000-cell runs completed with identical findings. These are
implementation gaps for the system owners, not weakened assertions.

## Not done (pending, not claimed)

- No run against Team 12 (URL, credentials, team ID and capability confirmation pending); no
  hosted Team 9 Phase 5 run (the system work and the tester deployment switch are pending).
- Team confirmation of the design map; pod adoption of pack 3.0.0 and this manifest.
- Two-member Team 12 review of the new performance fixture expectation; Excel checks.
- Target-side local-integration and manual-release entries (57 + 21 in the plan, less the
  14 tester-side entries implemented here as pytest cases).
- Deployment timing beyond the tester proxy: the 1,900 s deploy wait and proxy stop bounds and
  the renewable 1,900 s session lease (owner review against the 2,400 s budget).
