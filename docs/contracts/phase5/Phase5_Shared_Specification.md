# Phase 5 Shared Implementation Specification

Excel Accessibility Hub | Pod E | Teams 9 and 12  
Specification 3.0.0 | Ruleset 2.0.0 | Pack and suite target 3.0.0 | November 10 2026

## 1 Purpose and authority

This specification defines the shared observable behavior for completing the Excel Accessibility Hub in Phase 5. Both teams implement the same project API, scheduling and reminder policy, isolated testing controls, error semantics, and acceptance criteria. Each team retains its own system, tester, internal design, hosting, and independent workbook expectations.

The authority order is the instructors' Cross-team Testing Interface, the Phase 2 agreed requirements and acceptance criteria, this Phase 5 specification, and the inherited Phase 4 specification 2.1.0 with correction pack 2.1.1. This document supplies precise decisions where Phase 2 left behavior open; it overrides the inherited contract only where explicitly stated. Conflicts with instructor requirements are resolved in favor of the instructor document. Significant changes to agreed requirements need the course's required approval; this pack does not authorize reducing scope.

This is the implementation target prepared for pod adoption. Distribution does not establish Team 12 adoption, deployment, completed tests, or human acceptance. Before implementation freezes, both teams record adoption of this revision, target URLs, private access provisioning, and pack hashes in ADOPTION.md. That is a coordination record, not permission to silently choose different behaviors.

The inherited files are included unchanged in baseline_phase4/. Read that baseline with its CORRECTIONS.md. This document and the phase5 companion files govern new Phase 5 work. A test-plan entry describes a required check; it is not an executable test or a passing result.

## 2 Scope and completion

Phase 5 delivers periodic audits, deadline reminders, notification records, an isolated test clock, ordinary workbook deletion, and the remaining controlled-failure, authorization, transport, accessibility, performance and repeatability evidence for REQ-12 through REQ-15 and AC-12 through AC-18. Re-run AC-01 through AC-11 against the integrated Phase 5 service. Authentication and controlled failures already delivered in Phase 4 remain required for all new paths.

The reviewed Team 9 baseline includes the final pre-submission fixes in commit 328669902efc1094facb86bb05db1cc713a1487d, merged by PR 15 as ca6becb. These delivered two bounded worker slots, independent housekeeping, graceful draining, queue-versus-worker expiry diagnostics, deployment protection for remote sessions between jobs, and bounded draining of unexpectedly accepted tester work. Preserve those safeguards while adding scheduled work. They do not change the six-rule engine, finding targets, corrected tint policy or independent finding oracles; Team 12 may implement equivalent safeguards differently.

The six audit rules, finding identities, safe correction profiles, approved-edit order, immutable source and report guarantees, and all-or-nothing remediation remain unchanged. The system checks only uploaded copies, requires explicit human decisions for supported corrections, and never labels a workbook legally compliant. It does not monitor or modify an external live workbook. External email, SMS and push delivery are outside the required common contract; persistent in-application notification records are required.

Phase 4 acceptance gaps remain visible: Microsoft Excel no-repair inspections, cross-team human fixture/answer-key review, human explanation review, and the five-workbook real-world precision evaluation. The worksheet-renaming mismatch reported against Team 12 remains a joint investigation until new evidence resolves it. Do not silently change rules, finding identities or expected workbook results to erase that mismatch.

## 3 Versions and compatibility

| Item | Phase 5 target |
|---|---|
| Shared specification | 3.0.0 |
| Context pack and executable suite target | 3.0.0 |
| Six-rule audit ruleset | 2.0.0 |
| Business API generation | /api/v1 and health apiVersion 1.0 |
| Downloaded workbook audit report format | reportVersion 2.0.0 |
| New workbook audit report specificationVersion | 3.0.0 |
| Instructor OpenAPI dialect | 3.1.1 |

Health remains exactly {"status":"AVAILABLE","apiVersion":"1.0","rulesetVersion":"2.0.0"} when available. Do not add team, clock, design or deployment fields to health. The instructor starter's info.version 0.4.2 is not the pod's ruleset or specification version.

New audit report snapshots identify specificationVersion 3.0.0 without changing their reportVersion or business shape. Retained Phase 4 reports keep specificationVersion 2.1.0 and their original bytes. Old audits, source bytes, fixture expectations and completed tester reports are not rewritten. New Phase 5 regression tests adapt expected specification labels and the explicitly changed testing rules; they do not reinterpret an earlier saved run.

The companion phase5-wire-schemas.json contains the full inherited definitions plus Phase 5 overrides and additions. NewAuditReport checks new snapshots; AuditReport also accepts retained Phase 4 snapshots. Cross-record ownership, time, count inequalities, identity and conditional report-profile checks require application validation beyond JSON Schema.

## 4 Shared representation and permissions

