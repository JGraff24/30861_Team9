# Implementation decision verification — 2026-10-16

Follow-up requested by Ryan after local commit `a241c28`. Code, shared contracts,
implementation/evidence documents and recorded teammate sources were re-read. The
review found two defects and corrected them; it is not a claim that software has no
remaining defects. No push, merge or deployment was performed.

## Findings and corrections

1. **Medium — cleaned reports returned 409 instead of required 404.**
   `docs/contracts/phase3.md` explicitly requires CLEANED report lookup to return 404
   while retaining a reserved tombstone. `get_code_coverage` now checks CLEANED after
   owner authorization and returns 404. A regression verifies both report 404 and setup
   reuse 409. The earlier implementation note describing all non-READY states as 409 was
   incorrect and has been corrected.
2. **Medium — internal schema failures could be misclassified as client 400.**
   The route wrapper caught every Pydantic ValidationError, including failures inside
   repository/auth/service code. Input errors are now caught only during request parsing;
   internal invalid records produce sanitized system 500 responses. A regression creates
   a real Pydantic error in report aggregation and checks the public envelope.

Four new test cases cover these two defects and independently exercise both setup/report
deadlines. Blocked repository operations remain unfinished while the HTTP deadline
returns 500; the test then releases the worker thread. This verifies the documented
limitation rather than assuming cancellation stops synchronous storage.

## Decision-by-decision review

| Decision | Verified evidence | Remaining constraint |
|---|---|---|
| Shared ports, owner boundaries | Strict mypy; unchanged shared DTO/config/main/dependency files; actual factory assembly | Foundation app still requires Jackson's assembly |
| Atomic caller-supplied IDs | Eight-way real SQLite reservation; duplicate and tombstone tests | Requires repository transaction semantics |
| Persistent session ownership/allowlist | Owner/foreign/user tests; SQLite reopen and independent session records | Provisioned identities remain operator configuration |
| Preparation before READY | Failure-path tests, health check, mark-ready transition | Interrupted/late preparation requires inspection; no automatic reset |
| Scoped two-stage cleanup | Active-job refusal, foreign-owner refusal, unrelated namespace retained; real SQLite resumable CLEANED failure | Multiple target cleanups are sequential, not all-or-nothing |
| Read-only report aggregation | Repeated identical HTTP reports; repository delegation; no engine call or new measurement | No invocation yet honestly reports unavailable |
| HTTP envelope/status isolation | Malformed JSON/query, auth, foreign/missing/pending/cleaned cases; new internal-error regression | Schema validation covers foundation catalogs, not deployment sign-off |
| Thread-pool storage and deadlines | New tests prove 30/10-second mechanism using short injected deadlines while a worker blocks | Timeout cannot kill an in-flight thread; storage needs bounded I/O |
| Serializable trusted engine | Real-child constructor-state test and actual Dylan engine under Jackson guard | Pickle is trusted deployment configuration, never caller input |
| Fresh child and process group | Coverage before engine unpickle; process-group test; Jackson execution/process regressions | Descendant cleanup depends on outer guard; no OS security sandbox claim |
| Read-only chunked input | Mode-0400 tests; 25 MB test under inherited 4 MiB RLIMIT_FSIZE; actual input/cell boundaries pass | Extra copies and two interpreter startups |
| Bounded results and controlled errors | Malformed/oversized IPC, typed rejection, invalid metadata, crash, timeout and memory tests | Linux resource enforcement only; memory is per process |
| Allowlisted environment | Child test confirms no AWS/auth variables; source review confirms five allowed variables | Engine configuration must itself contain only appropriate pure state |
| Fixed engine coverage scope | Deployment-copy branch tests include an unexecuted file; actual report denominator 835 | Not HTTP/storage/UI/whole-system coverage; truthful revision config required |
| Optional instrumentation | Real child with failing coverage import still validates; disabled/non-session tests | Measurement collection itself remains subject to resource budgets |
| Repository measurement ownership | SQLite revision mismatch rejected; session line sets isolated; no second store introduced | Source changes must update deployment revision |
| Same-origin thin UI | 12 fixture-backed browser scenarios plus two real browser/API/worker flows | Lost upload response cannot recover IDs through this page |
| Memory-only token/header use | Browser checks no local/session storage, Authorization header, no X-Test-Session | Browser/password-manager behavior outside app control |
| Bounded polling and explicit recovery | Virtual-clock 75-second timeout, transport recovery, one POST assertion, terminal states | User may explicitly start another GET recovery window |
| Escaped content and CSP | Hostile filename/finding/error tests; live page CSP blocked test-harness string evaluation as intended | No claim of comprehensive penetration testing |
| Accessible structure and focus | Labels/live regions, keyboard Enter, result focus, visible outline, 375px no-horizontal-overflow | Human screen-reader/zoom/keyboard workflow acceptance pending |
| Contrast | Calculated authored foreground/background ratios listed below | Not a full human visual accessibility audit |

No changes were needed to the runner, coverage scope, persistence ownership or UI design
based on this review. Those choices remain appropriate for this contract subject to the
constraints above; they are not universally preferable architectures.

## Repeated checks and actual results

