# Jackson Phase 4 backend verification — October 29, 2026

Target specification 2.1.0 / ruleset 2.0.0. Backend integration remains blocked by the missing real Phase 4 engine; this evidence does not establish full Phase 4 acceptance. No deployed endpoint, AWS resource, retained production database, or private credential configuration was changed.

The existing `.venv` contained the locked dependencies but its Python standard library was missing. Verification used a clean temporary Python 3.12.16 interpreter with the existing installed site-packages, plus the existing Ruff binary. No dependency or lockfile was changed. Linux memory-limit enforcement, Docker runtime sizing, real S3 I/O and Microsoft Excel inspections were not verified on this macOS host.

## Fresh results

| Check | Result |
|---|---|
| `python -m pytest -q tests/unit/test_phase4_{decisions,persistence,routes,upload_recovery,worker,reports,cleanup_limits}.py` | 123 passed; HTTPX/Starlette deprecation warning only. |
| `python -m pytest -q tests/unit/test_phase4_processes.py` | 1 passed; actual temporary loopback API and separate worker entry point, including restart, downloads and completion logs. |
| `RULESET_VERSION=1.0.0 python -m pytest -q` | **693 passed, 9 failed, 6 skipped**. This is not a green full-suite result. Failures are individually explained below. Phase 4 fixtures explicitly select 2.0.0. |
| `ruff check .` / `ruff format --check .` | Passed. |
| `python -m mypy --platform linux app tester scripts` | Passed, 73 source files. This is type checking, not Linux runtime evidence. |
| `python -m scripts.export_schemas --check` | Passed; system OpenAPI 3.1.1 contains 13 business paths plus two instructor paths; tester catalog unchanged. Both catalogs validate in the new HTTP tests. |
| `python -m scripts.check_identity` | Passed unchanged Phase 3 identity vectors. New backend tests additionally validate normative Phase 4 finding hash attribution. |
| `python -m scripts.make_synthetic_fixtures --check` | Passed; original fixture bytes/expectations unchanged. These remain Phase 3 fixtures, not a reviewed all-six pack. |
| `python -m hatchling build -t wheel` | Passed; wheel generated locally in ignored `dist/`. |
| `git diff --cached --check -- . ":(exclude)docs/contracts/phase4/Phase4_Shared_Specification.md"` | Passed for authored changes. The unchanged imported specification retains its intentional two-space Markdown hard break on line 3. |

The Phase 4 process test also passed as part of the broad run. Temporary loopback tests required sandbox escalation; no external server was used. Four hosted acceptance tests skipped because no live target/tester credentials were configured, and two Linux RLIMIT_AS tests skipped on macOS. No hosted grader or full reciprocal tester run was attempted.

## Scope of the 124 new cases

- **39 decision/DTO cases:** companion examples against frozen schemas; strict typed values, XML text, Unicode whitespace, lengths, missing/extra values, name conflicts, supported actions, input-order validation and state coherence.
- **20 persistence cases:** empty/V1 migration preserving original payloads and legacy snapshot absence; acceptance order with tied timestamps; failed/manual/generated re-audits; immutable reports; actual concurrent database and service-level same-key races; conflicting reuse; audit/remediation exclusion; lease/queue restart fencing; publication transaction rollback; provenance; source-identity collision rejection and isolated cleanup.
- **25 HTTP boundary/workflow cases:** real authentication, sessions, SQLite, filesystem and worker behavior; exact error/body/header contracts; owner/granted/ungranted reviewer/user/test-admin isolation; multipart/JSON limits; all derived read paths; no-edit remediation, new versions, history, report/source immutability, and cleanup preserving ordinary data.
- **9 upload recovery cases:** real put/commit/delete with focused lost-acknowledgement injection; uncertain commits preserve possibly committed bytes; expired staging survives restart and fences late acceptance; invalid intake creates no accepted metadata; slow storage does not consume the queue budget.
- **22 worker cases:** isolated child calls, credentials absent from child environments, deadlines, synthetic APPLY edit/preservation/validation/audit/effect failures, storage and publication failures, lost acknowledgements, no-editor no-APPLY output, durable test fault/re-audit and genuine integrity failure precedence, late-worker cleanup across session deletion.
- **7 audit/report boundary cases:** incomplete/invalid inventories, classifications, context and finding IDs fail explicitly; oversized serialized content produces a controlled error without partial success.
- **1 remote cleanup budget case:** mocked S3 adapter boundary proves bounded housekeeping/backoff without AWS access.
- **1 actual-process case:** HTTP upload → separate module worker audit → keyed no-edit remediation → separate worker publication → API restart → downloads/report/history/replay; INFO logs contain safe correlated stage/remediation fields and no tokens or workbook content.

The test engine double explicitly fabricates test findings/edits **inside tests only**. OOXML intake, subprocess isolation, database transactions, storage and worker transitions are real. These checks do not verify six-rule detection accuracy, real APPLY corrections, independent preservation semantics, Excel no-repair behavior, or shared answer keys. Production has no double/fallback engine and refuses unsupported ruleset capability.

## Full regression failures retained for review