Keep Phase 4 camelCase business JSON, snake_case testing JSON, bounded UTF-8 parsing, identifier rules, required nullable response fields, and the four-field ProjectError envelope. Reject duplicate JSON keys, unknown fields, non-finite numbers, invalid UTF-8 and type coercion. An integer interval excludes booleans and decimal JSON numbers. Calendar dates use exactly YYYY-MM-DD and must be real Gregorian dates; timestamps use UTC RFC3339 ending in Z, with at most six fractional digits. JSON Schema integer validation alone may accept a mathematical integer written as 1.0; the strict request parser must still reject that lexical form. A calculation outside representable calendar years 0001 through 9999 returns 400 TIME_RANGE_EXCEEDED before changing state; reminder thresholds before year 0001 are simply outside the observable timeline.

Owners read and change their workbooks. Explicitly granted reviewers can read schedules and notifications, but cannot configure schedules or delete workbooks. An unrelated user is denied; an ungranted reviewer is denied. A test-admin can prepare/read owned test sessions and advance their clocks, but gains no ordinary workbook access. The clock path is the sole added project-path exception to the existing test-admin role rule.

For ordinary workbook routes, authenticate, validate session membership, resolve resource relationships in that namespace, authorize, then validate state and body. Unknown or wrong-namespace resources return 404; an identified same-namespace foreign resource returns 403. Never read workbook bytes before authorization. Use X-Test-Session to identify prepared test state; omission always selects ordinary data. No request supplies its own owner or namespace in JSON.

## 5 Operation catalog

Retain Phase 2 operation numbers 1 through 14 and Phase 4 paths S, V and R. The new read operation G makes a persisted schedule inspectable by the UI and independent tester; it does not renumber the earlier catalog. There are 18 business operations over 17 distinct paths.

| Operation | Method and path relative to system base | Success |
|---|---|---|
| 1 | GET /api/v1/health | 200 HealthResponse |
| 2 | POST /api/v1/workbooks | 202 UploadResponse |
| 3 | POST /api/v1/workbooks/{workbookId}/versions | 202 UploadResponse |
| 4 | GET /api/v1/workbooks/{workbookId}/versions/{versionId} | 200 VersionResponse |
| 5 | GET /api/v1/workbooks/{workbookId}/versions/{versionId}/audits/{auditId} | 200 AuditResponse |
| 6 | POST /api/v1/workbooks/{workbookId}/versions/{versionId}/audits | 202 ReauditResponse |
| 7 | POST /api/v1/workbooks/{workbookId}/versions/{versionId}/remediations | 202 RemediationAccepted |
| 8 | GET /api/v1/remediations/{remediationId} | 200 RemediationResponse |
| 9 | GET /api/v1/outputs/{outputId}/workbook | 200 XLSX |
| 10 | GET /api/v1/workbooks/{workbookId}/history | 200 HistoryResponse |
| 11 | PUT /api/v1/workbooks/{workbookId}/schedule | 200 ScheduleResponse |
| 12 | GET /api/v1/workbooks/{workbookId}/notifications | 200 NotificationResponse |
| 13 | POST /api/v1/test/clock | 200 ClockResponse |
| 14 | DELETE /api/v1/workbooks/{workbookId} | 204 with no body |
| S | GET /api/v1/workbooks/{workbookId}/versions/{versionId}/source | 200 XLSX |
| V | GET /api/v1/workbooks/{workbookId}/versions/{versionId}/audits/{auditId}/review | 200 ReviewResponse |
| R | GET /api/v1/workbooks/{workbookId}/versions/{versionId}/audits/{auditId}/report | 200 JSON attachment |
| G | GET /api/v1/workbooks/{workbookId}/schedule | 200 ScheduleResponse |

Operations 1 through 10 and S/V/R inherit Phase 4 semantics. New routes must appear with actual request/response/error schemas and authentication in the system OpenAPI catalog. Do not redirect project requests or expose unauthenticated storage URLs.

## 6 Configuring and reading schedules

Operation 11 accepts a ScheduleRequest with required enabled, conditional intervalDays, and optional complianceDeadline. If enabled is true, intervalDays is required and is an integer from 1 through 90 inclusive. If enabled is false, intervalDays may be absent, null, or a valid integer. An absent disabled interval normalizes to null. complianceDeadline may be absent, null, or a valid date; absence normalizes to null. These are full replacement semantics, not PATCH semantics. Invalid content returns 400 INVALID_REQUEST and leaves the previous configuration unchanged.

ScheduleResponse has exactly workbookId, enabled, intervalDays, complianceDeadline, versionId, nextAuditAt and reminderDays. reminderDays is always [90,30,7]. versionId is the newest committed stored version by the existing creation sequence, including a generated output. nextAuditAt is null when disabled. Before the first configuration, GET G returns enabled false, null interval/deadline/nextAuditAt, the latest versionId and the fixed reminderDays; it creates nothing.

