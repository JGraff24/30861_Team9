# Ryan Phase 5 verification

Executed locally on Linux x86_64 / Python 3.12.15, November 16, 2026.
These results do not establish a deployed release, pod adoption or human acceptance.
All runs were local; no deployment or hosted reciprocal testing was performed.

## Local target and UI checks

The implementation branch starts at Jackson PR 17, `842c1e7`. The unchanged
backend baseline passed **1,038 tests, 4 skipped**. Ryan's final target regression
passed **1,063 tests, 4 skipped** in 251.58 seconds. The four skips require a
configured hosted tester. Two existing warnings concern Starlette's TestClient
transport and openpyxl's unsupported conditional-formatting extension.

Route-count assertions and the process smoke script now include the clock route.
The process smoke verified 17 system paths, three tester paths and the disabled-clock
response. The final full run includes the new HTTP deadline/body-bound checks and
measured scheduled-execution test. Expected business outcomes were not weakened.

- 25 focused Phase 5 clock/measurement tests passed, using actual API, SQLite,
  storage and worker children. Existing backend tests cover transaction limits,
  failures and deletion recovery.
- 26 retained Phase 4 simulated-browser scenarios passed at `/` and `/system`.
- 16 Phase 5 simulated-browser scenarios passed at both prefixes, including
  lost-save acknowledgment, active/pre-delete/partial/lost-response deletion,
  explicit recovery, invalid interval feedback and reviewer restrictions.
- Two complete real API/engine/storage/worker browser flows passed. They exercise
  four supported correction forms, separate output/source/report preservation,
  keyed lost-remediation recovery, historical selection, generated-version schedule
  selection, persisted settings, reviewer restrictions, delete/cancel and removal
  of old source/report/schedule/notification URLs while preserving another workbook.
- Ruff, format, strict mypy, JavaScript syntax, generated OpenAPI checks, identity
  vectors, Phase 4 policy/manifest and synthetic-fixture checks passed.
- Built a wheel and installed it outside the repository. The installed health,
  disabled clock response, actual clock OpenAPI entry and all Phase 5 UI assets
  passed a smoke check. Docker was not available; no container-runtime claim is made.
- Runtime/measurement dependency audit initially found CVE-2026-126360 in inherited
  `opentelemetry-api` 1.45.1. The lockfile patch to 1.45.2 leaves **no known
  vulnerabilities** in the repeated pinned runtime/measurement audit.

## Accessibility

Retained **84 axe-core 4.11.0 scans with zero violations**: 68 simulated UI states
and 16 real-stack states, using Playwright 1.58.0 / Chromium 145.0.7632.6. Playwright
used its Ubuntu 24.04 fallback binary on this Ubuntu 26.04 host. Scanned tags are
WCAG 2 A/AA and WCAG 2.1 AA. Product CSP remained enabled.

See `phase5/ryan_accessibility_summary.json` and the exact raw JSON records in
`phase5/ryan_accessibility_scans.tar.gz`. The summary retains hashes, incomplete
checks and computed contrast samples. Some textarea/dialog text was marked
incomplete by axe's contrast rule; computed foreground/background samples all
exceeded 4.5:1 (minimum approximately 8.14:1). This does not replace inspection of
actual rendering, focus, native controls or assistive-technology behavior.

Native-dialog Tab/Escape/cancel/focus assertions ran automatically. Actual human
keyboard/interface inspection is **PENDING**, with steps in
`phase5/ryan_manual_validation.md`. C9/AC-17 credit is not appended to Dixon's
immutable HTTP suite report.

## Combined candidate

A separate local checkout combines Jackson PR 17, Dixon PR 16 (`f996909`) and
Dylan PR 18 (`cde9f16`) with Ryan's overlay. Integration conflicts in the proxy,
deployment probe and their test retained Jackson's 2,700-second protection and
compatible Phase 4/5 probe. Dixon's integration launcher needed two configuration
corrections for its Phase 5 mode: explicitly enable the test clock and select the
2,400-second tester timeout. These local integration resolutions do not alter the
shared pack or independent expected outcomes.

The pack validator passed its hash manifest, 59 schemas, 26 valid examples,
21 rejected negative examples and 435 normative assertion entries. This is artifact
consistency, not adoption or executable acceptance.

