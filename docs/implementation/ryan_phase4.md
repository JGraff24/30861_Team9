# Ryan — Phase 4 review UI and measured execution

This contribution targets Ryan's responsibilities only. It is based on Dylan's
`feature/dylan-phase4-engine` branch plus Jackson's corrected context-pack commit
`9ec4969`. The engine/backend commits are prerequisites, not Ryan-authored work.
Dixon's independent tester branch is not merged here. Nothing was deployed or
written to GitHub as part of this work.

## Delivered UI

The same-origin page supports owner uploads, additional versions, explicit audit
selection from history, manual re-audits, source and JSON-report downloads, four
typed correction forms, decline/manual-review decisions, remediation polling,
and separate output downloads and review of unresolved output findings.

Every decision starts unselected. Manual-only findings cannot be applied. The
header form shows the exact server target and proposed row; the row is read-only.
Rename forms explain reference updates. Existing values come from review context;
the browser never parses a workbook or invents meaning. Values are submitted
without trimming, and the server remains responsible for rule-specific validation.
A 422 message appears beside each submitted decision because the current backend
may return a batch-level error without a finding identifier.

`GET /ui/identity` is a private UI helper, excluded from the shared operation
catalog. It returns only the authenticated role with no-store caching. It uses
existing bearer authentication and rejects test-admin credentials. Read-only
reviewers open explicitly granted workbook IDs and see no write controls. Every
business operation still enforces resource permissions on the server.

The page respects a mounted base prefix. Bearer credentials stay in memory and
are sent only in Authorization headers. User text is rendered through textContent
or control values. Downloads use authenticated requests and local Blob URLs;
tokens never enter links or filenames. Controls have labels, keyboard focus,
announced status, and field-level errors.

## Recovery and history

Drafts are keyed by workbook/version/audit. On 409 the selected audit and history
refresh, but drafts do not migrate to a newer audit. A user explicitly opens the
new audit and starts new decisions. Historical audits remain readable.

A remediation submission freezes its exact serialized body and random 128-bit idempotency
key until acceptance is recovered. Network, timeout, or 5xx errors keep both for
an explicit retry; accepted jobs are resumed by GET. Pending work prevents other
navigation/mutations or credential changes from overwriting recovery state.
An upload or re-audit is never automatically repeated after uncertain acceptance.
The page directs the user to history before trying again. Recovery state and
credentials are intentionally lost when the page is closed or reloaded.

## System testing and measurement

Jackson's Phase 4 branch already extends measurement into correction children.
This contribution verifies that integration through a real six-rule engine,
SQLite, filesystem storage, measured subprocesses, and worker execution. It
checks preflight, editing, output validation/audit, preservation and correction
verification contribute line sets to the same session and revision. Unexecuted
source modules remain in the denominator. The instructor report remains exactly
the existing wire shape; repeated GETs do not execute work or mutate measurements.
A different empty session retains unavailable coverage instead of inheriting it.

Code coverage is scoped to `app/workbooks` and `app/audit`, including the editor
and preservation verifier. It excludes API, storage, orchestration, and UI. Browser
checks below are separate local evidence; they must not inflate a completed
HTTP-only tester report's C9 numerator. Additional setup tests verify invalid or
disabled fault requests cannot clean existing namespaces or create a new session,
and a fault session cannot be rearmed by reusing its ID.

The measured-runner regression now accepts results above the former Phase 3
4 MiB private envelope ceiling and rejects results above the current 74 MiB
envelope limit. These are internal transport tests, not valid public finding
examples. A small byte-type guard in the existing simulated target keeps its
multipart decoder compatible with strict typing. `.gitattributes` disables
line-ending conversion for the hash-pinned shared context pack; the supplied
artifact contents and hashes remain unchanged.

## Reproduce

Use Python 3.12 and the locked development environment on Linux (the worker uses
Linux process/resource controls):

```sh
uv sync --locked
uv run pytest tests/ryan tests/unit/test_dylan_phase4_http.py tests/unit/test_phase4_worker.py
uv run ruff check .
uv run ruff format --check .
uv run mypy app tester scripts
```

Browser verification is optional tooling outside application dependencies:

```sh
uv pip install playwright
uv run --no-sync python -m playwright install chromium
uv run --no-sync python -m tests.ryan.browser_ui
```

Browser checks exercise the real UI against a simulated HTTP target at both `/`
and `/system`; they are not proof of engine accuracy or deployment conformance.
The separate measurement integration test uses real engine/worker execution.
Actual verification results and inherited failures are recorded in the evidence
note, not inferred from this list of commands.

## Rule clarification and acceptance work remaining

- The supplied 2.1.0 pack covers real tables and qualifying plain ranges. The UI
  uses the exact server rectangle and proposed header row, not its own heuristic.
- Theme/tint handling and conditional-format manual treatment remain engine policy.
  Jackson's 2.1.1 correction fixes the half-up tint vectors without changing ruleset
  2.0.0. It supersedes only the affected oracle values; historical evidence stays
  unchanged. This is local adoption, not confirmation of Team 12 agreement.
- Dylan documents manual-only `currentValue` as null. The shared specification
  requires existing ALT_TEXT and WORKSHEET_NAME values even where no APPLY action
  is supported. The UI safely displays whatever is supplied, but the engine's
  conformance on those cases needs team resolution; no guessed values are added.
- Two Team 12 answer-key reviewers, Excel no-repair inspections, shared fixture
  adoption, and reciprocal deployed runs remain acceptance gates. No such reviews
  or results are claimed here.
- Scheduling, reminders, test clock, and ordinary deletion remain Phase 5.