| Existing test | Observed reason |
|---|---|
| `tests/contract/test_schemas.py::test_openapi_catalogs_are_valid_current_and_reflect_delivered_routes` | Stored catalog matches the generated catalog and validates, then the old assertion expects six system paths; Phase 4 delivers 15 including instructor routes. |
| `tests/ryan/test_runner.py::test_large_output` | Expects a 5 MB result to fail under the old 4 MiB limit. Phase 4 permits public results up to 64 MiB. Optional instrumentation still has its own 4 MiB budget. |
| `tests/unit/test_foundation.py::test_factories_do_not_contact_network_or_write_storage` | Old catalog assertion expects four paths; the application exposes 13 business paths without testing routes. Factory construction remains free of network/storage effects. |
| `tests/unit/test_jackson_api.py::test_storage_failure_and_health` | Expects the rollback method never to be called after a failed put. Durable staging now schedules safe hash-checked rollback even after a lost put acknowledgement. New tests prove no committed source is deleted. HTTP behavior remains a controlled 500. |
| `tests/unit/test_jackson_api.py::test_missing_integrations_are_unavailable` | Old catalog assertion expects four paths; Phase 4 exposes 13, even when unavailable engine processing correctly returns 503. |
| `tests/unit/test_jackson_deploy.py::test_deployment_renderer` | Expects proxy timeout literal 660 seconds; §10 now requires at least 1,830 seconds. |
| `tests/unit/test_jackson_processes.py::test_live_process_restart_and_worker` | Its subprocess deliberately clears the environment and uses a Phase 3 test engine without declaring a ruleset. The new target default 2.0.0 correctly returns 503, so its 200-health startup wait expires. The new explicitly configured Phase 4 process test passes. |
| `tests/unit/test_jackson_storage.py::test_schema_initialize_is_idempotent_and_foreign_keys` | Expects schema version 1; the tested additive upgrade is schema 2. |
| `tests/unit/test_jackson_storage.py::test_measurements_sets_unavailable_and_revision` | Counts remain 2/3, but it expects the old basis text. The new basis explicitly names measured paths, includes correction calls, and discloses excluded API/storage/UI/other modules. |

These expectations were not changed to make results pass. CI therefore remains red until the appropriate owners reconcile the superseded Phase 3 assertions and integration configuration. All fresh Phase 4 tests pass; this does not erase the regression failures or authorize release.

## Acceptance gates at the October 29 verification

Dylan's real six-rule audit/context and correction/preservation engine, Dixon's independent Phase 4 suite and reviewed fixtures, Ryan's new UI and final editor measurement scope, resolution of the tint-vector conflict (subsequently agreed October 30; see follow-up below), real deployed limits/storage verification, Excel inspections and retained reciprocal reports remain outstanding. See `docs/implementation/jackson_phase4.md` for exact interfaces, migration/deployment notes and the conflict calculations. This PR is a draft backend foundation; Jackson's portion and full Phase 4 acceptance are **not declared complete**.

## October 30 follow-up — corrected context pack 2.1.1

Jackson supplied the agreed Team 12 tint correction and replacement pack. The active files in `docs/contracts/phase4/` now match all eight archive members byte-for-byte. All seven member checksums in its `SHA256SUMS.txt` were verified before import and by the new regression tests. The replacement ZIP SHA256 is `a23409521dd0f3818fa6871ff6f43816c022d03d527542f89b98593aa62554c0`. The original pack remains in commit `694bb13`.

The tint discrepancy is resolved: `336699/-0.5 → 1A334D`, `336699/+0.5 → 8CB3D9`, with the added `010101/-0.5 → 010101` and `050505/-0.5 → 030303` half-boundary cases retained. The origin of the old values is unverified; floating-point error is only a possible explanation. Exact rational arithmetic in the **test-only pack checker** proves the HSL/half-up values without intermediate rounding or blanket epsilon. Below-half values are separately checked so a rounding tolerance cannot promote them incorrectly.

Pack revision and newly frozen suite target are **2.1.1**. Specification **2.1.0**, ruleset **2.0.0**, report/API formats, and historical records remain unchanged. Hash checks pin the baseline specification, wire schemas and examples to the earlier bytes. No database, production module, existing fixture/answer-key file, retained report or audit was edited.

Fresh follow-up verification:

| Command/check | Actual result |
|---|---|
| `python -m pytest -q tests/unit/test_phase4_context_pack.py` | **30 passed**: seven member checksums, version/baseline preservation, full tint inventory, seven exact HSL vectors, six half/below/above-half rounding cases, seven unrounded contrast comparisons, and theme/indexed-palette checks. The same tests against the preceding pack failed 13 cases and passed 17, exposing the intended missing/corrected artifacts. |
| `python -m pytest -q tests/unit/test_phase4_context_pack.py tests/unit/test_phase4_decisions.py tests/unit/test_phase4_persistence.py tests/unit/test_phase4_reports.py` | **96 passed**, including historical migration/report immutability checks. |
| `RULESET_VERSION=1.0.0 python -m pytest -q` | **723 passed, 9 failed, 6 skipped**. The same nine tests listed above fail for the same reasons; no new failure appeared. All 124 original Phase 4 backend cases and 30 new artifact cases pass. |
| Ruff lint/format, strict Linux-target mypy, schema export freshness, synthetic fixture consistency, `git diff --check` | Passed. OpenAPI catalogs remain unchanged. |

The existing runnable independent tester is manifest **1.5.0**, ruleset **1.0.0**. It has not been relabeled as a newly frozen Phase 4 suite. Its unchanged fixture manifest SHA256 is `8c10aee18b7cf3d04b19500b05d1cc68f99afbc1b2965981a899680db973afa1`; its unchanged expectations SHA256 is `892911df6575cabcf5aab87dd304f7403418778f02c1903aa0dddc11da328904`. Individual existing fixture byte hashes remain recorded in that manifest and pass the fixture consistency check.

These are pack/oracle consistency checks, **not production color-rule, theme-resolution, indexed-color engine, or workbook tint acceptance evidence**. No production color helper or executable tester is used to generate expected values. Dylan still needs to exercise the corrected vectors against the real color engine; Dixon still needs independent Phase 4 fixture/handler coverage and individually reported cases under `P4-RULE-COLOR-THEME-TINT`, with executable manifest target 2.1.1. Ryan's UI/final measurement integration and the other acceptance gates remain outstanding. No deployment or merge was performed.
