# Dixon Phase 3 evidence

Owner: Dixon (tester, tests/contract, tests/acceptance, fixture manifests, CI).
Branch `phase3/dixon` from foundation `ffa6947`. Implementation details:
[../implementation/dixon_phase3.md](../implementation/dixon_phase3.md). Every entry below is an
actual local run unless it is marked **pending**.

## Test run log

| Date (local) | What ran | Against | Result |
|---|---|---|---|
| 2026-10-14 | Baseline `pytest tests/unit tests/contract` on fresh checkout | Foundation only | 139 passed, 2 failed (Windows-only: cp1252 profile read; socketpair blocked by the no-network guard) |
| 2026-10-14 | Same, after the Windows fixes (commit `18c687c`) | Foundation only | 141 passed |
| 2026-10-14 | `pytest tests/unit tests/contract` | Tester, test-only fake transport | 189 passed |
| 2026-10-14 | `scripts.tester_selfcheck`, run 1 | Real tester process → **test-only fake target** process | HTTP 200 in 11.5 s; 22 run / 22 pass / 0 fail / 0 error / 0 skip; code 40/50 (fake value), component 6/6, system 8/8; acceptance 3 passed |
| 2026-10-14 | `scripts.tester_selfcheck`, run 2 | Same | HTTP 200 in 10.5 s; 22/22 pass; acceptance 3 passed |
| 2026-10-14 | pip-audit, locked runtime + measurement extra | Lock file | No known vulnerabilities found |
| 2026-10-14 | `pytest tests/unit tests/contract` after closing handoff gaps (30-test suite, records, limits) | Tester, test-only fake transport | 193 passed |
| 2026-10-14 | `scripts.tester_selfcheck`, 30-test suite | Real tester process → **test-only fake target** process | HTTP 200 in 11.7 s; 30 run / 30 pass / 0 fail / 0 error / 0 skip; acceptance 3 passed |
| **pending** | `scripts/run_suite.py` | Integrated local Team 9 system | System endpoints not implemented yet |
| **pending** | Same | Team 9 on AWS | No deployment or URL |
| **pending** | Team 12 tester → Team 9 system, and Team 9 tester → Team 12 system | Cross-team | URLs and access provisioning not available |
| 2026-10-14 | GitHub Actions `ci`, first run on PR #2 | Pushed branch | dependency-audit passed; checks (ubuntu) failed: 1 failed, 188 passed (fixture `create_system` byte); fixed in the next commit |

The fake target's 40/50 code coverage is a constant in `tests/support/fake_system.py`; it is
not a measurement of any real code. Self-check results show the tester's own behavior and
nothing about the Team 9 system.

## Cross-team report coverage

**Pending.** No cross-team report exists. When one does, record for each direction: run
datetime, tested tag/SHA, target URL, run/pass/fail/error/skip counts, each failure classified as
system, test or specification, and code/component/system coverage with their bases. The component
and system catalogs (C1-C6, eight AC portions) must be shared with Team 12 before quoting
percentages.

## Evidence index (contributions → assignment areas)

| Area | Evidence | Status |
|---|---|---|
| Delivered scope | Implementation doc "Delivered boundary" | Local only |
| All 14 operation statuses | `operation_status` in each run record (`scripts/run_suite.py --record`); catalog in `tests/fixtures/manifest.json` | Generator done; all 14 operations (name, method, path, REQ IDs) copied from Phase 2 Q5.1; Jackson consolidates |
| Test-ID to AC map with oracle rationale | Implementation doc "Suite and traceability"; `tests/fixtures/manifest.json`; `tests/fixtures/README.md` | Done; manual review pending |
| Regression and static checks, CI | Commands table in the implementation doc; `.github/workflows/ci.yml` | Local passes recorded; CI link pending a push |
| Deployed-run results | This log | Pending integration and deployment |
| Models and consistency | Implementation doc Mermaid models (checked against `tester/` code) | Done |
| Cross-testing and ambiguities | Contract confirmation items below | Pending runs |
| Limitations and Phase 4 risks | Implementation doc limitations table | Done |

### Ambiguities found while implementing (for Jackson's Team 12 confirmation list)

- Exact error `code` strings for 400/401/403/404/413/415/422 (only the `.xlsm` code is fixed).
- Whether `X-Test-Session` should go on the public health call (the tester sends it; an unknown
  session there would be a 400 under the current default).
- Cleanup uses `cleanup_session_ids` on a later new setup. The current implementation always
  queues failed/uncertain setup compensation; cleanup of retrieved completed namespaces remains
  opt-in. Pod confirmation of cleanup/idempotency behavior is still pending.
