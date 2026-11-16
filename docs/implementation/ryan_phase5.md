# Ryan — Phase 5 testing interface, measured execution and UI

Current integration: PRs #16–#19 are combined for the Phase 5 main release. Hosted testing
requires deployment of both the new system/website with its worker and the independent
tester. See the
[Phase 5 integration record](../evidence/phase5_integration.md) and
[Class Access Guide](../Team9_Class_Access_Guide_Phase5.md) for the current assembled
configuration and fresh evidence. The contribution-specific results and handoff boundaries
below retain their original revision context.

Implements Ryan's Phase 5 testing-interface and UI scope on Jackson's backend
PR #17 (`842c1e7`), against specification 3.0.0 and ruleset 2.0.0. The shared
specification/context pack and independent workbook expectations are unchanged.

## Clock and session boundary

`POST /api/v1/test/clock` is registered in the actual system OpenAPI catalog.
The trusted clock capability and namespace-presence gate precedes authentication;
then the creating test administrator and READY/legacy session are checked before
bounded strict body parsing. The route delegates to `LifecycleService.advance_clock`
and Jackson's transactional ownership/state recheck. It returns only the specified
clock response or the four-field ProjectError, with a request ID. The HTTP deadline
is ten real seconds. Timeout may hide a committed transaction: the caller can
explicitly repeat the same timestamp; the adapter never automatically retries.

System setup now uses the same strict bounded JSON parser as business routes,
rejecting duplicate keys before they can be collapsed into a dictionary. It keeps
the instructor `{message,source}` error envelope, including the 413 body limit.
The backend dependency supplies default/custom clocks, durable one-shot fault
counters, initialization, namespace cleanup, transactional scheduling and recovery.
These services were reused, not duplicated.

Real-stack tests cover capability/authentication precedence, malformed and oversized
requests, unknown/cleaned/foreign/not-ready/legacy sessions, replay, rollback,
restart, namespace and ordinary-data isolation, deadline response and worker lease
independence. Backend tests continue to exercise work limits, genuine storage
failures, deletion fencing/recovery, reminder policy and concurrent scheduling.

## Measurement and time

Scheduled audits execute the existing instrumented child engine. Added tests prove
that clock acceptance alone adds no engine measurements, subsequent worker execution
adds real line sets to the right session/revision, ordinary/other-session work cannot
contribute, invocation replay cannot inflate counts, report GETs are read-only, and
restart/cleanup preserve the correct scope. Existing correction measurement tests
remain in the regression suite.

The target report honestly declares `app/workbooks` and `app/audit` child line
coverage. API, storage and UI are excluded. Clock/deletion/session HTTP paths are
validated by integration tests; no global tracing or invented API coverage was
introduced. Auxiliary session line counts are not summed into a tester report.
Dixon retains ownership of component, system and model coverage.

Simulated timestamps remain business calendar time only. Queue/worker leases,
HTTP deadlines, process execution limits and suite timing remain real UTC or
monotonic time. Performance evidence uses Dylan's existing real-worker timing hooks
in the combined integration checkout, separating validation, queue, engine and
end-to-end intervals without changing processing bounds.

## UI behavior

The existing plain JavaScript UI now loads schedule settings and notification
history for each workbook. Owners can replace schedule settings, enable/disable
periodic audits, set/clear the institution-supplied deadline and inspect the newest
stored version and UTC next-audit time. Disabling periodic work explicitly retains
the deadline unless it is cleared. Reviewers see read-only summaries and notifications.
Every notification displays its version, threshold, status and safe text; DELIVERED
is explained as an in-application record, not email delivery.

Owner deletion uses an accessible native dialog naming the workbook, with Cancel
initially focused. It handles 204 without attempting JSON parsing. Active-operation
and known pre-delete failures preserve the current view. Partial/uncertain deletion
hides stale artifacts, retains a per-workbook recovery marker in memory, and allows
an explicit owner retry even when history now returns 404. Additional-version upload
to a pending deletion is disabled. A 404 after an uncertain deletion is described
as no longer accessible, not falsely certified as a verified 204. Changing identity
clears recovery markers and review state; no token is persisted. Successful deletion
clears stale results/downloads and returns focus to the existing-workbook input.

Schedule PUT and DELETE are never automatically resent after a lost response.
Schedule uncertainty asks the user to refresh persisted settings. Existing keyed
remediation recovery is retained. All untrusted messages and IDs use text rendering,
and the existing CSP and base-prefix routing are preserved. The required external
workbook-change disclosure appears on scheduling and results views.

## Running Ryan's checks

Restore pinned dependencies with `uv sync --frozen --extra measurement`. Then:

```sh
.venv/bin/python -m pytest tests/ryan tests/unit/test_phase5_backend.py tests/contract -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy app tester scripts
.venv/bin/python -m scripts.export_schemas --check
node --check app/ui/static/app.js
```

Optional browser tools are Playwright 1.58.0 with Chromium and axe-core 4.11.0;
they are development evidence tools and do not change runtime dependencies. Pass
the installed local `axe.min.js` path:

```sh
.venv/bin/python -m tests.ryan.browser_ui
.venv/bin/python -m tests.ryan.browser_phase5 --axe-script /path/to/axe.min.js --evidence-dir /tmp/phase5-scans
.venv/bin/python -m tests.ryan.browser_real --axe-script /path/to/axe.min.js --evidence-dir /tmp/real-scans
```

On the tested Ubuntu 26.04 host, Playwright 1.58.0 needed
`PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64` to install/use its fallback
Chromium build. Product CSP is not relaxed: scans inject the analyzer through
browser DevTools evaluation. Raw scans preserve incomplete checks and include
computed text contrast samples; a clean automated scan is not a manual sign-off.

Combined validation also used Dixon's tester (#16) and Dylan's evidence tools
(#18). The verification report distinguishes that combined candidate from this
PR's own regression results. The common simulated target rejects non-byte
multipart payloads, preserving type safety when imported by Phase 5 checks.

The lockfile updates only the transitive `opentelemetry-api` dependency from
1.45.1 to 1.45.2 to resolve CVE-2026-126360 found by the dependency audit.

See `docs/evidence/ryan_phase5.md` for actual results and
`docs/evidence/phase5/ryan_manual_validation.md` for the human checks still pending.