The combined regression passed **1,115 tests, 5 skipped** in 379.62 seconds. All
five skips require a configured hosted tester. Combined Ruff, formatting, mypy
(117 source files), schema, Phase 5 policy, manifest and coverage-catalog checks
passed. The newly imported simulated tester exposed inherited typing issues:
non-byte multipart payload narrowing and two Phase 5 fake-state narrowing cases.
These were fixed without changing expected workbook results. Both full own-team
and cross-team fake runs passed again after those fixes (2 tests, 13.58 seconds).

The real independent own-team suite 3.0.0 completed **392 pass / 0 fail / 0 error /
3 skip**. The skips are `P5-CLOCK-NOT-READY`, `P5-FAULT-REJECT-DISABLED`, and
`P5-TESTER-CROSS-OMIT`; their conditions are unavailable/inapplicable in this
configured run. Local target tests cover not-ready and disabled-capability behavior.
The launcher-to-report wall-clock window was approximately 896 seconds, including
startup/prechecks, within the 2,400-second run budget. This window is an upper-bound
observation, not an additional simulated or monotonic per-test measurement.

Its five AC-18 runs all passed, with identical issue types/locations and observed
end-to-end times of 35.9, 30.7, 30.4, 33.9 and 35.9 seconds. The report's generic
performance-comparison message mentions a registered HTTPS path; this particular
run used loopback HTTP and provides **no HTTPS deployment evidence**. The stored
report is retained unchanged rather than rewriting that tester message.

Reported coverage is code 3,065/3,542, component 11/12, system 17/18 and model 19/23.
This is evaluated scope, not full acceptance. The design map remains Dixon's draft
0.1.0; UI/manual evidence is separate. Report, run record and worker events are in
`phase5/ryan_own_suite_*`. `ryan_http_candidate_manifest.json` pins the source overlay
used by that run; `ryan_source_manifest.json` pins the final primary implementation.

The combined candidate was recorded as local commit
`459d6b4f8a7b544c9117bcf4a1c3abedac5f5935`; it is not this PR's head. Its
integration-only adjustments enabled the clock and 2,400-second timeout in
Dixon's launcher, replaced a heterogeneous fake-state cleanup loop with explicit
dictionary cleanup, and asserted a configured due time before fake calendar
arithmetic. The common multipart bytes guard is included in this PR. These
adjustments changed no independent expected findings or shared policy vectors.

## Detailed measured execution

Dylan's `scripts.ac18_performance` ran against the clean combined candidate above,
with production bounds and code measurement enabled, using real API, SQLite,
filesystem storage and the production worker loop. **15/15 succeeded**, five per
workload, with identical findings for every repeated workload. No controlled or
uncontrolled failures occurred.

| Workload | Max queue | Max engine | Max end to end |
| --- | ---: | ---: | ---: |
| 250,000 cells | 0.203 s | 21.079 s | 27.520 s |
| 50-sheet mixed workbook, 250,000 cells | 0.143 s | 20.432 s | 26.742 s |
| Exactly 25,000,000 bytes | 0.185 s | 1.044 s | 2.366 s |

`phase5/ryan_performance.json` retains all five outcomes per workload, input hashes,
findings digests, host/revision and monotonic validation/queue/engine/worker/end-to-end
measurements. The combined regression had finished before these benchmark runs;
the two short fake-profile retests finished before benchmarking as well. No
simulated calendar time contributes to these durations. This is local performance
evidence, not a claim about an untested hosted deployment.

Reproduce from the combined checkout:

```sh
python -m scripts.ac18_performance --workload cells --workload mixed --workload bytes --mode measured --output /tmp/ryan-performance.json
```

## Evidence boundaries

Target code coverage remains real child line sets in `app/workbooks` and `app/audit`;
API, persistence and UI are explicitly excluded. Scheduled work is measured after
execution, not on clock acceptance. Other/auxiliary namespaces are not summed into
the main session. All performance durations use real monotonic time.

No human Excel inspection, other-team fixture review, real-workbook precision
adjudication, hosted reciprocal run, live S3/IAM/backup deletion or deployed
monitoring validation was performed. Those remain separate team/release gates.