- Whether an unreferenced-ancillary-chunk PNG padding approach is an acceptable way to hit the
  exact 25,000,000-byte boundary for both teams' validators.
- Resolved from the Phase 2 specification: worksheet/cell limit violations and password-protected
  structures are 422 (Operation 2 error responses and the common status codes). Still open:
  `.xlsm` is 415 under "not an .xlsx" but 422 under "macro-enabled"; the tester expects 422.
- The full list is in `pending_confirmation` in `tests/fixtures/manifest.json` and is copied
  into every run record.

## GenAI use (Claude Code, this session)

**Delegated:** reading the repository and contract; designing and writing the tester package,
suite, fixtures generator, tests, CI workflow and these docs; running all verification commands.
After the first PR, Dixon supplied the personal Phase 3 handoff. Claude Code compared it with
the delivered work, reported the gaps (no operation-status artifact or run records, missing
protected/limit/unauthenticated/foreign-audit tests, no opt-in live CI job, no
coverage-unavailable test, docs gaps), and closed them on the same PR.
Dixon directed the scope (implement the owner part, clone into `C:\Users\Dixon Wagner\code`, no
push). Dixon has not yet reviewed the resulting code.

**Verification performed:** every claim in the results tables came from a command executed in this
session (outputs are quoted in the implementation doc). Real processes on 8000/8001 were used for
the self-check. Tests cover the failure paths, not just the passing path.

**Real corrections that occurred:**

1. The first diagnosis of the baseline profile-hash failure blamed CRLF line endings, and a
   `.gitattributes` was added. A re-run showed the same digest. The actual cause was
   `Path.read_text()` defaulting to cp1252 on Windows. The `.gitattributes` was removed and
   `encoding="utf-8"` added.
2. The deadline-abort test passed or failed depending on timing, because Windows `time.monotonic()`
   ticks roughly every 15 ms, so a 1 µs deadline didn't always expire. The fix injected the
   clock into the target client and advanced it deterministically in the test.
3. The fixtures were first DEFLATE-compressed. That output depends on the zlib build, so the
   committed bytes could differ between Windows and Linux CI. They were switched to stored
   entries and a fixed PNG `IDAT` stream.