A first enable or a changed enabled/interval configuration sets nextAuditAt to the effective current time plus intervalDays times 86,400 seconds. A deadline-only change preserves the periodic due time. An exactly repeated normalized PUT preserves the due time and creates no duplicate work. Disabling cancels future periodic work; it does not cancel an audit already accepted. To disable reminders too, clear complianceDeadline with null or omit it in the replacement body.

Only the owner configures a schedule. The owner records the date supplied by the responsible institutional administrator; this does not create a new institution-admin account role or authorize the software to determine the legal deadline. Example dates are test inputs, not legal advice.

## 7 Periodic audit behavior

A schedule follows the newest committed version when work is dispatched. Uploading a new version or publishing a generated version changes that selection but does not postpone nextAuditAt. Re-auditing an older version does not make it newest. Creation order, not filename, UUID sorting or last-audit time, selects the version.

At or after nextAuditAt, enqueue one durable audit on that version using the installed ruleset. Create a fresh audit record, update its latest-audit pointer and preserve all older audits/reports. Scheduled audits use the same engine, status derivation, authorization inheritance, failure containment and provenance rules as manual audits. They create no new workbook version.

If several intervals were missed, coalesce them into one audit. From old due instant d, interval I seconds and effective time t at dispatch, set nextAuditAt = d + (floor((t-d)/I)+1)*I. This is the first original-grid due instant strictly after t; do not reset cadence to t+I. Failure of the accepted audit does not undo that advance or disable future intervals. There is no automatic retry of the same failed occurrence; the next interval or a manual re-audit provides a new attempt.

Preserve the existing one-active-operation-per-version rule. A real scheduler leaves a busy version due and retries a later tick. It must not create a duplicate audit or overwrite an active remediation. Each real-time scheduler evaluates due work within five seconds while its dependencies are available. Restart resumes from durable due times; multiple scheduler processes must claim a due occurrence transactionally. This does not require a particular scheduler product.

## 8 Deadline reminders and notification records

Deadline reminders are independent of whether periodic auditing is enabled. A nonnull complianceDeadline enables the policy. The three due instants are 00:00:00Z on the deadline date minus 90, 30 and 7 calendar days. Use UTC calendar arithmetic across leap years; do not substitute months or local daylight-saving offsets.

When a due instant is processed, select the newest stored version and its latest accepted audit. A successful terminal audit with no findings suppresses the reminder. An absent audit, PROCESSING, PROCESSING_FAILED, or a successful audit with any finding makes the version eligible. DECLINE and MANUAL_REVIEW do not resolve findings. Use only that version's records, never an older clean version. For a clock advance that also dispatches audits, evaluate reminder eligibility from the pre-dispatch snapshot so the newly enqueued audit does not itself make a clean workbook appear unresolved.

A deadline set after an earlier reminder instant does not backfill that earlier reminder. An instant equal to configuration time is eligible on the next processing pass. On a forward clock jump, evaluate every crossed, still-applicable instant, including several offsets; evaluate against the snapshot at processing time rather than reconstructing past workbook states. A clean evaluation is also recorded internally as considered, so later changes do not retroactively emit that occurrence.

Each (workbookId, deadline date, offset) is considered at most once, durably across restart. Repeating a clock timestamp, a scheduler tick or the same PUT cannot issue it again. Changing the deadline affects future occurrences; returning to an earlier deadline does not reissue already considered keys. Retain earlier notification records unchanged until workbook deletion. A disabled/null deadline produces no new reminders.

Operation 12 returns exactly {notifications:[...]}. Every record has notificationId, type, workbookId, versionId, scheduledFor, status and message. Use type COMPLIANCE_DEADLINE_REMINDER; AUDIT_REMINDER remains a reserved Phase 2 enum value and is not emitted by this milestone. scheduledFor is the threshold instant, not the processing time. Order by scheduledFor and then durable creation sequence, oldest first. No records returns an empty array.

The required delivery mechanism is a durable in-application record. A successful reminder becomes DELIVERED when committed and visible through operation 12; that status makes no email-delivery claim. The isolated notification fault records FAILED with a safe explanation. PENDING may exist internally before terminal recording, but every reminder returned in a successful clock response is already terminal. A failed reminder does not change audit/version status and is not retried automatically. Optional external delivery must not change these shared records or acceptance expectations.

## 9 Effective time and isolated clocks

Ordinary data uses real UTC time. Each fresh prepared Phase 5 namespace in the designated test deployment has its own persistent simulated clock. Empty system setup initializes it to 2027-01-01T00:00:00Z. Optional setup.clock.currentTime chooses a different initial UTC timestamp. An explicit clock configuration when that trusted capability is disabled returns testing 403 without creating a runnable session. Older sessions without a Phase 5 clock remain unchanged; advancing one returns 409 LEGACY_SESSION_CLOCK. Prepare a fresh session.

