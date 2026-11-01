# Ryan Phase 4 local verification — 2026-10-31

Scope: Ryan's UI, test-interface and measurement integration on the local combined
Dylan/Jackson prerequisite base `9dd62f7c073c2976af826b2318279d68e91e56af`.
This is local evidence, not a deployment, shared-suite run, or Team 12 approval.
The dependency merge is separate from Ryan's implementation commit.

## Passing checks

- Focused pytest run: **87 passed**, one upstream Starlette/httpx deprecation
  warning. Included `tests/ryan`, real-engine HTTP workflow, Phase 4 worker
  orchestration tests, and corrected context-pack regressions.
- Chromium browser checks: **24 passed** (12 scenarios at `/` and `/system`).
  Retained observations: [ryan_phase4_browser.jsonl](ryan_phase4_browser.jsonl).
  Covered all four typed input forms, explicit unselected defaults, manual-only
  restrictions, mixed apply/decline, uploads and additional versions, history,
  re-audits, downloads, reviewer restrictions, failed audits/remediations,
  invalid corrections, stale conflicts, resumed polling, and lost acceptance.
- `ruff check .` and `ruff format --check .`: passed.
- `mypy app tester scripts`: passed (85 source files).
- `node --check app/ui/static/app.js`: passed.
- `python -m scripts.export_schemas --check`: both system/tester catalogs passed.

The real measurement test exercised production correction preflight, editing,
output validation/audit and both verifiers through isolated child processes. It
checked actual line-set union, equal source denominators, session/revision
attribution, immutable source/report bytes, unchanged measurements after repeated
report GETs, and no coverage leakage into another session. This is stronger than
counting a module's presence as exercised coverage, but it is not full acceptance
of every workbook representation.

## Test conditions and corrections found

Tests used Linux under WSL, Python 3.12.15 and the locked project dependencies.
Chromium was installed as optional test tooling; no runtime dependency lock was
changed. Python tests ran from a local Linux snapshot to avoid NTFS import latency
causing artificial child timeouts. An earlier slow mounted-filesystem run was
stopped and is not counted as verification evidence.

The browser tests caught use of `crypto.randomUUID` on an HTTP origin. The UI now
uses 128 random bits from `crypto.getRandomValues`, which works on local HTTP test
origins as well as HTTPS. Retry tests confirm the key and exact request body are
reused after a lost response. Code review also found that historical `/review`
requests are forbidden by the contract: the UI now reads historical audits without
requesting editable context, and handles a race with a newly accepted audit.

An inherited Ryan test still assumed the old 4 MiB result ceiling. It now verifies
that a 5 MB private result is accepted and a result over the current 74 MiB private
ceiling is rejected. The fake target's multipart decoder now checks for bytes
before hashing or ZIP parsing, resolving strict type-check errors.

Windows `core.autocrlf` altered the shared pack's raw-byte checksums. The working
files were restored to their committed bytes, and `.gitattributes` now disables
newline translation for those immutable artifacts. No specification text, policy
constant, or supplied hash was changed.

## Remaining acceptance gates

No external service was mutated, no hosted suite was run, and no git push occurred.
The API/UI tests do not replace Excel no-repair inspection, screen-reader/manual
accessibility review, independent fixture answer-key reviews, or reciprocal pod
runs. The independent Phase 4 tester remains Dixon's separate branch. Engine
current-value conformance and team adoption questions are listed in
[Ryan's implementation note](../implementation/ryan_phase4.md).


## Full-suite comparison

The unchanged prerequisite base produced **800 passed, 31 failed, 4 skipped**.
The final working tree produced **818 passed, 22 failed, 4 skipped**. Every final
failure ID also failed on the unchanged base; there are no new failing IDs.
The nine resolved failures are the old private-output-limit test and eight
context-pack byte/newline assertions. The additional passing cases come from
new Ryan measurement, fault-setup and output-boundary tests.

The 22 remaining failures cover old route-count assertions, legacy backend test
engines configured against the new default ruleset, the old proxy timeout,
schema-version/basis assertions, and the legacy process startup scenario. These
are inherited integration/test-maintenance issues, not a green full-suite result.
They remain for the relevant owners; this local commit is not ready to claim full
Phase 4 acceptance. Four live-service tests were skipped because no remote test
configuration was supplied.

Exact remaining and resolved IDs are retained in
[ryan_phase4_run_record.json](ryan_phase4_run_record.json).
