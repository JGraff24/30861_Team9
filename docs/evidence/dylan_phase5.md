# Dylan — Phase 5 engine evidence

Executed November 15 2026 on macOS 15.5 arm64 (8 CPUs, 8 GiB), CPython 3.12.3, with the
locked development environment. Code revision `f6a9250` on `feature/dylan-phase5-engine`
(base `main` `ca6becb`). Everything below is **local** evidence from the real production
code paths. No hosted system, Team 12 system, AWS resource, shared pack file or adoption
record was contacted or changed. See [the implementation note](../implementation/dylan_phase5.md)
for what was built.

## Summary of status

| Item | Status |
|---|---|
| Scheduled audits reuse the engine deterministically, preserving source bytes and history | **Verified locally** on `main`, and on a trial merge with Jackson's PR #17 (`842c1e7`) |
| AC-18 local performance and repeatability (20 runs at production bounds) | **Measured locally**: 20/20 SUCCESS, identical findings per workload, max end-to-end 19.9 s |
| AC-18 hosted runs (P5-PERFORMANCE-RUN-1..5, COMPARE) | **Not run here**: Dixon's tester against the deployment |
| AC-09 Excel no-repair inspections (P4-MANUAL-EXCEL-*, P5-MANUAL-EXCEL) | **Packet prepared; inspections NOT performed** (manual, Dylan) |
| Five-real-workbook precision study (P4-MANUAL-REAL-*, P5-MANUAL-REAL-PRECISION) | **Harness ready; study NOT performed** (workbook selection and human adjudication) |
| Team 12 rename mismatch | **Open**; see [engine analysis](phase5/dylan_rename_engine_analysis.md). Expectations unchanged |

## Checks executed

| Check | Result |
|---|---|
| `pytest -q` (full regression, after the first run was invalidated, see below) | **1021 passed, 7 skipped**, 0 failed, 133 s. Skips: 4 need live tester credentials, 2 need Linux RLIMIT_AS, 1 is the scheduled-audit module waiting for Jackson's backend |
| `pytest tests/unit/test_dylan_phase5_*.py` on `main` | 13 passed, 1 module skipped (scheduled audit, explicit reason) |
| Same files plus the scheduled module on a local trial merge with `origin/codex/jackson-phase5-backend` (`842c1e7`) | **11 passed** (3 scheduled-audit + 8 engine); the evidence-tool tests were added afterwards and run on `main` only |
| `ruff check .`, `ruff format --check .`, `mypy --platform linux app tester scripts` | Passed (168 files formatted; 105 source files typed) |

The first full run reported 55 failures, all SQLite `OperationalError` in tester/session
tests. The host disk was at 100% (about 255 MiB free). The same files passed on a pristine
`main` checkout and in this checkout once space recovered, and the complete rerun above is
green. Those failures were environmental, not caused by this change, which modifies no
production code. **Free disk space on this machine before further work.**

## Scheduled re-audit reuse

- The evaluation of every synthetic Phase 4 fixture plus Team 12's `clean.xlsx` is
  byte-identical in-process and in fresh interpreters with `PYTHONHASHSEED` 0, 1 and
  4294967295.
- No engine module imports clock, randomness, locale or environment sources.
- Real stack, manual path: repeated re-audits reproduce status, findings, limitations
  and review context. The first audit's record, report download and source bytes stay
  byte-identical, and history keeps every audit.
- A generated newest version re-audits like its publication audit.
- Retained ruleset 1.0.0 audits are untouched by a 2.0.0 upgrade.
- Real stack, scheduler path (trial merge): a scheduled audit equals the manual audit
  (findings, limitations, status, review items, report findings). Coalesced missed
  intervals add exactly one equal audit. A scheduled audit of a generated newest version
  equals its publication audit.
- Real stack, scheduler fault: `TEST_SCHEDULED_AUDIT_FAILURE` fails cleanly. Details are
  `{stage: scheduled_audit, testOnly: true}`, with no findings and empty `checksPerformed`.
  Earlier bytes are unchanged, and the next interval succeeds with identical findings.

## AC-18 local performance and repeatability

Source: [`phase5/dylan_ac18_local.json`](phase5/dylan_ac18_local.json)
(`scripts.ac18_performance`).

- Production bounds: 60 s audit, 30 s validation, 15 s queue, two worker slots.
- Five sequential runs per workload and mode on an otherwise idle local system.
- Real monotonic time; no simulated clock.
- "Measured" means an isolated test session with per-session code measurement, which is
  how hosted tester namespaces run.
- RLIMIT_AS memory limits are not enforced on macOS.

| Workload (SHA256 prefix) | Mode | Outcomes | Intake validation s | Queue s | Engine s | End-to-end s | Findings identical |
|---|---|---|---|---|---|---|---|
| `cells`: tester boundary generator, 1 sheet × 250,000 cells, 6,906,151 B (`2e48fede`) | ordinary | 5 SUCCESS | 1.49–1.81 | 0.003–0.185 | 4.51–5.31 | 6.22–7.18 | Yes (1 TABLE_HEADER) |
| same | measured | 5 SUCCESS | 4.30–4.67 | 0.030–0.156 | 14.05–15.07 | 18.48–19.90 | Yes, same digest as ordinary |
| `mixed`: 50 generic sheets × 5,000 cells, headers/links/merges, 6,530,748 B (`93ffe150`) | ordinary | 5 SUCCESS | 1.40–1.61 | 0.006–0.151 | 4.06–4.37 | 5.54–6.13 | Yes (200 findings: 50 each LINK_TEXT, MERGED_CELLS, TABLE_HEADER, WORKSHEET_NAME) |
| same | measured | 5 SUCCESS | 4.25–4.70 | 0.013–0.171 | 13.72–14.00 | 18.10–18.82 | Yes, same digest as ordinary |