Operation 13 requires the designated trusted clock capability, the creating test-admin credential, and X-Test-Session for that owned READY namespace. A disabled capability or an absent namespace returns project 404 TEST_CLOCK_DISABLED. Missing/invalid authentication returns 401 once the route is enabled and a namespace is supplied. A valid wrong role/foreign session owner returns 403. Unknown/cleaned session returns 400 INVALID_TEST_SESSION; a known not-ready session returns 409 SESSION_NOT_READY. It never changes ordinary data or another namespace.

ClockRequest is exactly {currentTime}. The timestamp must be at least the current simulated time. Backward movement returns 400 CLOCK_REWIND; an identical timestamp is allowed. ClockResponse is exactly currentTime, triggeredAudits and triggeredNotifications, each ID array listing only work newly accepted/recorded by this call. Arrays are empty on a no-op with nothing due; an equal-time call may process newly configured work due at that instant.

Clock advancement atomically stores the new effective time, the accepted scheduled audits/jobs, cadence changes, reminder decisions and resulting notification records. Sort due audit acceptance by old nextAuditAt and workbookId; sort reminders by scheduledFor, workbookId and offset descending. If a due selected version has active work, return 409 ACTIVE_OPERATION before any clock/time/event changes. Poll that work and retry the identical clock request; no automatic POST retry is authorized. A pending deletion anywhere in that same namespace similarly returns 409 DELETION_IN_PROGRESS with no advancement.

A call may accept at most 1,000 scheduled audits and consider at most 3,000 reminder occurrences. Larger work sets return 409 CLOCK_WORK_LIMIT with no state changes; use smaller advances or fewer test workbooks. Storage/transaction failure returns a controlled 500 with no partial clock commit. Once committed, lost HTTP acknowledgment is recovered by repeating the same timestamp: it returns the current time and no already accepted IDs. This clock-specific idempotence does not extend to upload, setup or run POSTs.

The clock call accepts work; it does not wait for audits to finish. The tester polls each returned audit through the normal audit API before dependent assertions. Audit acceptance timestamps and notification dates use effective time in the namespace. Completion timestamps must be at least the corresponding start timestamp. Job queue deadlines, leases, execution bounds, deployment protection, network timeouts and suite budgets use real/monotonic time. Advancing simulated time must not expire a worker lease or make a 60-second execution appear to last months. Test namespaces run due scheduling only through operation 13, preventing a real background tick from consuming the test's events.

## 10 Deletion and recovery

Operation 14 requires the workbook owner. Before removal, transactionally prevent new uploads, re-audits, remediations, schedule changes and scheduler claims for that workbook. An active audit, remediation or unexpired upload-staging reservation returns 409 ACTIVE_OPERATION and changes nothing. Expired jobs must be fenced before deletion can proceed. A delete racing a new operation must have one consistent winner. Fenced or late writers must not recreate content after 204; if outstanding storage writes cannot be safely drained or prevented, return a controlled deletion failure and retain the cleanup reservation.

A completed deletion returns 204 with no body only after the workbook's source and generated bytes, reports, findings/review context, versions, history, decisions, idempotency records, notification records and schedules have been removed from the application's retained content. No future audit/reminder may run for it. It also covers stored object versions and application-managed copies/backups that would otherwise preserve that content. A restore must never resurrect an acknowledged deletion. Implementations must document how deletion reaches such copies; inaccessible provider-level forensic remnants are outside this prototype's physical-erasure claim. Only content-free operational evidence may remain, such as an opaque deletion key and outcome needed to prevent resurrection.

Do not delete first and then pretend an object-storage failure was success. Use a durable deletion reservation and recoverable cleanup. If deletion partially fails, return 500 DELETION_FAILED; retain the cleanup intent and make the workbook unavailable to ordinary reads/writes with 404. A retry by the original owner continues that same cleanup; other identities gain no access to the reservation. A reservation itself contains only what is needed to finish removal and is removed when safe. A failed request that is known to have made no destructive change may leave the original readable, as with the injected pre-delete fault.

After successful deletion, old version/audit/output/report/schedule/notification URLs and history return 404; a repeated DELETE returns 404. Other workbooks, test namespaces and finalized independent tester reports remain unchanged. Test-session cleanup is still an administrator-owned namespace action and is not a substitute for exercising ordinary owner deletion. Cleanup removes the namespace clock and scheduler/notification state along with all earlier session resources.

## 11 Controlled failures

Keep source integrity verification, bounded child execution, durable claims, stale-worker fencing, candidate quarantine/cleanup and atomic output publication. A failed scheduled audit records PROCESSING_FAILED on its latest audit/version, preserves source bytes/history and leaves a later scheduled/manual attempt possible. A notification failure never changes audit state. An ordinary processing failure must not expose a partial workbook or a successful partial report.

