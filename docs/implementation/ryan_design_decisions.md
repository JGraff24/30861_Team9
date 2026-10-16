# Ryan design decisions — 2026-10-16

## Subprocess layering and trusted configuration

Context: Jackson already serializes a runner into a process-group leader, applies Linux
limits and terminates descendants. Ryan must start coverage before loading Dylan's
engine while preserving its constructor configuration.

Choice: serialize the deployment-selected engine at runner construction; unpickle it
only after coverage starts in a fresh child. Inherit the guard's process group. Use JSON
for results, an allowlisted environment and private read-only input chunks <=4 MiB.

Pros: retains engine state and Jackson's safety boundary; clean coverage per invocation;
valid 25 MB inputs survive the inherited file-size ceiling. Cons: two interpreter
startups, extra input copies, trusted-pickle compatibility requirement and Python/Linux
operational dependence. Pickle is not acceptable for caller-supplied configuration.

Alternatives: import a class by name (loses constructor state); fork the engine (coverage
starts after imports and inherits unrelated state); remove Jackson's guard (loses its
independent deadline/process-tree boundary); one large temporary input (observed failure
under inherited RLIMIT_FSIZE). A future explicit immutable engine-spec DTO could replace
pickle after both owners agree on constructor schemas.

Integration consequences: never add a nested process group. Keep Jackson's outer guard
for descendant termination. Engine adapters must be serializable pure configurations.
Only validation errors expose engine-selected public detail; untyped exceptions are
sanitized. Memory limits are per process, not an aggregate cgroup limit.

## Fixed coverage scope and unavailable measurements

Context: measuring only imported files inflates coverage; including HTTP/storage requires
additional request/job attribution unavailable in the current contract.

Choice: enumerate all deployed Python sources under app/workbooks and app/audit, use
coverage.py public analysis for executable and missing lines, and return per-session,
per-invocation, per-revision sets. Report unavailable when instrumentation fails.

Pros: stable denominator across input branches, unexecuted files included, no fabricated
counts and no engine-success penalty for missing optional instrumentation. Cons: only
engine line coverage, filesystem traversal each invocation, and source identity relies
on accurate DEPLOYMENT_SHA configuration. Annotation/import statements count according
to coverage.py; this is not semantic rule coverage.

Alternatives: imported-file-only denominator (misleading), whole-service coverage (needs
new attribution design), checked-in static line manifests (stale after changes), failing
a successful audit when coverage is missing (changes business behavior unnecessarily).

Integration consequences: API and worker deploy identical trees/revisions; repository
rejects revision mismatch and unions line sets. Empty foundation source scope is not
representative of deployed engine coverage. Do not present these counts as whole-system
or accessibility compliance evidence.

## Persistence ownership and preparation

Context: shared ports and Jackson's SQLite repository provide atomic reservations,
READY checks, immutable invocation records and aggregation.

Choice: a synchronous stateless orchestration service over those ports, called off the
ASGI event loop. Reserve supplied IDs before preparation; keep FAILED records on errors.

Pros: restart persistence, isolated concurrency and a single authoritative data store.
Cons: correctness depends on repository transaction semantics; an interrupted PREPARING
session requires operator inspection/explicit cleanup and a new ID. No auto-retry or
implicit reset is attempted.

Alternatives: an in-memory dictionary (loses restart/concurrency semantics), Ryan-owned
SQLite tables (duplicates authority), replacing caller IDs (breaks instructor contract).

Integration consequences: Jackson initializes storage; prepare checks source-store
readiness but does not execute audits. Test identities come from settings, never from
untrusted setup. A READY report delegates aggregation without recording new measurements.

## Scoped cleanup and partial failures

Context: deleting blobs inside SQLite transactions is impossible; concurrent uploads/jobs
must not race namespace deletion. Existing repository methods reserve CLEANED tombstones.

Choice: verify owner, begin cleanup atomically, delete only returned session blob keys,
finish cleanup; on failure retain CLEANED and an actionable cleanup_error. Validate all
requested owners before any deletion, reserve the new session ID, then clean sequentially.

