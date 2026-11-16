# Dylan — Phase 5 workbook processing and audit engine

Current integration: PRs #16–#19 are combined for the Phase 5 main release. Hosted testing
requires deployment of both the new system/website with its worker and the independent
tester. See the
[Phase 5 integration record](../evidence/phase5_integration.md) and
[Class Access Guide](../Team9_Class_Access_Guide_Phase5.md) for the current assembled
configuration and fresh evidence. The contribution-specific results and handoff boundaries
below retain their original revision context.

Target: specification 3.0.0, ruleset 2.0.0 (unchanged), pack 3.0.0. Base: `main` at
`ca6becb` (the reviewed Phase 4 baseline). This contribution changes **no production
code**: no rule, finding identity, finding target, correction profile, tint policy,
expectation or fixture. It adds verification of the engine's Phase 5 obligations, tools
that produce the required engine evidence, and the evidence itself. Results and the
remaining human work are in [the evidence note](../evidence/dylan_phase5.md).

## Scope

| Obligation (spec 3.0.0) | Delivered here | Owner of the rest |
|---|---|---|
| §7 scheduled audits use the same engine, status derivation and failure containment; fresh audit; older audits/reports preserved; no new version | Determinism and history tests (below); verified against Jackson's scheduler on a local trial merge | Jackson: scheduler, clock persistence, faults; Ryan: HTTP clock route |
| §2/§17 open rename mismatch | Engine-side analysis and `scripts.rename_inventory` diagnostic; expectations preserved | Pod decision; Team 12 evidence; Dixon's probe |
| §17 AC-09 Excel no-repair checks for every supported correction profile | `scripts.excel_inspection_packet` generates real outputs, hashes, effects, an independent comparison and a PENDING record | **Dylan in Excel** (manual) |
| §17 five real workbooks, ≥90% adjudicated precision, Microsoft checker comparison | `scripts.real_workbook_precision` prepare/score harness | **Dylan** selects the workbooks and **people** adjudicate them |
| §17 AC-18 five runs, controlled outcomes, identical findings, queue vs engine reported separately | `scripts.ac18_performance` with real local measurements | Hosted runs: Dixon's P5-PERFORMANCE cases |

## Why no engine change was needed

Scheduled audits reach the engine through the same `AuditWorker` path as manual audits,
which always starts a fresh bounded child process. Reusing the engine is therefore safe
only if the evaluation of fixed bytes cannot depend on the process, the clock or the
environment. I checked this rather than assuming it:

- `analyze` sorts findings by (ruleId, worksheet, target) and sorts limitations. Sets
  inside the rules are used only for membership tests or are sorted before output.
- No module in `app/audit` or `app/workbooks` imports `time`, `datetime`, `random`,
  `uuid`, `os`, `locale` or calls `hash()`. A simulated namespace clock therefore cannot
  change findings, and Phase 5 needs no clock parameter on the engine port.
- Finding IDs depend only on (source SHA256, ruleset, rule, worksheet, target). A
  scheduled audit of the same stored version therefore produces the same IDs as the
  manual audit, which is what reminder eligibility and history comparisons depend on.

## Tests added

`tests/unit/test_dylan_phase5_engine.py` (runs on `main`):

- The six-rule evaluation of every synthetic Phase 4 fixture, plus Team 12's received
  `clean.xlsx`, is byte-identical in-process and in child interpreters with
  `PYTHONHASHSEED` 0, 1 and 4294967295.
- An AST guard keeps clock, randomness, locale and environment imports out of the engine.
- Real API/SQLite/filesystem/worker-child tests:
  - manual re-audits reproduce status, findings, limitations and review context;
  - earlier audit records, report downloads and source bytes stay byte-identical;
  - history lists every audit in order;
  - a generated (remediated) newest version re-audits exactly like its publication audit;
  - a retained ruleset 1.0.0 audit stays byte-identical when the installed ruleset
    becomes 2.0.0, and its 409 `LEGACY_REPORT_UNAVAILABLE` stays the same.

`tests/unit/test_dylan_phase5_scheduled_audit.py` (skipped with an explicit reason until
`app.services.lifecycle` exists; Jackson's PR adds it):

- A scheduler-dispatched audit reproduces the manual audit's findings, limitations,
  status, review context and report findings, adds no version and keeps earlier bytes.
  Coalesced missed intervals produce one more identical audit.
- The scheduler following a generated newest version audits it like its publication audit.
- `scheduled_audit_failures`: `TEST_SCHEDULED_AUDIT_FAILURE` with stage
  `scheduled_audit`, no findings, empty `checksPerformed`, earlier audit/report/source
  unchanged. The next interval succeeds with identical findings.

`tests/unit/test_dylan_phase5_evidence_tools.py`: the evidence tools themselves
(successful runs versus controlled timeouts, refusal to score unadjudicated or altered
studies, PENDING human fields, rename inventory of the shared fixture).

## Evidence tools

All tools use the real production stack through `scripts/engine_evidence.local_system`:

- in-process API (no network listener)
- SQLite metadata
- filesystem object storage
- production engine
- production bounded child-process runner

They never contact a hosted system, never modify the shared pack, and label their output
as local evidence with the git revision and host.

- `scripts/rename_inventory.py WORKBOOK [--apply NAME]` — rename classification,
  blockers, reference sites by kind, the stricter-x14 counterfactual, and optionally the
  real correction with preservation and output re-audit.
- `scripts/excel_inspection_packet.py --output DIR` — one output per supported
  correction plan in `expectations-phase4.json`. It records source/output/declared
  hashes, approved decisions/effects, the tester's independent comparator result, the
  output audit and a Markdown record whose human fields are PENDING. Outputs are
  deterministic: they were identical across repeated generations.
- `scripts/real_workbook_precision.py prepare|score` — five bounded audits per real
  workbook (P4-MANUAL-REAL-DETERMINISM), an adjudication CSV bound to the engine output
  by a digest, checker comparison columns, and per-rule/overall precision. A rule with no
  findings is "not estimable". It refuses to score pending, unattributed or altered rows,
  and never stores workbook paths.
- `scripts/ac18_performance.py` — five sequential runs per workload through the
  production worker loop (`run_workers`, two slots). It times the following with the
  monotonic clock by wrapping production calls in-process only:
  - intake validation
  - upload request
  - queue (acceptance → claim)
  - engine child
  - worker job
  - acceptance → terminal
  - end to end

  Outcomes are SUCCESS, CONTROLLED_FAILURE or UNCONTROLLED. Successful findings are
  compared by digest. `--audit-timeout-seconds` can only lower the bound, to demonstrate
  the controlled-timeout path, and is labeled `productionBounds: false`.

## Interfaces relied on (unchanged)

- `CorrectionEngine`/`WorkbookEngine` ports
- `AuditWorker.run_once`, `run_workers`
- `execute_bounded`
- the business routes 2–10 and S/V/R
- Jackson's `LifecycleService.advance_clock(principal, session, {"currentTime"})` and the
  setup keys `faults.scheduled_audit_failures` / `clock`

No coordination was required to add these checks. If Jackson's or Ryan's interfaces
change, only the skipped scheduled-audit module needs updating.
