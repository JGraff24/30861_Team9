# Phase 4 integration review

This record supersedes the separate owner documents' pre-integration blockers. It
does not rewrite their historical test results or retained audit/tester reports.
The integrated source includes PRs #8 (Jackson), #10 (Dylan), #11 (Ryan) and #9
(Dixon), followed by integration fixes. Main is held until exact-head CI passes.

## Target and changes

The supplied context pack revision and suite manifest are **2.1.1**. Specification
**2.1.0** and ruleset **2.0.0** remain unchanged. Pack members retain their supplied
hashes. Historical Phase 3 audits, reports and independent oracles retain their
original versions and results.

- Integrated all thirteen business routes, immutable audits/reports, versioned
  storage, durable remediation, session measurement, independent tester and UI.
- Fixed isolated-session idempotency replay: blob prefixes no longer overwrite
  the raw session namespace used by the transactional idempotency key.
- Corrected independent rational HSL tint arithmetic and retained all seven pack
  vectors, including `336699` → `1A334D` / `8CB3D9` and discriminating half-channel
  fixtures. Expectations do not call production color helpers.
- Hardened worksheet-reference inventory, conservative unsupported-reference
  handling, inherited color assessment and OOXML preservation of comments,
  processing instructions, relationships, table metadata and unapproved edits.
  Known x14 conditional formulas participate in safe worksheet renaming;
  conditional visual rendering has an explicit unevaluated/manual limitation.
- Expanded the independent suite from 215 to 253 individually reported cases;
  fixed decision error oracles, preservation comparison, disclosure assertions
  and deterministic limitations. Accepted Phase 4 tester sessions persist their
  exact fixture bytes separately from mutable progress rows.
- Fixed cross-workbook UI selection and new-version destination feedback while
  retaining audit-keyed unsent decisions and read-only reviewer behavior.
- Fixed the production Docker build's missing bundled wire schema. Production
  selects the corrected installed suite, ruleset and designated isolated-fault
  capability while retaining credentials, namespaces, S3 configuration and mounts.
- Deployment now drains and stops writers before backup/migration, probes the new
  release privately before ingress, and restores database ownership/permissions
  during pre-publication rollback. Once ingress may have opened, recovery retains
  new data and the pinned image, even if main advances. Durable publication markers
  prevent an unsafe rollback after a lost proxy-start acknowledgement.

## Verification scope

Fresh automated evidence from the integrated source:

- Final combined regression run: **968 passed, 6 skipped**. Four skips require
  configured live tester credentials (exercised by the real HTTP self-checks);
  two Linux memory-cap checks skip on macOS and run in Linux CI. An initial
  sandboxed run could not bind sockets; the complete rerun with loopback access
  passed without changing assertions.
- Real local HTTP/API, independent tester, production worker, engine, SQLite and
  filesystem storage: **253/253 Phase 4 cases passed**, plus **4 live tester
  acceptance tests**. API restart and retained source/report checks passed.
- Legacy real HTTP self-check: **63 passed, 1 intentional observational-fault
  skip**, plus **4 live tester acceptance tests**.
- Browser automation: **26 simulated cases** and **2 real workflows** passed,
  covering both root and `/system`, four corrections, declined findings,
  lost-acceptance replay, downloads, provenance, authorization, stale audit
  refresh and cross-workbook version destination. Simulation is distinct from
  actual engine/API/worker behavior in the two real workflows.
- Focused engine/HTTP tests: **120 passed**. Tester/legacy/contract tests:
  **191 passed**, plus **3 real socket deadline tests**. Deployment tests:
  **46 passed**, including a copied-source wheel build and actual SQLite
  Phase 3 migration/rollback; Docker/host commands in those tests are simulated.
- Ruff, formatting, strict mypy, OpenAPI freshness, fixture generation, manifest,
  identity and corrected policy vectors passed. Locked runtime dependency audit
  reported no known vulnerabilities.

CI reruns the combined suite, both real HTTP self-checks, portable Windows checks
and dependency audit. It also builds and smokes the actual installed production
Docker image; no Docker daemon was available locally. The Phase 4 HTTP report is
retained as a CI artifact. Temporary local reports contain automated observations,
not fixture-review approvals.

### CI follow-up

The first Linux run passed **970 regression tests** (four live-configuration
skips) and the legacy real HTTP workflow; Windows passed **318 tests** and the
dependency audit passed. Its Phase 4 run returned **246 passes, two controlled
audit errors and five dependent skips**, so main remained unchanged. A diagnostic
follow-up also passed the actual production Docker build and installed-image smoke.

A focused slow-worker simulation reproduced a scheduling defect in the tester:
accepted boundary-size intake jobs were still running when unrelated accuracy
uploads entered their 15-second queues. The tester now polls each accepted
boundary audit to completion as preparation before continuing. It keeps the
existing 80-second polling bound, reports genuine boundary audit failures, and
awards no coverage for preparation. Finding expectations, deliberate concurrency
checks, Phase 3 execution and the system's queue bound are unchanged. Both new
regressions and the affected tester/contract suite passed (**193 tests**). Fresh
exact-head CI and real-system verification remain the release gate.

## Limits and release handling

Jackson explicitly excluded fixture reviews and fixture-review records. They were
not performed and are not claimed as acceptance evidence. No Team 12 hosted run,
manual keyboard/screen-reader assessment or live-host migration/rollback was
performed during integration. Browser tests do not establish manual accessibility
conformance. HTTP coverage remains scoped to the engine measurement inventory;
separate browser evidence does not inflate the HTTP report's coverage counters.

The system database uses additive schema 2 migrations from PR #8. The independent
tester adds its immutable fixture-plan snapshot table. Existing rows and retained
reports are preserved. See [deployment and recovery](../../deploy/AUTODEPLOY.md)
for the one-way publication boundary and first-upgrade recovery procedure.

No host or AWS deployment command was run during this review. The existing CD
poller can activate main only after successful push CI for that exact SHA. An
integration PR's green CI is a merge gate; it does not prove that the host has
updated or establish full Phase 4 acceptance.