Pros: no ordinary-data wipe, no ID reuse, resumable partial deletion, active jobs refused.
Cons: cleanup of several namespaces is not all-or-nothing; a failed setup reserves a
FAILED new ID; availability of failure recording depends on storage itself.

Alternatives: global reset (unsafe), delete ID on failure (permits reuse/races), resurrect
READY after partial deletion (inconsistent sources), distributed transactions (unsupported).

Integration consequences: blob deletion must be idempotent and namespace constrained;
repository must preserve tombstones when mark_session_failed follows cleanup reservation.
This is not the ordinary-user REQ-14 deletion operation.

## UI credentials, requests and accessibility

Context: a small same-origin UI must work behind /system without storing access tokens
or implying remediation capabilities. Upload POSTs are not safely repeatable.

Choice: Jinja shell, vanilla JS, password-field token used only in headers, no persistent
browser storage, root_path-prefixed URLs, explicit upload and bounded GET polling with
manual recovery using retained IDs. Render all service text with textContent.

Pros: small dependency surface, transparent timing, no duplicate automatic uploads,
escaped hostile text, keyboard controls and live announcements. Cons: reloading loses
credentials/recovery IDs; an upload whose response is lost cannot be recovered through
this page; browser/password-manager behavior is outside application control. Automated
checks cannot replace human assistive-technology testing.

Alternatives: localStorage tokens (unnecessary persistent exposure), query tokens (URL
leakage), automatic POST retries (duplicate workbooks), client framework (unneeded for
this page), unsupported remediation buttons (misrepresent delivered capability).

Integration consequences: authenticate with ordinary user tokens, never test-admin UI
credentials. The root factory serves HTML/static routes only; Jackson attaches it.
Keep the no-compliance language and exact two-rule scope aligned with Dylan's engine.
Manual keyboard, screen-reader and zoom evaluation remain human review obligations.


## Verification follow-up: errors and request deadlines

Context: the 2026-10-16 review found that treating every non-READY report as 409
contradicted the contract's explicit CLEANED→404 rule. A broad Pydantic ValidationError
catch could also turn corrupt stored data into a client 400.

Choice: retain PREPARING/FAILED→409, map owner-authorized CLEANED reports to 404, and
keep ID reservation at 409. Catch malformed JSON/DTO validation only where client input
is parsed; unexpected internal validation failures use the sanitized system 500 path.
Enforce 30-second setup and 10-second report waits with asyncio.timeout around thread-pool
calls. Tests use shorter injected deadlines and blocked repository calls.

Pros: wire behavior matches the explicit contract, errors identify the responsible side,
and blocked synchronous storage does not hold the HTTP response indefinitely. Cons:
Python cannot cancel an already-running repository thread; a timed-out setup can finish
later. This limitation is documented and the session remains inspectable.

Alternatives: one 409 for every state (rejected by contract); catch every validation error
as 400 (misclassifies storage defects); run storage on the event loop (blocks unrelated
requests); forcefully kill threads (unsafe and unsupported). Moving session operations to
independent workers would require new persistence/coordination contracts.

Integration consequences: storage adapters still need bounded I/O; callers must not reuse
a session ID after an uncertain setup outcome. The re-review matrix and independent
verification results are in `docs/evidence/ryan_phase3_verification.md`.

## Verification follow-up: coverage size and concurrent cleanup

Optional coverage shares the bounded IPC envelope with engine output. When instrumentation
alone makes that envelope too large, discard its line sets and return an attributed
unavailable measurement, then apply the same size limit again. This preserves business
results without raising the output cap; the cost is losing detailed coverage for that
invocation. Failing every oversized combined envelope was rejected because it made
optional instrumentation change successful engine behavior. Oversized engine results
still fail, and all computation remains under the original memory/deadline budgets.

Session readiness and report aggregation are separate repository reads. Owner cleanup can
occur between them, so a specific RecordNotFoundError from aggregation maps to 404 after
authorization. Holding a database transaction across service calls would require a new
repository port; treating the race as a generic 500 misclassified an expected lifecycle
transition. Other storage failures remain sanitized 500s. Real SQLite and regression
probes verify this interleaving without broadening cleanup authority.