For repeatable hosted AC-14 checks, the designated test deployment supports the following setup.faults keys. Each supplied value is exactly integer 1; the object is nonempty, unknown keys are rejected, and counters are durable. Rejected malformed/unauthorized requests do not consume a fault. Counters cannot be rearmed through a reused session ID.

| Key | Consumption and observable result |
|---|---|
| audit_failures | First accepted initial/new-version upload audit or manual re-audit; inherited TEST_AUDIT_FAILURE after source verification |
| remediation_failures | First accepted remediation, including no-edit decisions; TEST_REMEDIATION_FAILURE after source verification, before editing/publication |
| report_failures | First accepted ordinary audit job; fail normal report finalization after engine completion with TEST_REPORT_FAILURE; retain a failed audit and bounded failure-report snapshot |
| storage_failures | Next otherwise-valid initial/new-version upload fails before acceptance with HTTP 500 TEST_STORAGE_FAILURE; no new workbook/version/audit/job appears |
| scheduled_audit_failures | First accepted scheduled audit; TEST_SCHEDULED_AUDIT_FAILURE after source verification, before rules |
| notification_failures | First eligible reminder creation; persist FAILED notification, leave audit state unchanged |
| deletion_failures | Next otherwise-valid owner deletion fails before reservation/blob removal with HTTP 500 TEST_DELETION_FAILURE; original remains readable |

Use a separate fresh namespace per fault family. Reject audit_failures and report_failures together with testing 400 to avoid ambiguous injected precedence. Other combinations are permitted but the normative suite isolates them. All controls require the creating test-admin, trusted server enablement and a new isolated session. Well-formed fault setup with the capability disabled returns testing 403 without creating a runnable session. Ordinary data and other namespaces are never eligible.

Injected asynchronous errors use details {stage:<audit|remediation|report|scheduled_audit>,testOnly:true}, a valid requestId and the declared code. Their fixed message is '<Operation> failure requested by the isolated test session.' using Audit, Remediation, Report or Scheduled audit. The inherited audit message remains byte-identical. Failed audit snapshots contain no partial findings; checksPerformed records only checks actually completed (empty for pre-rule faults and all six for report finalization failure). A safety failure-report path must still retain that failed audit; if real persistence itself fails, return/record its genuine controlled storage failure rather than inventing a successful report. A remediation failure publishes no output version/audit/report.

Supplement hosted faults with local integration tests for genuine object-store failures, report serialization failures, transaction rollback, lost acknowledgments, deletion after partial removal, concurrent claims and recovery across restart. The fault switch demonstrates behavior, not the completeness of real failure handling.

## 12 Errors and timing

New business paths use the existing ProjectError {code,message,requestId,details} and X-Request-ID. Testing setup/report/run continue to use exactly {message,source}; do not mix envelopes. New route errors include INVALID_REQUEST (400), CLOCK_REWIND (400), UNAUTHORIZED (401), FORBIDDEN (403), NOT_FOUND or TEST_CLOCK_DISABLED (404), ACTIVE_OPERATION, DELETION_IN_PROGRESS, CLOCK_WORK_LIMIT or SESSION_NOT_READY (409), DELETION_FAILED/STORAGE_FAILED (500), and SERVICE_UNAVAILABLE (503). The disabled/namespace clock rules in section 9 qualify normal lookup precedence. Injected errors are named in section 11.

| Bound | Required maximum |
|---|---|
| Workbook validation / audit engine / remediation worker | Existing 30 / 60 / 180 seconds |
| Accepted-job queue wait | Existing 15 real seconds |
| System setup / tester setup | 30 / 45 seconds |
| Ordinary business GET / schedule PUT / clock advance | 10 seconds each |
| Workbook upload / binary or report download | Existing 35 / 30 seconds |
| DELETE including completed storage cleanup | 30 seconds; controlled failure if incomplete |
| Audit / remediation polling deadline | Existing 80 / 210 real seconds from acceptance |
| Phase 5 whole tester suite | 2,400 seconds including project calls and coverage retrieval |
| Caller run read timeout / proxy upstream read timeout | At least 2,430 seconds |
| JSON request / JSON response / binary transport | Existing 1 MiB / 64 MiB / 32 MiB |

Deployment coordination must protect the full registered evaluation window and overhead, including gaps between project requests, target preparation and coverage retrieval. Team 9 currently uses a renewable 1,900-second external-session protection lease and a 1,900-second deployment wait bound. Review those settings and their renewal behavior against the new 2,400-second run budget before release; do not assume changing the proxy timeout alone is sufficient. Expiry of deployment protection must never delete a session or its evidence, and a deployer must defer rather than interrupt still-protected evaluation. Equivalent mechanisms are allowed for Team 12.

The new suite budget is an explicit Phase 5 increase from 1,800 seconds; record actual runtime before release and revise jointly if needed. It is not permission to extend per-workbook processing bounds. Keep 25,000,000 file bytes, 50 worksheets and 250,000 nonempty cells. Retry only bounded safe GET transport failures, at most three attempts within the original deadline. Never automatically resend setup, upload, re-audit or tester run. Explicit schedule PUT replay, identical clock replay, owner delete recovery and keyed remediation have their stated semantics.

