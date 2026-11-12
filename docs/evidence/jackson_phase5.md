# Jackson — Phase 5 backend verification

Executed November 12, 2026 on macOS arm64 with Python 3.12.15 and the locked development dependencies. Base: `ca6becbefc32ed1462be2dfee54a8230c7361e8a` (main/PR 15); review branch: `codex/jackson-phase5-backend`. The original workspace virtual environment referenced a missing interpreter, so verification used an isolated `/tmp/team9-phase5-venv` built with uv 0.13.1 and `uv sync --locked --group dev`. No dependency or lockfile changes were required.

The original `Phase5_Agent_Context_Pack.zip` SHA256 is `903ee2c80aef678567e6ef13570b28931112127d27c698adf85dab806792d990`. The original ZIP and shared files were not changed. Its offline validator passed all pack hashes, 59 schema definitions, 26 valid and 21 negative examples, and 435 inventory entries. This is artifact consistency, not pod adoption or executed acceptance.

## Fresh checks

| Check | Command actually executed, shortened only by removing the interpreter/tool directory | Result |
|---|---|---|
| Full regression, final source | `pytest -q --tb=short --junitxml=/tmp/team9-phase5-release-review-junit.xml` | 1,036 passed, 6 skipped, 0 failures/errors, 2 warnings; 127.20 seconds |
| Focused Phase 5 backend integration | `pytest -q tests/unit/test_phase5_backend.py --tb=short` | 28 passed; 25.36 seconds |
| Lint | `ruff check .` | Passed |
| Formatting | `ruff format --check .` | 164 files checked, passed |
| Types | `mypy --platform linux app tester scripts` | 103 source files, passed |
| Generated API catalogs | `python -m scripts.export_schemas --check` | System and tester catalogs current; full regression also validates schemas |
| Patch whitespace | `git diff --check` | Passed |
| Installed artifact | `uv build --wheel --no-build-isolation --out-dir /tmp/team9-phase5-dist --python /tmp/team9-phase5-venv/bin/python`, install into isolated target, factory smoke | Wheel built; imports resolved from the installed artifact; schema-3 initialization, six-rule health, protected schedule API and Phase 5 catalog passed |
| Pack validation | `python /tmp/team9-phase5-context/validate_pack.py` | Passed, including hashes |

The full suite needed loopback access outside the filesystem/network sandbox to run existing local HTTP-server tests. Initial exploratory runs exposed an introduced local UTC-import bug, an ordinary-time acceptance regression and stale Phase 4 route/schema/timing assertions; these were corrected and the final full run above is green. Existing hash-pinned specification files, fixture answers, saved reports and tester expectations were not rewritten. Earlier failed exploratory output is retained in temporary logs, not presented as passing evidence.

Four skips require a configured live tester URL/target/token. Two skips require Linux RLIMIT_AS enforcement. The two warnings are the existing Starlette TestClient deprecation and openpyxl's conditional-formatting-extension warning. macOS execution is not a substitute for the Linux resource-limit or hosted checks. No Docker executable was available, so a Docker image build and container-specific smoke were not run; the installed wheel was exercised directly.

## Requirement-to-evidence map

| Backend obligation | Evidence and scope |
|---|---|
| AC-12 schedule configuration, version selection, cadence, restart and claims | Real HTTP/SQLite/engine/worker tests accept strict intervals 1/90 and reject wrong lexical/types, preserve replacement cadence, coalesce missed periods, follow uploaded/generated creation order, replay after restart and roll back busy/over-limit/calendar failures. Two real worker processes accept one occurrence and drain cleanly; observed dispatch completes within five real seconds. Confirmed locally. |
| AC-13 eligibility, UTC reminders and deduplication | Real stored clean/unresolved/failed states, pre-dispatch clean suppression, exact thresholds, leap-year dates, forward jumps, no backfill, deadline changes, durable consideration and terminal records. Confirmed locally. |
| AC-14 controlled failures and preservation | All seven isolated fault families; real metadata rollback, final clock-commit failure after notification insertion, lost storage acknowledgment, partial deletion, restart, checkpoint failure and source/output preservation. S3 version/delete-marker/error checks use a labeled client double. Backend failures confirmed locally; actual AWS behavior unverified. |
| AC-15 ownership and namespace boundaries | Owner, granted/ungranted reviewer, foreign user, missing/invalid credentials and test-admin business restrictions across new routes; clock-service ownership, disabled/legacy/unknown namespaces and setup isolation. Backend confirmed locally. Ryan's HTTP clock authentication/precedence remains integration work. |
| AC-16 erasure, recovery and restores | Source/generated artifacts, reports, jobs, decisions/keys and scheduler/notification records erased; registered SQLite/filesystem backups purged; active storage fencing; independent journal replay hides restored workbooks; corrupt journal fails closed. Confirmed on local adapters. Live versioned S3 permissions, retained snapshot/copy inventory, encrypted host storage and actual restore rehearsal remain release evidence. |
| Deployment protection, concurrency and monitoring | Existing worker/remote-session deployment tests pass with 2,700-second protection; real scheduler processes emit structured monitor/dispatch/drained events. Clock activity renews protection with real time. Backup migration, startup rollback and bounded-defer unit tests pass. Hosted log routing/operator response and the actual 2,400-second Phase 5 suite remain unverified. |
| Historical compatibility | Full Phase 4 engine/remediation/persistence/tester regression passes; an explicit schema-2 reopening test preserves retained Phase 4 report bytes. New report labels are 3.0.0; ruleset and report format remain 2.0.0. |

The new test module has 28 executable cases after parameter expansion. It is backend integration evidence, not an independent cross-team tester or a completed Phase 5 suite. No C9/AC-17 UI coverage, Excel no-repair approval, human fixture review, five-real-workbook precision study, hosted alarm installation or reciprocal acceptance is claimed.

Operational rollout, backup registration, S3 permissions, restore safeguards, monitoring response and Ryan/Dixon handoffs are documented in [the implementation note](../implementation/jackson_phase5.md). No live data, IAM role, deployment, registry/adoption record or shared context pack was modified.
