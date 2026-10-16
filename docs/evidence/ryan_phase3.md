# Ryan Phase 3 evidence — 2026-10-16

This record separates local tests, assembled snapshot tests, browser automation and
pending human/deployment evidence. No CI, AWS, public deployment or cross-team run is
claimed. All implementation work used one Codex agent; no subagents were used. The user
requested a single local commit and no push/merge/deployment.

## Source and PR identity

Foundation: `ffa6947b6850a5df624e5f48837e029fab8c7272`.
Live GitHub branch/PR heads were checked during implementation, using `git ls-remote`
and the GitHub pull-request API. All matched the existing snapshots:

| Owner | PR | Exact tested head |
|---|---|---|
| Jackson | [#3](https://github.com/JGraff24/30861_Team9/pull/3) | `6e2ebfb47f59c80ef903d712dd8fe322adfe74c5` |
| Dylan | [#4](https://github.com/JGraff24/30861_Team9/pull/4) | `264f89f1af1b20611b8e4912686eda31740843de` |
| Dixon | [#2](https://github.com/JGraff24/30861_Team9/pull/2) | `66d588c6fafcc20fff7f9a541f9e4c29060fa128` |

All three PRs were open; PR #1 was closed. PR #4's metadata update timestamp was
2026-10-16T20:18:14Z, but its code SHA was unchanged. Issue-comment API lists for #2–4
were empty. This does not claim review approval or predict future branch changes.

## Owner and foundation validation

Pinned uv 0.13.1 installed under `/tmp/ryan-tools`; CPython 3.12.15 downloaded into `/tmp`.
`uv sync --locked --group dev` succeeded with the unchanged lockfile. Network downloads
and socket-based tests required approved execution outside the restricted sandbox.
No application dependencies or teammate implementation files were changed.

| Check | Result |
|---|---|
| `uv sync --locked --group dev` | Passed, 83 packages installed |
| `ruff check .` | Passed |
| `mypy app tester` | Passed, strict configuration, 25 source files |
| `python -m pytest tests/ryan tests/unit tests/contract -q` | 158 passed; one upstream Starlette/httpx deprecation warning |
| `python -m scripts.export_schemas --check` | Passed; unchanged foundation catalogs |
| `python -m scripts.check_identity` | Passed, all three golden identity vectors |
| `python -m scripts.smoke_foundation` | Passed; ports 8000/8001 docs 200, unassembled feature paths 404 |
| `node --check app/ui/static/app.js` | Passed |

Owner runner tests use real fresh interpreters for success, configured-engine state,
validation rejection/details, timeout, abrupt crash, malformed IPC, invalid metadata,
oversized output, memory allocation rejection, environment filtering, inherited process
group, read-only inputs, temp cleanup and 25 MB input under an inherited 4 MiB file cap.
Temporary deployment copies prove that unexecuted files remain in the denominator,
branch-specific line sets differ by session, and an unavailable coverage import preserves
successful validation with honest unavailable measurement.

Session tests cover duplicate concurrent reservation, malformed JSON/DTOs, auth, disabled
routes, allowed subjects, foreign/unknown/pending reports, read-only report behavior,
namespace isolation, active-work cleanup refusal and resumable tombstones. UI HTTP tests
check both root and mounted `/system` paths, labels, live status, assets and CSP.

The initial sandbox test runs hung on ASGI socket operations; approved reruns passed.
The initial smoke run explicitly failed with `PermissionError` on socket creation;
its approved rerun passed. These environmental failures were not counted as test passes.

## Actual assembled integration

An isolated `/tmp/ryan-integration` tree was assembled from `git archive` snapshots:
Jackson's tree as the base; Dylan's app/audit and app/workbooks; Dixon's tester, fixture,
contract, support and tester-test files plus its fixture-generator/operator CLI scripts and tester OpenAPI catalog;
Ryan's app/measurement, app/test_interface and app/ui overlaid. Local assembly drivers,
synthetic credential configuration and SQLite/blob data stayed outside Ryan's commit.

The configured factories are listed in the implementation record. The deployment
revision in this synthetic assembly was Dylan's SHA; the actual composite identity is
all three SHAs above plus Ryan's local source, not that SHA alone. Do not reuse this
synthetic revision label for production.

A real clean fixture was accepted through the assembled FastAPI application. A new
service/repository assembly reopened the same SQLite database; Jackson's worker consumed
the durable job, Dylan's child engine completed it, and authenticated retrieval returned
AUDIT_COMPLETE. Repeated report GETs returned unchanged coverage: 633/835 engine lines.

A second SQLite check passed eight-way concurrent ID reservation (one success), reopen,
independent session coverage, revision-mismatch refusal, foreign-admin cleanup rejection,
cleanup finalization failure with persistent CLEANED/error, successful retry, and retention
of the unrelated READY namespace/coverage.

Dixon's actual `TesterService` ran its independent suite against a live loopback Uvicorn
system plus Jackson's worker, not a fake target. Final run:

- 53 outcomes: **52 pass, 0 fail, 0 error, 1 skip**.
- Skip: `P3-AUDIT-FAILURE-ENVELOPE`; no failed audit was observed in that run.
- Code coverage: **698/835** validation/audit lines.
- Tester component coverage: **6/6**; Phase 3 system catalog coverage: **7/8**.
- Stored wire report: [ryan_phase3_http_report.json](ryan_phase3_http_report.json).
- Target was temporary `http://127.0.0.1:18769`, not a deployment URL.

The first integrated run had 49 passes, 3 system errors and 1 skip. All three errors
were legal large-workbook/cell-boundary cases. Investigation found Jackson's guard set
RLIMIT_FSIZE=4 MiB before Ryan copied the input into a single file. Ryan's input chunking
fix preserved the guard and removed all three errors on rerun. This was a Ryan integration
bug, not an unexplained teammate incompatibility. No teammate code was modified.
The initial restart probe omitted X-Test-Session on retrieval, correctly got 404, and
was corrected to use the required header. A teammate-regression collection attempt
initially omitted Dixon's fixture-generator script; assembly was corrected before rerun. The broader teammate regression selection then
had 139 passes and 7 failures because its CLI and catalog were also missing from the
temporary assembly. After copying those recorded Dixon artifacts, the affected selection
passed 8/8 (including one already-passing contract check). Thus all 146 selected unique
teammate regression tests passed across the corrected runs. No teammate implementation
change was needed. Selected files: Jackson storage/execution/API tests, Dixon tester,
deadline and tester-contract tests.

## Browser and accessibility evidence

Playwright 1.64.0 / Chromium 156.0.8078.4 were installed under `/tmp`, outside application
dependencies. The first launch lacked libnspr4/libnss3/libasound; those libraries were
extracted from downloaded packages into `/tmp` without system installation. A sandbox
socket denial then required an approved browser execution. No failed launch is counted
as a browser check.

`tests/ryan/browser_ui.py` completed **12/12 scenarios**: root and `/system`, each with
clean results, findings, terminal audit failure, 75-second polling timeout/recovery,
transport recovery and upload rejection. The browser intercepts HTTP with fixture JSON;
it does not substitute for the separate actual HTTP integration run. Virtual clock
advancement exercises the 75-second limit without a real 75-second wait.

Assertions include keyboard Enter submission, visible focus style, focused results,
exactly one upload POST despite recovery, no test-session header, token in Authorization,
no local/session storage, and hostile filenames/findings/errors rendered as literal text
with no injected images or script execution. HTTP tests separately exercise the actual
Jinja root_path rendering and CSP. These are automated checks, not human accessibility
sign-off. Manual keyboard navigation, screen-reader announcements, 200–400% zoom,
visual/contrast review and real-user workflow review remain pending with Ryan.

Optional reproduction (in a separate environment with native browser libraries):

```sh
python -m pip install playwright==1.64.0
python -m playwright install chromium
python tests/ryan/browser_ui.py
```

Runtime dependencies remain locked through uv; this separate command is only for
optional browser tooling, not application installation.

## Assignment Q1–Q9 evidence index and remaining obligations

| Evidence area | Available fact / pending input |
|---|---|
| Delivered scope/milestones | Ryan modules complete locally; full production assembly remains Jackson's responsibility |
| Release/hosting/configuration | Local branch/commit only; factory configuration documented; AWS/HTTPS/URLs/log evidence pending Jackson |
| Operations and requirement trace | Supports testing/coverage and UI for delivered upload/version/audit paths; ordinary deletion and other unimplemented operations not claimed |
| Test/AC map and coverage | Independent Dixon report above; AC-14 observed-failure branch skipped; full Phase 2 acceptance definitions still needed |
| Regression/static/CI evidence | Local command results above; actual CI run/link pending |
| Models and consistency | Shared DTOs unchanged; state/sequence models match implemented paths; corrected UI scope and input-limit mismatch |
| Cross-team testing/ambiguities | Team 12 approvals and both testing directions pending; no invented pod agreement |
| Limits/next-phase risks | Linux-only resource claims; human accessibility and real-world workbook precision/preservation evidence pending |
| GenAI contribution/reflection | Codex implemented and verified owner work; independent suite exposed a bug owner tests initially missed; added a real inherited-limit regression |

Five de-identified real-world workbooks, two pod reviewers, Excel Accessibility Checker
comparisons, five repeated audits per workbook, the 90% precision assessment, and Excel
repair-warning/preservation experiments require genuine human inputs. Ryan coordinates
reviews; Dylan provides engine expectations; Jackson deployment/approval; Dixon deployed
and cross-team suite runs. No synthetic fixture result is presented as this human evidence.
