# Jackson — Phase 4 backend integration

Target: pack revision 2.1.1, specification 2.1.0, ruleset 2.0.0, suite manifest target 2.1.1. This is a backend implementation and integration proposal, **not completed Phase 4 acceptance**. No deployment was performed. The deployed grading system was not contacted. The working tree started clean at `d72d9a2` on `main`; changes are on `codex/jackson-phase4-backend`.

The corrected attached pack 2.1.1 is retained byte-for-byte in `docs/contracts/phase4/`, including `CORRECTIONS.md` and `SHA256SUMS.txt`. Its ZIP SHA256 is `a23409521dd0f3818fa6871ff6f43816c022d03d527542f89b98593aa62554c0`; all seven supplied member hashes were verified. The baseline specification, schemas and examples are unchanged. The earlier pack remains available in Git commit `694bb13`; its ZIP SHA256 was `9e67b41d73b1f889f14a64e7d9e878f36111c5fb63342e6e6e184cb193154a6b`. The inventory specifies assertions, not executable tests or acceptance evidence. The tint correction is agreed by Jackson's supplied email; no full shared adoption, independent answer-key approval, deployment or reciprocal hosted run is claimed.

## Delivered backend behavior

| Specification | Implementation and evidence |
|---|---|
| §§3–5, 8, 10 | All 13 business paths, strict bounded JSON/multipart parsing, typed correction validation, 400/404/409/422 distinctions, required nullable fields, public health, request IDs, bearer declarations and OpenAPI 3.1.1. HTTP/decision tests validate actual responses against companion schemas. |
| §4 | Authentication and session membership precede path validation/content access; all parent relationships resolve before ownership. Explicit reviewer grants permit reads, including derived outputs/reports/history, with empty review actions. Mutations require the owner. Test admins have no business access. |
| §§5, 9, 12 | New upload versions, immutable re-audits, durable review context, report snapshots, generated provenance and transactional creation/acceptance sequences. Historical audits remain unchanged. Failed audits remain in history. |
| §§8–9 | Mandatory owner/namespace/method/full-path/key idempotency with canonical UTF-8 request digest; stored original acceptance replayed before competing-state checks. A second lookup closes the service-level concurrent-acceptance race. Invalid first submissions do not reserve keys. |
| §§9, 11 | Durable jobs, unique per-version operation reservation, transactional leases and stale-worker fencing. Workers verify immutable input, isolate corrections, validate output, verify preservation, audit output, verify effects, then store/read back bytes before one metadata/report/publication transaction. |
| §§5.4, 11–12 | Source/output downloads authorize before reads, verify bounded bytes/hash, and supply attachment, length and no-store headers. Report bytes are retained in SQLite and never rebuilt on GET. No-edit batches create a fresh version/output with identical bytes and a fresh audit. |
| §14.1 | Strict, disabled-by-default isolated one-shot audit fault; accepted upload/re-audit transactions assign it durably. Integrity failures take precedence. Failed audit/report use the exact injected envelope and empty checks/findings. No public fault route or filename trigger. |
| §§10, 14, 16 | 1 MiB JSON requests, 64 MiB public JSON results, existing 25 MB XLSX bound, 30/60/180 second execution ceilings and 15 second queue allowance. Correction children strip credential environment inputs. Correlated INFO completion logs work under the actual module entry point. |

Ordinary deletion, scheduling and notifications remain absent and return the normal project 404. No UI, independent tester logic, fixture expectations, audit rules or OOXML editor implementation was substituted.

## Engine integration required from Dylan

The current `app.audit.engine.OoxmlWorkbookEngine` implements two rules and exposes no Phase 4 correction or preservation API. No accessible Phase 4 branch/PR was found during the read-only repository check. The default target setting is now 2.0.0. With the currently installed engine, health and otherwise valid new processing requests fail with 503 instead of pretending two rules are six. Explicit `RULESET_VERSION=1.0.0` permits legacy operation for regression/development; existing stored audit versions are always honored.

`app/contracts/ports.py` defines `CorrectionEngine`. A real Phase 4 engine must declare `supported_rulesets = ("2.0.0",)` (or include that version), preserve the existing validation/audit signatures, and implement:

```python
audit(data, validated, ruleset_version) -> AuditEvaluation
validate_decisions(data, validated, source_audit, decisions) -> None
remediate(data, validated, source_audit, decisions) -> bytes
verify_preservation(source, candidate, validated_source, source_audit, decisions) -> None
verify_corrections(source, candidate, source_audit, decisions, output_evaluation) -> None
```

The successful audit returns all six `checks_performed` in contract order and `review_context` in canonical finding order, from the same inventory and safety assessment. Backend checks reject incomplete inventory, incoherent classification/actions, malformed read values, duplicate findings and incorrect finding hashes. No review context is reconstructed from browser guesses or Phase 3 classifications.

Validation methods return `None` only after success, raising typed errors on failure. Preflight correction errors may use `ProjectException(422, code, message)` with `INVALID_CORRECTION`, `UNSUPPORTED_CORRECTION`, or `CONFLICTING_CORRECTIONS`. Backend error transport exposes safe codes and fixed prose. Batch package safety, destination-aware hyperlink checks, independent preservation comparison and independent verification of approved effects remain engine responsibilities. Targets are immutable source inventory targets, never caller-selected XML paths.

Stored decisions are canonicalized by finding ID after input-order validation. The worker supplies correction calls in ALT_TEXT, LINK_TEXT, TABLE_HEADER, WORKSHEET_NAME family order and source finding order within each family. A decline/manual-only batch needs no editor methods: the backend copies source bytes and checks exact equality, then still validates and audits the candidate. APPLY requires all correction/verification methods. Neither branch can publish partial success.