- Locked uv installation: passed; unchanged lockfile, 83 installed packages checked.
- Ruff: passed. Strict mypy: passed, 25 source files.
- Entire root pytest suite: **162 passed**, one upstream Starlette/httpx deprecation warning.
- Foundation schema export check, three identity vectors and live foundation smoke: passed.
- JavaScript syntax check: passed.
- Locked runtime dependency audit, including measurement extra: **no known vulnerabilities
  found** by pip-audit. This is a database-based check at review time, not a security guarantee.
- Recorded teammate regression selection: **147 passed in one run**, including Jackson's
  storage, execution, API and process tests and Dixon's tester/deadline/contract tests.
- Real SQLite persistence/concurrency/cleanup/revision probe: passed again.
- Actual Dixon HTTP suite against local Uvicorn/worker: **52 pass, 0 fail, 0 error, 1 skip**.
  The same `P3-AUDIT-FAILURE-ENVELOPE` observed-failure branch remains skipped; controlled
  worker failures have separate injected regression coverage. Engine coverage remains
  **698/835**, component **6/6**, partial system catalog **7/8**.
- Fixture-backed Chromium suite: **12/12 passed** again.
- Actual Chromium → Jinja/CSP page → project HTTP API → Jackson worker → Dylan engine:
  clean.xlsx completed with **0 findings**; seeded_issues.xlsx completed with **6 findings**.
  Result focus, source metadata, no page errors and **375px reflow** checks passed. No mock
  target was used for these two flows. An agent inspected the resulting narrow screenshot;
  this is explicitly not human accessibility acceptance.

Authored contrast against white, calculated from sRGB relative luminance: body `#172b3a`
14.55:1; primary button `#174e78` 8.76:1; disabled button `#4b5d6b` 6.82:1; error `#9b1c20`
8.14:1; focus `#8e3900` 7.68:1. White button text uses the same ratios.

The first rerun of the temporary HTTP driver used the wrong working directory and could
not locate its fixture; it was rerun from the integration root with a fresh session ID.
The live-browser harness initially used string-evaluation polling blocked by CSP; it was
changed to Playwright locator assertions without weakening the application's CSP. These
harness failures are not counted as successful checks or application defects.

GitHub branch and PR heads were re-read and remain identical to the three full SHAs in
`ryan_phase3.md`. Temporary assembly, scripts, native browser dependencies, runtime data,
logs and screenshot remain under `/tmp`; no teammate branch or implementation changed.
The original evidence record retains the first implementation run; this document records
the expanded follow-up verification rather than rewriting historical results.

## Reproduction and limits

```sh
uv sync --locked --group dev
uv run ruff check .
uv run mypy app tester
uv run pytest -q
uv run python -m scripts.export_schemas --check
uv run python -m scripts.check_identity
uv run python -m scripts.smoke_foundation
node --check app/ui/static/app.js
```

Browser tooling and temporary integration assembly instructions are in the original
[evidence record](ryan_phase3.md). The temporary integrated regression selection was:
`test_jackson_storage.py`, `test_jackson_execution.py`, `test_jackson_api.py`,
`test_jackson_processes.py`, `test_tester.py`, `test_tester_deadlines.py` and
`test_tester_contract.py`, under their recorded owner directories.

Still pending: full accepted Phase 2 definitions, Team 12 confirmation/cross-testing,
real-world workbook reviews and precision/preservation evidence, human assistive-technology
review, CI run evidence, production assembly, deployment and HTTPS evidence. Local synthetic
verification cannot supply those inputs. The completed review does not change those limits.

## Additional stress/concurrency pass — 2026-10-16

At Ryan's request, another review started from local commit `10f93e7` and added probes
outside the previous happy-path coverage. Two further edge cases were reproduced as
failing tests before correction:

- **Oversized optional coverage:** synthetic instrumentation in a real child returned
  large line maps. The combined JSON exceeded the 4 MiB IPC limit and incorrectly failed
  otherwise successful validation. The child now retries serialization with empty line
  maps and an explicit unavailable reason, preserving session/invocation/revision and
  the engine result. If the engine result itself still exceeds the limit, it remains a
  controlled invalid-result failure. The memory and output ceilings are unchanged.
- **Report versus cleanup race:** cleanup could reserve CLEANED after the report service's
  readiness read but before repository aggregation. Jackson's repository then raised
  RecordNotFoundError, which became a 500. The service now maps that specific post-authorization
  absence to 404. A regression and a separate real SQLite probe both passed; the latter
  actually cleans the namespace between the read and aggregation, retaining its tombstone.

A separate real nested-process probe configured an engine to start a sleeping descendant.
Jackson's outer guard timed out and killed the group, including that descendant. The
probe checked that the descendant was absent or terminated (zombie awaiting reaping),
not merely that the runner returned a timeout. No subprocess isolation design changed.

Final results for this pass: **164 root tests passed**; Ruff and strict mypy passed;
foundation schema and identity checks passed; **12/12 Chromium scenarios passed** again;
Dixon's actual local HTTP suite again returned **52 pass, 0 fail, 0 error, 1 skip**.
The skipped observed-audit-failure test remains disclosed. The new coverage stress test
uses injected synthetic instrumentation in a temporary deployment copy; it is not a claim
that the production 835-line engine currently approaches the output limit. Existing
oversized-engine-output and normal measured-child tests still pass.

The change preserves one local implementation commit through amendment. Handoff files
remain untouched/untracked. No push, merge or deployment was performed. The human and
external-evidence limits listed above remain unchanged.