A no-response timeout is communication error. A valid response proving an operation stayed PROCESSING beyond the agreed bound is an evaluable failure. A genuine unexpected target internal failure preventing evaluation is system error. Expected rejection/fault cases pass only when their complete expected envelope, isolation and preservation assertions pass.

## 13 Instructor testing interface

Every tester exposes POST /test/setup, POST /test/run and GET /test/report. Every system exposes POST /test/setup and GET /test/report alongside the project API. The tester owns order, inputs and assertions; there is no required system /test/run and no encoded batch of project calls in setup.

Tester setup accepts exactly {target_url}, generates a fresh session ID, snapshots the suite/fixtures/expectations/target identity, and calls target system setup with that ID and pod starting data. Return 201 {session_id} only when preparation succeeds. No tests execute during setup. Forward a valid system setup error's status, message and source unchanged. Internal tester setup failure uses 500/source tester; no target response uses source communication. Failed setup is not runnable.

System setup accepts {session_id,setup:{}}; the existing omitted-setup-as-empty behavior is retained. The setup object may contain cleanup_session_ids, clock:{currentTime}, and faults as defined here. These remain starting-state controls within the existing envelope, not new grader fields. The tester's main namespace uses explicit empty setup; dedicated scenarios create their own namespaces through the normal system setup with approved test-admin credentials. A duplicate system ID returns testing 409. Setup must remain reachable even when processing resources are stopped; fail preparation clearly if they cannot become ready within the registered bound.

One successful tester session permits exactly one run. Reject simultaneous, later and post-abort run attempts with 409. Invalid requests do not consume a session. Execute planned tests in frozen order through normal project paths, preserve existing base prefixes, retrieve target code coverage, then store the complete SuiteReport before returning 200. A completed report can contain fail and error outcomes. A tester abort returns 500/source tester after retaining available results, an interrupted tester error and skips for unexecuted tests. Report GET returns the stored body unchanged, or 409 before the run ends, and never runs tests. A service crash may prevent retrieval; do not claim otherwise. Durable retention across restart may exceed the instructor minimum and remains desirable.

System report returns only {session_id,coverage:{code:{covered,total,basis}}} for the prepared session's requests so far. GET itself does not change measurement. Later project requests may update it. The tester copies that measured code coverage rather than computing target execution from test outcomes. Retain state through the suite and coverage retrieval. Auxiliary namespace coverage is excluded unless the target supplies a verifiable combined scope; do not sum line counts from different sessions.

## 14 Outcomes and the four coverage measures

SuiteReport has exactly session_id, target_url, results and coverage. Every planned test appears once with a unique nonempty test_id and nonempty message. Outcomes are pass, fail, error and skip. Include source exactly for error, using tester, system, communication or unknown. A correct expected project error is pass. An evaluable disagreement is fail. An unexpected target internal failure preventing evaluation is error/source system. A no-response network failure is error/source communication. A test that did not execute is skip. Preserve the first meaningful failure and continue independent cases when their resources exist.

This supersedes the Phase 4 rule that treated any accepted clean fixture's PROCESSING_FAILED body as fail: when it reports an unexpected internal processing failure that prevented evaluation, classify error/source system. An invalid status/body, a wrong finding tuple, or a continuing PROCESSING state beyond its bound remains an evaluable fail. An intentionally requested fault matching its oracle is a pass. Do not relabel past reports.

Every report contains coverage.code, component and system. For own-team testing it additionally requires coverage.model. For cross-team testing the model key is absent, not null and not a placeholder. Determine the tester team and target team from trusted, exact approved-target configuration; do not infer ownership from host similarity or a caller-supplied flag, and do not add fields to the instructor setup request. Freeze that classification for the session.

| Measure | Owner and denominator |
|---|---|
| code | Target instrumentation; source lines or branches in its declared measured scope |
| component | Tester; all twelve shared components in section 15 |
| system | Tester; AC-01 through AC-18, denominator 18 |
| model for own-team runs | Tester; that team's versioned Phase 5 design requirements, each counted once |

Each measure has covered, total and nonempty basis. Counts are nonnegative integers, covered <= total. If unavailable, both counts are null with an explanation; zero means measured none. Code basis identifies lines/branches and the included code. Component/system basis identifies catalogs and actual qualifying-assertion rules, including partial/manual evidence limits. Model basis identifies the team design version and a stable link to the design-requirements-to-tests mapping.

Coverage means evaluated, not passed or accepted. An executed qualifying assertion can count even when it fails. Setup, preparation, skips, unexecuted branches and mere test presence earn no component/system/model credit. One mapped requirement counts once. A normal API-only run does not exercise the UI and cannot claim AC-17 or C9. Automated preservation can exercise the automated portion of AC-09, with the missing Excel evidence disclosed; it does not establish full acceptance.