Production APPLY integration and all four real correction families are **unverified and blocked by the missing engine**. Labeled engine doubles are confined to tests; factories select production services only from trusted configuration.

## Persistence, recovery and rollout

`SQLiteRepository.initialize()` upgrades schema 1 to schema 2 transactionally and is idempotent. It preserves existing workbook/version/audit payloads exactly and adds operation reservations, ordered events, reports, remediations/jobs, reserved outputs, idempotency, upload staging and cleanup ledgers. Legacy records are not relabeled and do not acquire invented context, reports or provenance. Legacy report/review requests return the specified 409 errors. The separate tester database and its retained reports are untouched.

Foreign keys, relationship triggers, immutable terminal-audit/report triggers, unique operation reservations and transaction rechecks enforce relationships and concurrency. Completed generated versions and their audit/report/output metadata become visible together. The exact output blob is immutable and verified first. Reserved/unpublished outputs return 409. Generated re-audits retain the version's originating remediation and committed-change provenance.

Upload staging records exist before object writes and fence expired late acceptance. Queue timestamps start after object write/readback. Lost put acknowledgements leave a recoverable deletion intent. Lost commit acknowledgements are checked against committed metadata before compensation; uncertainty never justifies deleting possibly committed bytes. Candidate hashes/keys are registered before output storage. Failed operations expose no successful output, and repeated cleanup removes late writes. Cleanup queries exclude committed version keys.

Session cleanup refuses active audit/remediation/upload reservations, tombstones the session, then removes all its source/output blobs, reports, histories, decision jobs, keys and measurements. Ordinary data is excluded. A private deletion-intent ledger survives resource cleanup to remove writes from expired workers; it is not public history or report data. Remote S3 cleanup handles at most one object per worker cycle and backs off each retained intent for 60 seconds, avoiding repeated missing-object requests every queue poll. Filesystem cleanup remains cheap and repeatable. A restart rechecks retained intents.

Rollout requires a backup, stopped old API/worker processes, schema upgrade and compatible new API/worker images together. Schema-1 code refuses schema 2, so downgrading requires restoring a pre-upgrade backup; do not rewrite retained records backward. The existing release/polling process, private credential handling, EBS/named-volume mounts, S3 prefix and role-based adapter remain intact.

Deployment configuration changes are limited to the tester proxy timeout of 1,830 seconds, system/worker temporary-space ceiling of 1 GiB, and example Phase 4 runtime limits/fault enablement. No AWS provisioning, IAM change, secret rotation, image deployment, merge or live migration was performed. Container/memory sizing and live S3 behavior need deployment-environment validation before release.

## Teammate handoffs and resolved tint correction

- **Dylan:** implement and integrate the six-rule audit/context and correction/preservation methods above; follow the confirmed HSL/half-up tint policy without intermediate rounding or a blanket epsilon; provide real supported fixtures and correction effects and run the corrected/boundary vectors against the real engine. Backend completion does not establish engine correctness or Excel preservation.
- **Dixon:** implement the Phase 4 independent suite/fixtures/answer keys with manifest target 2.1.1 and reciprocal runs, validate the new paths/reports, and reconcile superseded Phase 3 assertions through shared contract review. The shared context constants now reflect the agreed corrected pack; runnable tester expectations must remain independent of production helpers. The existing executable tester expectations were not edited here. Its runtime defaults/client bounds remain Phase 3 unless configured; its complete 1,800 second suite and new JSON/binary bounds require Dixon's implementation.
- **Ryan:** integrate new review/history/remediation routes into the UI, retain keyed bodies across retries, and review session/fault behavior and measurement scope. The existing instructor setup/report envelope is unchanged. Measured children include only deployed `app/workbooks` and `app/audit` line sets, including correction calls when their implementation lives there; API/storage/UI and editor modules elsewhere are excluded. No whole-system or fabricated coverage is claimed. Extend the declared scope when real editor modules arrive.

Resolved October 30, 2026 by Jackson's supplied Team 12 email and corrected pack 2.1.1: §7.7 HSL transformation with exact half-up channel rounding governs. Base `336699` at tint `-0.5` yields exact channels `(25.5, 51, 76.5)` and `1A334D`; tint `+0.5` yields `(140.25, 178.5, 216.75)` and `8CB3D9`. Retain the added `010101/-0.5 → 010101` and `050505/-0.5 → 030303` cases. Intermediate numerical error must not move an exact half below the rounding boundary, and genuinely below-half values must not be promoted by an unspecified epsilon. The origin of the superseded values is not established; the earlier attribution to floating-point artifacts was a hypothesis, not a verified diagnosis.

The corrected pack changes the tint oracle and advances the pack/suite target: newly frozen suites target 2.1.1, while specification 2.1.0, ruleset 2.0.0, API and report formats stay unchanged. `CORRECTIONS.md` takes precedence over the baseline's old suite target. The existing runnable tester is still manifest 1.5.0 / ruleset 1.0.0; it has not been relabeled as a Phase 4 suite. No historical audit, answer-key artifact or retained observation is relabeled. The pack regression tests evaluate tint using independent rational arithmetic and verify the frozen contrast/theme/index data; they do not implement or establish production color-rule acceptance. Dylan's real color implementation and Dixon's workbook fixtures/HTTP handlers remain required.

Human Excel no-repair inspections, fixture review by the other team, real hosted all-six execution, both testing directions and full Phase 4 acceptance remain outstanding. See `docs/evidence/jackson_phase4.md` for actual verification and current failures.
