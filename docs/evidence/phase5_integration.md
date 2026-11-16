# Phase 5 integration and verification

Prepared November 16, 2026 for Jackson's request to combine the four Phase 5 PRs,
run checks and the system suite, update documentation and create a Phase 5 class guide.
Hosted testing was subsequently deferred at Jackson's explicit request because of the
current Wi-Fi connection. This record distinguishes local source verification from
hosted release and human acceptance. Jackson subsequently instructed that completion
remain local: do not merge into main or push. The four GitHub PRs are left open.

## Integrated inputs

| PR | Contribution | Exact head integrated |
|---|---|---|
| [#16](https://github.com/JGraff24/30861_Team9/pull/16) | Dixon: independent tester and profiles | `f996909033335df2e84bb767a83e00617b38af94` |
| [#17](https://github.com/JGraff24/30861_Team9/pull/17) | Jackson: backend lifecycle and persistence | `842c1e7665915ce98683e8e3a865a034ff5f67ec` |
| [#18](https://github.com/JGraff24/30861_Team9/pull/18) | Dylan: engine verification and evidence tools | `cde9f16ad0c24d421f21bbca69bf8044a1bcb0b0` |
| [#19](https://github.com/JGraff24/30861_Team9/pull/19) | Ryan: clock, measured execution and UI | `7c25eb9cdb3dcd9043b12413760f3541299bbcbb` |

Base: main `ca6becbefc32ed1462be2dfee54a8230c7361e8a`. Local branch:
`integration/phase5`. All four heads are preserved in the merge ancestry; #19 already
includes #17. The combined source plus integration fixes is commit
`0b6312b` (documentation/evidence are added afterward).

The supplied `/Users/jacksongraff/Downloads/Phase5_Agent_Context_Pack.zip` has SHA256
`903ee2c80aef678567e6ef13570b28931112127d27c698adf85dab806792d990`.
Every one of its 21 files matches `docs/contracts/phase5/` byte for byte. The pack's
instructions were treated as reference requirements, not as additional authorization to
publish, deploy, claim adoption, edit expected findings or contact another team.

## Integration decisions

- Resolved the three #16/#17 merge overlaps in nginx, the deploy probe and its test.
  Retained Jackson's 2,700-second proxy/protection settings and the compatible Phase 4/5
  private release probe. No audit rule, expected finding, fixture bytes or shared pack changed.
- Enabled the isolated test clock and 2,400-second run budget in the Phase 5 real integration
  launcher. The imported launcher otherwise left the clock off and selected 1,800 seconds.
- Made the source/default and production tester select the actual Phase 5 395-case manifest.
  Production fixes tester identity to `team9`; exact target-team classification remains
  trusted private server configuration. Installed-wheel checks verify the Phase 5 schemas,
  manifest, expectations and design map as well as retained Phase 4 assets.
- Kept the retained Phase 3 tester fixture and fake self-check at an explicit 600-second
  budget after changing the global default to Phase 5. The deadline-abort regression still
  checks retained error/skip reporting and refusal of a second run.
- Made fail/error outcomes block the real Phase 5 CI job. Removed the old real Phase 4 job
  against this upgraded target: frozen suite 2.1.1 requires report label 2.1.0, while new
  Phase 5 reports must say 3.0.0. The Phase 5 run preserves the first 253 inherited cases
  and validates their new reports against the correct pack. Phase 4 policy/manifest checks,
  its fake tester self-check and retained-report regression tests remain.
- Updated the README, deployment/restore/reciprocal/fixture documentation and created the
  Phase 5 Class Access Guide in Markdown and a five-page tagged PDF with bookmarks.
  Historical owner evidence, earlier guides and reports retain their original context.

The workspace initially had a malformed duplicate tracking ref `origin/HEAD 2`, which
prevented fetching. Its contents were backed up at `/tmp/team9-origin-HEAD-duplicate.backup`
before removing that duplicate. No source branch was deleted. The existing `.venv` pointed
to a missing temporary Python installation; restored pinned uv 0.13.1 and Python 3.12.15,
then installed the locked dev dependencies.

## Fresh verification

Execution host: macOS arm64, Python 3.12.15. Optional UI tools: Playwright 1.58.0,
Chromium 145.0.7632.6 and axe-core 4.11.0. Temporary tools do not alter the dependency lock.
All tests use private temporary credentials; no credentials are included in evidence.
The launcher records a dirty working tree because deployment and fixture Markdown files
were being updated. `integration-2026-11-16/source-manifest.json` pins all 254 tracked
runtime/build/fixture files and identifies those documentation-only differences; application,
tester, scripts, test code, dependencies and workflows match the recorded source commit.

All checks below completed. Fresh runtime commands returned exit status 0; skips and platform limits remain explicit.

| Check | Fresh result |
|---|---|
| Locked resolution; Ruff lint/format; strict mypy (`app tester scripts`) | Passed; 117 typed source files |
| Shared pack validator | Passed: hashes, 59 definitions, 26 valid / 21 invalid examples, 435 assertion entries |
| OpenAPI export freshness, identity vectors, Phase 4/5 policy, Phase 4/5 manifest, coverage catalogs, synthetic fixture regeneration | Passed |
| JavaScript syntax | Passed: `node --check app/ui/static/app.js` |
| Locked runtime + measurement dependency audit | Passed: no known vulnerabilities |
| Process smoke | Passed: actual system/tester processes, docs, health, auth and disabled clock |
| Full pytest regression | **1,113 passed, 7 skipped**, 197.47 s; five unconfigured hosted checks and two Linux-only memory checks |
| Real Phase 5 independent suite | **392 pass / 0 fail / 0 error / 3 skip**, 487.535 s launcher wall time (below 2,400 s) |
| Browser and accessibility | **26 Phase 4 + 16 Phase 5 simulated scenarios and 2 real-stack flows passed**; **84 axe scans / zero violations**, minimum sampled text contrast **8.14:1** |
| Detailed measured AC-18 workloads | **15/15 SUCCESS** (five each: cells, mixed 50-sheet workbook, exactly 25,000,000 bytes), identical findings per workload, no controlled/uncontrolled failures |
| Legacy real HTTP and fake tester checks | Legacy real: **63 pass / 1 intentional skip**, plus **4 live acceptance pass / 1 profile-only skip**. Phase 3/4 fake self-checks passed. Phase 5 own/cross fake self-checks each passed **5 live acceptance tests**; all main reports have zero fail/error outcomes |

The initial restricted pytest run could not bind loopback sockets in five checks; two
release assertions also still expected the former default Phase 4 manifest. Those results
were investigated, the release assertions updated to Phase 5, and the full suite rerun with
local sockets available. They were not counted as passing evidence.

The own-system report records code **3,065/3,542**, component **11/12**, system
**17/18**, and model **19/23**. Its three skips are `P5-CLOCK-NOT-READY`,
`P5-FAULT-REJECT-DISABLED`, and `P5-TESTER-CROSS-OMIT`: the first two conditions are
covered in local target tests; the cross-only profile assertion is inapplicable to an
own-team report. All 395 planned IDs are present once. Five independent AC-18 boundary
runs and the successful-findings comparison passed. The run is loopback HTTP; its generic
performance message does not establish a tested HTTPS deployment.

[Own-system report](phase5/integration-2026-11-16/own/report.json),
[run record](phase5/integration-2026-11-16/own/run-record.json), and
[worker events](phase5/integration-2026-11-16/own/worker-events.jsonl) retain the original
results and provenance. [Static checks](phase5/integration-2026-11-16/static-checks.json),
[pytest XML](phase5/integration-2026-11-16/pytest.xml), and
[runtime command results](phase5/integration-2026-11-16/runtime-checks.json) provide the
fresh check evidence.

Browser artifacts retain the [Phase 4 scenarios](phase5/integration-2026-11-16/browser-phase4.jsonl),
[Phase 5 scenarios](phase5/integration-2026-11-16/browser-phase5.jsonl), and
[real API/engine/storage/worker flows](phase5/integration-2026-11-16/browser-real.jsonl).
The [accessibility summary](phase5/integration-2026-11-16/accessibility-summary.json) records
all 84 scans, analyzer/browser metadata, hashes, incomplete checks and contrast samples.
[Original raw scan bytes](phase5/integration-2026-11-16/accessibility-scans.tar.gz) are retained
in a verified compressed archive. Zero automated violations and sampled contrast do not
replace actual keyboard, native-control, assistive-technology and rendering inspection.

Detailed measured performance uses the real API, SQLite, filesystem storage, production
worker loop (two slots), measured engine children and real monotonic time. Workloads ran
sequentially after the suites/browser checks, with no competing test suite. The report keeps
validation, queue, engine, worker and end-to-end timing separately for every run. macOS does
not enforce Linux RLIMIT_AS; deployment resource evidence remains separate.

| Workload (five successful runs each) | Max queue | Max engine | Max end to end |
|---|---|---|---|
| tester boundary generator: one worksheet, exactly 250,000 numeric cells | 0.133 s | 12.893 s | 17.031 s |
| 50 generic worksheets x 5,000 cells (250,000), headers, links and merges | 0.235 s | 12.575 s | 16.560 s |
| tester boundary generator: clean workbook of exactly 25,000,000 bytes | 0.132 s | 0.583 s | 1.441 s |

[Full timing report](phase5/integration-2026-11-16/performance.json) includes input hashes,
source/host information, all outcomes and findings digests. The independent HTTP suite's
five performance runs separately took 17.1 seconds each end to end (upload included),
with approximately 3.9-second upload requests, and returned identical findings. These local
measurements make no claim about an untested HTTPS/S3 deployment.

## Individual PR CI findings

These GitHub results were inspected during integration; they are historical PR-head checks,
not CI for the combined branch:

| PR run | Result and relevant finding |
|---|---|
| [#16 / run 51](https://github.com/JGraff24/30861_Team9/actions/runs/43262663878) | Success on its tester-only baseline; its Phase 5 real job allowed target gaps |
| [#17 / run 52](https://github.com/JGraff24/30861_Team9/actions/runs/43263727190) | Failed: stale Windows smoke route count and Phase 4 report-version assertions; Ryan's smoke update and the integrated Phase 5 suite address those boundaries |
| [#18 / run 53](https://github.com/JGraff24/30861_Team9/actions/runs/43762255906) | Linux/Windows checks passed; dependency audit failed on the inherited dependency. Ryan's lockfile patch is included and the fresh locked audit passed |
| [#19 / run 54](https://github.com/JGraff24/30861_Team9/actions/runs/43932041204) | Linux Phase 4 integration returned 214 pass / 18 fail / 21 skip, with all 18 failures rejecting `specificationVersion: 3.0.0` under the frozen 2.1.0 report schema. Windows and dependency audit passed |

The integration uses Phase 5's inherited assertions and new-report schema rather than
relabeling current reports or weakening the frozen Phase 4 pack. Fresh combined Linux/Windows
CI still requires publishing this branch/PR; no such result is claimed here.

## Acceptance boundaries

| Requirement | Evidence boundary |
|---|---|
| AC-01–08, AC-10–11 | Inherited cases execute in the Phase 5 run; immutable history/preservation also exercised by local regression |
| AC-09 | Independent automated preservation is covered; Microsoft Excel no-repair inspections remain pending |
| AC-12 | Local backend/engine/clock tests plus independent cadence/version/coalescing checks; deployment scheduling still needs host evidence |
| AC-13 | Local/independent UTC reminder eligibility, deduplication and failure checks; UI disclosure verified separately |
| AC-14 | All isolated fault families and genuine local persistence/cleanup/restart checks; live object-store behavior remains a deployment task |
| AC-15 | Owner/reviewer/foreign-user/admin and namespace boundaries in local regression and independent suite |
| AC-16 | Local deletion, fencing, registered-copy purge and journal restore tests; live HTTPS/S3/IAM/snapshot inventory and restore rehearsal remain unverified |
| AC-17 | Automated browser/axe evidence is separate from the HTTP report; human keyboard/interface acceptance remains pending |
| AC-18 | Five independent supported boundary runs plus separately measured queue/engine/end-to-end workloads; hosted performance and the human five-real-workbook precision study remain separate |

Model coverage uses the unchanged **draft design map 0.1.0** with all 23 requirements.
It does not constitute team confirmation of that map. C9 and AC-17 are not appended to an
immutable HTTP report. Code coverage is measured child execution in `app/workbooks` and
`app/audit`; auxiliary namespace counts are not summed into the main session.

## Outstanding release and human work

- Hosted testing: deferred by Jackson; no hosted own-team or Team 12 reciprocal result.
- Docker, Linux memory enforcement and fresh Windows execution: unavailable on this macOS
  host. CI is configured to run the Linux image/runtime and Windows portable checks after
  publication. Earlier individual PR CI results are not combined-source proof.
- Private exact URL→team map, deployed manifest/revision, approved class access and 30/45/2,400
  setup/run registration must be confirmed before a graded hosted run.
- Confirm the design map and record pod adoption without modifying the immutable context
  pack's unfilled adoption template as if it were actual agreement.
- Complete Excel inspections, two other-team human fixture reviewers, human explanation and
  keyboard/interface checks, and the manually adjudicated real-workbook precision study.
- Verify live S3 version permissions, all retained backup/snapshot copies, deletion recovery,
  newest-journal restore fencing, deployment draining and monitoring response on the host.
- Retain the Team 12 rename investigation and its independent expectations until joint
  evidence resolves it. No rule/oracle was weakened in this integration.

No main-branch merge, push, cloud modification or deployment is claimed by local verification.