Observations:

- All 20 runs finished well inside 60 s. Queue time stayed under 0.2 s because each run
  was alone. This does not measure queueing behind concurrent suites.
- **Code measurement roughly triples validation and engine time** (engine ≈ 4.5 s →
  ≈ 14 s). On a slower hosted CPU, or with two concurrent measured suites, the 60 s bound
  has less headroom than the ordinary numbers suggest. Hosted P5-PERFORMANCE runs must
  report this split. Dixon's November 12 observation of 41.9–46.5 s end to end for the
  same `cells` workbook came through the full HTTP tester on another run and is not
  directly comparable with these in-process numbers.
- The 25,000,000-byte `bytes` workload was **not run**: with about 255 MiB free, five
  25 MB uploads plus child copies risked disk-full failures that would have been
  misrecorded. Run `--workload bytes` once disk space is available.

### Controlled-failure path (not AC-18 success evidence)

Source: [`phase5/dylan_ac18_controlled_timeout_demo.json`](phase5/dylan_ac18_controlled_timeout_demo.json).
This uses the same `cells` workbook with the audit bound lowered to 2 s
(`productionBounds: false`). All 5 runs ended as PROCESSING_FAILED with an error
envelope in 3.8–4.2 s, and none reported findings. The harness correctly classified
them as CONTROLLED_FAILURE, never SUCCESS.

**Defect found (outside my area, reported, not changed):** 2 of 5 runs recorded
`PROCESSING_TIMEOUT`, but 3 recorded `PROCESSING_FAILED` with stage `audit`.

- Cause: `app/jobs/execution.py` `execute_bounded` catches only `ProcessLookupError`
  around `os.killpg`. On macOS, `killpg` returns EPERM (`PermissionError`) when the
  group's only member has exited but not yet been reaped. That is exactly the race when
  the guarded child stops at the deadline.
- Reproduced directly: `killpg` of an exited, unreaped `start_new_session` child raises
  `PermissionError(1)`.
- Effect: a correct, controlled failure with no data loss, but the wrong error code on
  macOS hosts.
- Linux (the production image) should allow signalling a zombie, so production timeouts
  are expected to be unaffected. **Not verified on Linux here.**
- Suggested owner fix: also tolerate `PermissionError` in those two `killpg` calls.

## Excel inspection packet (AC-09)

Source: [`phase5/excel_inspection/`](phase5/excel_inspection/) (`manifest.json`,
`INSPECTION_RECORD.md`, 10 committed outputs).

Each of the 11 supported-correction plans in `expectations-phase4.json` was run through
the real local system:

- alt ×4: alt, preserve, large, mixed
- link ×2
- new-table header
- enabled header
- rename with every reference kind
- combined four-family batch
- no-edit batch

All 11 were REMEDIATED. The tester's independent comparator found **zero unexplained
differences** in each, and every declared output hash equals the downloaded bytes. Two
generations produced byte-identical outputs. The 3 MB `large_preservation` output is not
committed; its hash is in the manifest and it regenerates deterministically.

**Not done: nobody has opened these files in Microsoft Excel.** Every
reviewer/date/version/repair-prompt field is PENDING.

## Precision study

`scripts.real_workbook_precision` is ready and tested on synthetic workbooks only. The
synthetic verdicts used in its unit test exercise the arithmetic and are **not** study
results. No real workbook has been audited for the study and no precision figure exists yet.

## Still requires people (none of these is claimed as done)

1. **Excel inspections (Dylan).** Open each output in `phase5/excel_inspection/` in
   Microsoft Excel (16.115 is installed on this Mac). Record the version, platform, date,
   repair-prompt observation and effect checks in `INSPECTION_RECORD.md` /
   `manifest.json`, and confirm each file's SHA256 first. Do not save the files. Spec §17
   also needs this for Team 12's outputs; that is their record.
2. **Precision study (Dylan plus adjudicators).** Choose at least five de-identified
   real workbooks and run `prepare`. Adjudicate every finding TP/FP with the adjudicator's
   name, and record Microsoft Accessibility Checker results (Review > Check
   Accessibility) per finding and in `checker_only.csv`. Then run `score`. Precision of
   at least 90% is the pass condition; rules with no findings are reported as not estimable.
3. **Rename investigation.** Get Team 12's failing report, run Dixon's probe against
   Team 12, do Excel step R1, and record the pod decision (see the analysis).
4. **Hosted AC-18.** Dixon's P5-PERFORMANCE-RUN-1..5 and COMPARE against the deployed
   system, reporting queue and engine times separately. Also run the `bytes` workload locally.
5. **Fixture review.** Two Team 12 members review Team 9's answer keys
   (P5-MANUAL-FIXTURE-REVIEW). That is outside the engine but is still open.