Each team supplies its own design map before an own-team session can become runnable. It lists every milestone design requirement, including those not yet tested, with stable IDs, observable assertions and test IDs; total is the entire list, not just mapped entries. Missing/invalid own-team design configuration fails tester setup with 500/source tester. Freeze map bytes/hash/design version, profile and planned design checks with the session. Cross-team runs need no access to the other team's internal design map. Completed old reports remain byte-identical; schema evolution must not inject model into old reports on retrieval.

## 15 Shared component catalog

| ID | Shared component | Qualifying assertion |
|---|---|---|
| C1 | Authentication and authorization | Evaluate an actual allow/deny boundary |
| C2 | Workbook intake | Evaluate accepted/rejected workbook structure or limits |
| C3 | Immutable bytes and version metadata | Compare retained bytes/hash or version identity |
| C4 | Six-rule audit engine | Evaluate exact expected findings or deterministic results |
| C5 | Durable audit and remediation jobs | Evaluate processing, terminal state or recovery |
| C6 | Test sessions and code measurement | Evaluate isolation or real measured session report |
| C7 | Review decisions and corrections | Evaluate approved/rejected decisions or actual edit effects |
| C8 | Downloads reports and history | Evaluate returned artifact or historical record |
| C9 | User interface | Execute retained browser/keyboard/accessibility assertions |
| C10 | Periodic scheduling and clock | Evaluate cadence, dispatch or isolated time behavior |
| C11 | Deadline reminders and notifications | Evaluate eligibility, occurrence or delivery record |
| C12 | Workbook deletion | Evaluate owner deletion, removal, fencing or recovery |

Components are logical shared responsibilities, not prescribed processes or AWS services. HTTP-only reports have at most 11/12 unless the run actually executes qualified UI checks. Local human evidence must not be appended afterward to inflate an immutable run's numerator.

## 16 User interface and operational evidence

Add owner controls to read/configure the interval and supplied deadline, show enabled state, next due time and selected stored version, view notifications, and delete a workbook after an explicit confirmation identifying it. Reviewers see read-only schedule/notification views. Show pending/failed operations and safe text errors. Never expose the test clock as an ordinary user's product control.

Display: 'Scheduled audits check the stored version only. Changes to an external workbook cannot be detected until a new version is uploaded.' Keep a version identifier visible with results. Do not replace the existing audit limitation array or six-rule policy merely to add this UI disclosure.

Complete the upload, findings, corrections, scheduling, notifications and download/delete flows by keyboard. Check programmatic labels, visible focus, text errors and live status announcements. Run an automated accessibility scan and manual keyboard inspection; normal text contrast is at least 4.5:1 and large text 3:1. Browser automation alone does not complete the manual evidence.

Retain structured, content-safe request/job/scheduler IDs and failure stages. Monitor queue depth, failed jobs, scheduled dispatch lag and notification failures, including a demonstrated signal and response procedure. Never log workbook contents or tokens. Test release draining with scheduled jobs and external sessions as well as manual work. Document real single-host concurrency and coordinate full-suite windows; the contract does not promise zero downtime or unlimited simultaneous suites.

## 17 Acceptance evidence

| AC | Required Phase 5 evidence |
|---|---|
| AC-01 to AC-08 and AC-10 to AC-11 | Re-run the six-rule upload review correction report and history regression workflow, including boundaries and rejected decisions |
| AC-09 | Independent preservation plus recorded Excel no-repair checks for every supported correction profile |
| AC-12 | Accept intervals 1/90; reject 0/91 and wrong types; before/exact due; disable/re-enable; newest version; coalescing; restart and claim isolation |
| AC-13 | Eligible reminders at 90/30/7 UTC days; clean suppression; no-audit/failed eligibility; dedup; deadline changes; external-version disclosure |
| AC-14 | Controlled audit remediation report storage scheduled-audit failures with preservation, safe errors and no partial successful output |
| AC-15 | Owner/granted reviewer/foreign user/unauthenticated and test-admin boundaries across every new operation and namespace |
| AC-16 | HTTPS/rejected HTTP, protected hashed credentials, complete owner deletion including schedules/outputs/reports, and documented backup/restore deletion behavior |
| AC-17 | Automated scan plus actual keyboard workflow and text-contrast evidence for all delivered screens |
| AC-18 | Five documented runs of supported boundary work; each finishes within 60 seconds or controlled failure; successful findings identical; report queue and execution time separately |

For AC-18, measure real elapsed time with a monotonic clock. Do not use simulated calendar jumps. Separately record intake validation, queue, engine and end-to-end times so a 60-second engine result does not conceal excessive queue latency. A controlled timeout is within the stated failure alternative, not a successful audit; disclose all five outcomes. Complete the Phase 2 five-real-workbook >=90% precision study with manually adjudicated findings and supporting comparison to Microsoft's Accessibility Checker. The checker is supporting evidence, not the sole oracle.

