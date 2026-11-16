# Phase 5 contract changes

Specification 3.0.0 | November 10 2026 | Prepared for pod adoption

## Shared behavior added

- Implement operations 11 through 14: schedule configuration, notification reads, isolated clock advancement and owner deletion.
- Add operation G, GET on the schedule path, to inspect persisted policy. Add versionId to ScheduleResponse so the newest stored selection is visible. Original operation numbers stay stable.
- Use full replacement schedule requests; require a strict integer interval 1 through 90 when enabled; optional/null deadline. Identical normalized PUT preserves cadence; deadline-only changes do not reset it.
- Follow the newest committed stored version, including generated output, without postponing due time. Coalesce missed periods into one audit and advance on the original interval grid.
- Emit deadline reminders at UTC midnight 90/30/7 calendar days before the supplied date. They remain active with periodic auditing disabled if a deadline remains set. Do not backfill earlier thresholds; deduplicate even clean/suppressed occurrences.
- Evaluate reminder eligibility against the latest version/latest accepted audit before dispatching this tick's new audit. Record local in-application delivery, not an external email claim.
- Give each new designated test namespace an independent durable clock. Empty setup uses 2027-01-01 UTC. Keep real timeouts, queues, leases and run budgets independent of simulated calendar time.
- Make clock advance atomic, idempotent for the same timestamp, and nonblocking for audit execution. Refuse due busy versions before changing time or events.
- Make owner deletion complete and recoverable across storage failures, active work and restart. Include outputs, reports, schedule, notifications and retained content copies; do not claim success while known copies remain.
- Extend isolated one-shot faults to remediation, report finalization, upload storage, scheduled audits, notifications and pre-delete failure. Keep instructor setup/run/report envelopes unchanged.

## Instructor requirements incorporated

- Own-team suite reports require coverage.model; cross-team reports omit the key entirely. Trusted target/team configuration determines the profile.
- Model coverage counts the team's full milestone design requirements, with an actual design version and requirements-to-tests link; it is distinct from shared acceptance coverage.
- Shared system coverage now has eighteen criteria in scope; component coverage has twelve logical components. Setup/skips earn no qualifying-assertion credit, and HTTP-only evidence excludes the UI.
- Unexpected target internal failures preventing evaluation are error/source system. This overrides the old Phase 4 clean-fixture PROCESSING_FAILED-as-fail rule. Expected controlled errors can pass; evaluable disagreements remain failures.
- Preserve one run per session, setup readiness, stored-report retrieval after loss/abort, exact failure sources and registered complete timing bounds.

## Compatibility and adoption

Audit ruleset stays 2.0.0. No six-rule finding or tint policy changes. Business API generation stays /api/v1 and health apiVersion 1.0. New workbook reports identify specificationVersion 3.0.0; retained Phase 4 report bytes keep 2.1.0. Workbook reportVersion remains 2.0.0. The companion schema includes legacy/new audit profiles and conditional tester report profiles.

Whole-suite budget increases to 2,400 seconds, with caller/proxy read timeout at least 2,430 seconds; workbook validation/audit/remediation/queue bounds stay 30/60/180/15 real seconds. These are implementation targets pending actual timing validation and recorded pod adoption, not measured performance claims.

Historical team implementation notes describing pre-integration blockers are not new obligations and do not override the integrated Phase 4 submission. Open human evidence and the Team 12 rename mismatch remain acceptance work. No adoption, fix, release or passing implementation suite is implied by this change log.

## Final Phase 4 commit recheck

Rechecked PR 15 and code commit 3286699, already included in the pinned ca6becb baseline. This revision makes their operational/tester safeguards explicit and requires reviewing deployment protection against the longer Phase 5 suite. It does not change schemas, ruleset, finding targets, tint vectors, assertion counts or historical results. PR 14 was README-only.