4. The first CI run (PR #2) failed on Ubuntu: `zipfile.ZipInfo` records the host OS in the
   "version made by" byte (0 on Windows, 3 on Linux), so the generator's output didn't match the
   committed bytes. The fix pins `create_system = 0`; the committed fixtures are unchanged.

**Reflection (Dixon to write):** _Personal reflection on agentic engineering is the student's own
and has not been drafted here._


## Codex handoff completion and final audit (2026-10-14)

The user requested comparing `phase3/dixon` with `04_Phase3_Dixon_Codex.md`, then completing the
Dixon-owned implementation and verifying every branch change against that document. Codex
reviewed the original branch at `9a76499` and the complete Dixon diff from foundation `ffa6947`,
modified local files, and ran actual local checks. No subagents, push, merge, deployment,
paid provisioning or unapproved remote target calls were used.

The initial 193-test green suite missed real defects: continuously streamed replies could
exceed a setup deadline and become READY; audit resource IDs/prose were not checked; failed
pre-hash requests counted AC-03; coverage was fetched twice; and recovery rebuilt from a changed
manifest. Controlled reproductions established these defects before changes. The implementation
now bounds calls, snapshots setup/plans, preserves actual assertion markers, uses individual
case outcomes, retrieves code coverage once, and rejects wrong/reused resource IDs and non-Z
wire timestamps. Interrupted setup queues deferred compensation. Runtime fixture assets ship
in the wheel, and the operator CLI writes evidence then returns nonzero on failures/errors.

| Date | Actual check | Outcome |
|---|---|---|
| 2026-10-14 | Complete pytest run, final working tree | 225 passed; 4 configured-live tests skipped; one existing Starlette/HTTPX warning |
| 2026-10-14 | Final local real-process self-check | 51 planned; 50 executed/pass, 0 fail, 0 error, 1 controlled-failure observation skip; run 200 in 11.0 s |
| 2026-10-14 | Configured acceptance against the local fake target/tester | 4 passed in 22.33 s, including lost-response recovery without a second run |
| 2026-10-14 | Ruff lint/format, strict mypy, OpenAPI/profile, vectors and fixture hashes | Passed; 49 formatted Python files, 32 source files typed, 13 fixtures current |
| 2026-10-14 | uv lock consistency and standard locked install | Passed; 84 packages resolved, local project installed |
| 2026-10-14 | Wheel build and load outside checkout | Passed; 51 cases, 13 assets and 3 routes; no target/network/state access |
| 2026-10-14 | Locked runtime + measurement dependency scan | No known vulnerabilities found |
| 2026-10-14 | Full diff/import boundary and whitespace review | Passed; production tester imports shared contracts/settings only |

Retained artifacts: [local report](dixon_local_selfcheck/report.json),
[run/operation/AC record](dixon_local_selfcheck/run-record.json), and the complete
[handoff verification matrix](dixon_handoff_verification.md). That earlier run's UTC timestamp was
2026-10-14T19:24:25.460510Z; the retained files now represent the latest follow-up below.
Target revision remains **unknown**, not inferred. This was an explicitly
fake target at loopback, not the integrated Team 9 system. Code 40/50 is a constant fake response;
component 6/6 and system 7/8 are partial Phase 3 catalogs. A healthy target does not earn AC-14
without an observed controlled failure. Local controlled fault regressions separately prove
tester error classification/retention, not system storage/worker implementation.

**Verification corrections during this session:** the first controlled-failure regression
incorrectly expected five covered ACs; execution showed only four relevant assertions ran,
so the test expectation was corrected without increasing fabricated coverage. The first
non-isolated offline editable build lacked Hatchling's dynamically required `editables`
build tool; the normal isolated `uv sync --locked --group dev` succeeded. The standalone wheel
was independently built and loaded outside the checkout. These are actual observed corrections.

**Remaining evidence:** real Team 9 integration, AWS/HTTPS and Team 12 both-direction runs;
shared workbooks, two-member/manual prose review, Excel repair-warning checks, real-world
precision/preservation experiments and UI accessibility. Office CFB encryption and additional
unsupported/ambiguous object fixtures are explicit remaining fixture scope. Jackson must
confirm current remote CI/protection state and shared profile defaults. No new remote CI run or
branch-protection setting was performed here. Human reflection remains the student's own task.

**GenAI contribution/verification:** Codex performed the comparison, implementation, test
regressions, local processes, artifact packaging, dependency scan and final requirement audit.
The user directed the scope and requested the final handoff verification. Code and claims were
checked against controlled HTTP responses, actual socket behavior, saved report bytes and real
command output; the report distinguishes external/manual evidence from local implementation.


## Windows CI and tester coverage follow-up (2026-10-14)

At `ae40ed9`, [Windows CI](https://github.com/JGraff24/30861_Team9/actions/runs/38624661749/job/116060646470?pr=2)
reported 224 passed / 1 failed: the late-DNS test timed out before entering DNS and then waited
for a resolver that never ran. The supplied traceback and public job metadata agree. Ubuntu
checks and the dependency audit passed on that run. The test now isolates the DNS wall timeout
from filesystem preparation using the injected clock, checks that DNS actually started, and
releases its worker in `finally`. Ordinary and deliberately slow preparation both pass locally;
real-clock streaming setup/suite tests remain enabled. No production deadlines were loosened.

The review's two gaps are also fixed: baseline findings/prose are saved from a valid completed
audit before accuracy assertions, and authorized reviewer version/audit GETs are in the plan.
Regressions cover consistently incorrect findings that still repeat, unstable incorrect findings,
missing/foreign reviewer grants and wrong reviewer response IDs. Existing foreign-user denial
checks remain in the suite. Models were checked; higher-level state/architecture nodes are
unchanged, and suite descriptions/counts were updated.

| Actual check | Outcome |
|---|---|
| Final local pytest | 234 passed, 4 unconfigured live checks skipped; existing deprecation warning |
| Local process suite, manifest 1.4.0 | 53 planned, 52 executed/pass, 0 fail/error, 1 expected skip; HTTP 200 in 11.2 s |
| Configured local acceptance | 4 passed in 22.35 s |
| Ruff, strict mypy, schemas, fixture hashes and factory process smoke | Passed |
| New Windows/GitHub run | Pending; no push or rerun performed in this session |

The retained [report](dixon_local_selfcheck/report.json) and
[run/operation/AC record](dixon_local_selfcheck/run-record.json) now include both reviewer cases.
The fake code measure remains explicitly 40/50; component/system coverage is still partial 6/6
and 7/8. These are local fake-target results, not real Team 9/Team 12 evidence. Codex performed
this follow-up from the user's CI traceback and review, without subagents or deployment.