For each supported correction's Excel inspection retain source/output hashes, approved effects, preservation result, Excel version/platform, reviewer/date and repair-warning observation. Two members of the other team review each team's fixture expectations; independent AI review and successful cross-tests do not substitute for human review. Each tester may use its own independently authored fixtures; common alignment vectors do not require identical complete workbook collections.

## 18 Test inventory and independence

phase5-test-plan.json imports the prior normative assertion inventory and adds hosted, local and human checks. Preserve earlier stable assertion IDs where meaning is unchanged; expand grouped variants into individually reported executable IDs. Record any adaptation needed for spec version 3.0.0, the instructor's outcome rule, the 18-criterion denominator and own/cross report profiles. Retain earlier failures and historical report bytes.

The new fixture scenarios use actual independently reviewed .xlsx bytes from each team's own tester, including clean, seeded, generated-version, rich-preservation and supported-rename workbooks. Calendar vectors are independent policy oracles, not engine-generated expectations. JSON examples illustrate wire formatting and are not deployment evidence. A runnable manifest must bind every planned ID to a real handler, resources, exact oracle, prerequisites, component/AC mappings and qualifying assertion. An unbuilt fixture is an explicit skip and an outstanding acceptance task.

Retain the final Phase 4 tester scheduling safeguards. Finish accepted boundary/preparatory jobs before submitting unrelated fixture work. If a target incorrectly accepts a negative request, retain that original failed assertion and drain only the actual returned job through bounded owner GETs before continuing. Never replay the mutation, replace a failed fixture with a new successful one, or award coverage for the drain. A failed preparation still produces a failed/error observation and dependent skips under the instructor outcome rules; draining does not turn it into success.

Test the tester with deliberately wrong outputs, missing model coverage on a self-run, forbidden model on a cross-run, wrong totals, duplicate/missing IDs, source misclassification, failed preparation, lost responses, aborts and retained report retrieval. No new hosted debugging endpoint or production-helper-derived answer key is allowed. Mocks prove tester behavior; real API/worker/storage execution proves integration.

## 19 Release and pod adoption

Before release, migrate persistent metadata additively, preserve earlier audit/report/tester bytes, freeze the new schemas/manifest/design map and build the installed artifact. Run existing static, type, dependency, schema, policy, integration and browser checks. Validate failure/restart/deletion behavior in the deployment environment. Record actual CI and hosted evidence; distributing this pack is not a completed release.

Update both registry rows with distinguishable reachable system/tester base URLs, the same intra-pod specification URL, private access instructions and the registered 30/45/2,400-second setup/run bounds. Base URLs have no /test suffix, query, fragment or credentials. Register a larger caller/proxy read timeout as specified in section 12.

Run both reciprocal directions and each team's own-system suite with actual code/spec/suite/fixture hashes, timestamps, counts and stored reports. Own-system reports include design coverage; reciprocal reports omit it. Retain failure ownership and root-cause status. Internal storage/hosting changes remain each team's choice; observable behavior, supported scope, deadlines, finding identities and expected results require coordinated pod changes.

## 20 Sources and traceability

1. Pod E Phase 2 submission, submission_431060817.pdf: Q3.1 REQ-12 through REQ-15; Q4.1 AC-12 through AC-18; Q5.1 operations 11 through 14; Q5.2 schemas; Q6.3 milestone; Q9 risk evidence. Supplied November 10 2026.
2. Instructors. Cross-team Testing Interface. Read November 10 2026. https://docs.google.com/document/d/1gfsbGC6oMAOlAW9uCDsIZptCUECsBxBU/edit . The provided document's Phase 5 model-coverage requirement governs section 14. Its starter uses OpenAPI 3.1.1.
3. Pod E. Phase 4 Shared Implementation Specification 2.1.0 and corrected context pack 2.1.1, October 28/30 2026. Included unchanged in baseline_phase4/.
4. Team 9 Phase 4 submitted answers, Phase 4 .pdf, supplied in this conversation; delivered behavior and unfinished human acceptance evidence.
5. Team 9 source baseline ca6becbefc32ed1462be2dfee54a8230c7361e8a, including PR 15 and its code commit 328669902efc1094facb86bb05db1cc713a1487d. PR: https://github.com/JGraff24/30861_Team9/pull/15 . https://github.com/JGraff24/30861_Team9/tree/ca6becbefc32ed1462be2dfee54a8230c7361e8a . Read November 10 2026. The shared contract does not require Team 12 to adopt Team 9's internal design.

The source map and change log identify which obligations are inherited and which precision decisions this revision supplies. The adoption record stays pending until both teams actually record agreement. No human review, deployed fix or reciprocal run is asserted by an empty record.
