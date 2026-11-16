# 30861_Team9

Excel Accessibility Hub — Phase 5 workbook auditing, human review, supported corrections,
periodic audits, deadline reminders and workbook deletion.

Upload an authenticated `.xlsx` copy, review six accessibility checks, explicitly approve
supported corrections, and download a separate generated workbook and immutable JSON report.
Schedules follow the newest stored version. Persistent in-application reminders use the supplied
deadline. Results support human review and are not a compliance determination.

## Specification and versions

The [Phase 5 shared specification](docs/contracts/phase5/Phase5_Shared_Specification.md)
and its [context pack](docs/contracts/phase5/README.md) define the current contract.
All 21 adopted pack files match the supplied ZIP byte for byte. Pack validation establishes
artifact consistency; pod adoption and human acceptance are separate records.

| Item | Current source target |
|---|---|
| Specification, context pack and executable suite | **3.0.0** |
| Six-rule audit ruleset | **2.0.0** |
| New audit report | `reportVersion: "2.0.0"`, `specificationVersion: "3.0.0"` |
| Business API / OpenAPI | `/api/v1`, health `apiVersion: "1.0"` / OpenAPI 3.1.1 |
| Independent suite | **395 cases**: 253 inherited + 142 Phase 5 cases |
| System metadata schema | **3**, additive migration from schemas 1/2 |
| Suite / caller / proxy limits | **2,400 / at least 2,430 / 2,700 seconds** |

The pack's 435 normative assertion entries are an inventory, not the executable case count.
The [manifest](tests/fixtures/manifest-phase5.json) binds runnable cases; the
[coverage catalogs](docs/testing/phase5-coverage-catalogs.md) distinguish hosted, local and
human checks. The [design map](docs/design/team9-design-map.json) remains draft 0.1.0 and
requires team confirmation. Historical reports keep their original versions and bytes.

The [Phase 4 pack](docs/contracts/phase4/README.md), including corrected half-up tint vectors,
continues to govern unchanged audit/editor behavior. Finding identities, safe correction
profiles and independent expected workbook results are unchanged.

## Class access and website

Use the new **[Phase 5 Class Access Guide](docs/Team9_Class_Access_Guide_Phase5.md)**
([PDF](docs/Team9_Class_Access_Guide_Phase5.pdf)) for upload, correction, scheduling,
notifications, deletion and instructor testing. The
[Phase 4 PDF](docs/Team9_Class_Access_Guide_Phase4.pdf) and
[Phase 3 PDF](docs/Team9_Class_Access_Guide.pdf) remain historical references.
Use the privately supplied class credentials; local installations generate separate tokens.

| Service | Registered address |
|---|---|
| Website | https://3.139.178.70.sslip.io/system/ |
| System base / docs | https://3.139.178.70.sslip.io/system / https://3.139.178.70.sslip.io/system/docs |
| Tester base / docs | https://3.139.178.70.sslip.io/tester / https://3.139.178.70.sslip.io/tester/docs |

These are the existing registered addresses, not a claim that this branch is deployed.
Hosted testing was deferred at Jackson's request on November 16, 2026 because of the current
Wi-Fi connection. Use [local integration evidence](docs/evidence/phase5_integration.md)
for this combined source and verify the deployed release separately before class testing.
Health alone cannot distinguish Phase 4 from Phase 5; both return exactly:

```json
{"status":"AVAILABLE","apiVersion":"1.0","rulesetVersion":"2.0.0"}
```

Enter a **userA** token without `Bearer` in **User access token**, choose **Connect**, select
an `.xlsx`, and choose **Upload and check**. Save the workbook ID. Review findings and
limitations; supply explicit input for supported **APPLY** actions, submit decisions, and
wait for verification. Download the generated workbook and its JSON report. **Open history**
selects earlier versions/audits; **New version of workbook in history** uploads external edits.
**Re-audit selected version** creates a fresh audit without changing the version order.
Credentials and pending recovery information remain in the tab's memory.

| Rule | Scope | Supported correction after approval |
|---|---|---|
| `ALT_TEXT` | Missing alternative text and objects requiring review | Set title/description on supported DrawingML objects |
| `WORKSHEET_NAME` | Generic worksheet names | Rename supported sheets and understood references |
| `TABLE_HEADER` | Table headers and qualifying plain ranges | Confirm a safe header range or enable a supported table header |
| `MERGED_CELLS` | Populated merged cells | Manual review |
| `LINK_TEXT` | Ambiguous hyperlink labels | Replace a supported simple cell-link label |
| `COLOR_CONTRAST` | Color-only meaning and declared contrast policy | Manual review |

Unsafe/unsupported corrections remain findings. The system does not invent alternative text,
evaluate formulas, follow hyperlinks or run macros. Generated `REMEDIATED` status means an
output was committed; unresolved findings may remain. The **MANUAL REVIEW** action records the
wire decision `MANUAL_REVIEW`; neither it nor **DECLINE** edits workbook content.

## Phase 5 lifecycle

In **Schedule and notifications**, owners select periodic auditing, an integer interval of
1–90 days, and an optional institution-supplied deadline. **Save schedule** replaces all settings.
Disabling periodic auditing leaves deadline reminders enabled; clear the deadline to stop them.
The summary shows enabled state, next due time and selected version. Reviewers have read-only
schedule and notification access when explicitly granted.

Scheduled audits check the stored version only. Changes to an external workbook cannot be
detected until a new version is uploaded. The schedule follows the newest committed version,
including generated outputs, without postponing the due time. Missed intervals coalesce into
one audit while retaining the original cadence. Failed occurrences remain inspectable; a later
interval or manual re-audit creates a new attempt.

Reminders occur at UTC midnight 90, 30 and 7 days before the supplied deadline when the newest
version lacks a successful clean audit. They are durably deduplicated. **DELIVERED** means a
record is visible in **Notification history**, not email delivery. Earlier threshold instants
are not backfilled when a deadline is first configured. Use **Refresh schedule and notifications**
to retrieve current records.

**Delete workbook…** opens a confirmation identifying the workbook. A successful deletion
removes all versions, outputs, reports, findings, schedules and notifications, including registered
application-managed copies. Active work returns a conflict. Partial cleanup retains a recovery
reservation and hides content; the original owner can explicitly resume cleanup. See the guide
for recovery and the [backend runbook](docs/implementation/jackson_phase5.md) for backup/journal
requirements. Ordinary users cannot control the isolated test clock.

## Independent tester

Authorize in tester `/docs` with the privately supplied **operator** token, without `Bearer`.
Call `POST /test/setup` once with exactly:

```json
{"target_url":"https://3.139.178.70.sslip.io/system"}
```

Require 201, save `session_id`, then call `POST /test/run` once with
`{"session_id":"RETURNED_ID"}`. Retrieve the same stored report with `GET /test/report`.
The Phase 5 caller read timeout must be at least **2,430 seconds**. After a lost run response,
retrieve the report and poll 409 within the original deadline. Never automatically repeat an
uncertain setup/upload/re-audit/run POST. Each prepared tester session permits one run.

Trusted server configuration selects own/cross profiles using `TESTER_TEAM_ID` and exact
`TARGET_TEAM_MAP_JSON` entries. Own-team reports require `coverage.model` and a complete valid
design map; cross-team reports omit the key entirely. Grader setup has no credentials or team
flags. `pass`, `fail`, `error` and `skip` remain distinct. Coverage means evaluated scope, not
acceptance: an HTTP-only run cannot count UI component C9 or AC-17. Code measurement covers
`app/workbooks` and `app/audit`; API, storage and UI code are excluded. Auxiliary clock namespaces
are not summed into the main session's line counts.

The existing Team 12 address is `https://13.184.14.226.sslip.io`; confirm its Phase 5 capability,
release SHA, trusted identity map and private access before reciprocal testing. See the
[reciprocal run procedure](docs/testing/phase5-reciprocal-team12.md). No reciprocal run or
hosted Phase 5 result is claimed by this integration.

## Local setup

Use Python 3.12 and uv 0.13.1:

```sh
python3 -m venv .uv-tool
.uv-tool/bin/python -m pip install uv==0.13.1
export PATH="$PWD/.uv-tool/bin:$PATH"
uv python install 3.12
uv sync --locked --group dev
uv run python -m scripts.create_dev_tokens --local-only
```

The generator writes ignored `.env.local` with mode 0600 and refuses overwrite/production.
Copy it privately to `.env` if absent, preserving permissions. On Windows, use the
`.uv-tool\Scripts` equivalents. The service/worker require Linux/WSL or macOS; Linux enforces
the process memory cap. Portable tester checks also run in Windows CI.

For a full isolated Phase 5 suite, set these in private server configuration:

```dotenv
ENABLE_TEST_INTERFACE=true
ENABLE_TEST_AUDIT_FAULTS=true
ENABLE_TEST_CLOCK=true
ENABLE_CODE_MEASUREMENT=true
FIXTURE_MANIFEST_PATH=tests/fixtures/manifest-phase5.json
TESTER_RUN_TIMEOUT_SECONDS=2400
TESTER_TEAM_ID=team9
TARGET_TEAM_MAP_JSON={"http://127.0.0.1:8000":"team9"}
```

Also configure the exact allowed target, its private `TARGET_AUTH_MAP_JSON`, operator hashes,
reviewer grants and session users. No token is valid by default. The clock/fault switches apply
only to fresh isolated sessions and remain disabled by default outside the designated test
configuration. Coverage is optional at runtime (`uv sync --locked --no-dev --extra measurement`).

Run three terminals from the repository root:

```sh
./scripts/run_system.sh
./scripts/run_worker.sh
./scripts/run_tester.sh
```

Equivalent commands are `uv run --locked uvicorn app.main:create_app --factory --host 127.0.0.1
--port 8000 --no-access-log`, `uv run --locked python -m app.jobs.worker`, and `uv run --locked
uvicorn tester.main:create_app --factory --host 127.0.0.1 --port 8001 --no-access-log`.
Use `http://127.0.0.1:8000/`, system `/docs` on port 8000 and tester `/docs` on port 8001.
API/worker share one persistent store; tester uses a separate store. Production uses two worker
slots with graceful draining. Coordinate full-suite windows on the single host.

## Routes and recovery

The [system](docs/contracts/system.openapi.json) and
[tester](docs/contracts/tester.openapi.json) catalogs contain actual schemas and authentication.
The system has 18 business operations over 17 business paths (health is public); the clock POST
shares the existing test-admin authentication exception. System `/test/setup` and `/test/report`
are additional instructor paths; the tester adds `/test/run`.

| Operation | Method and path relative to system base |
|---|---|
| Upload / new version | `POST /api/v1/workbooks` / `POST /api/v1/workbooks/{workbookId}/versions` |
| Version / audit reads | `GET /api/v1/workbooks/{workbookId}/versions/{versionId}` / append `/audits/{auditId}` |
| Manual re-audit | `POST /api/v1/workbooks/{workbookId}/versions/{versionId}/audits` |
| Stored review context | `GET /api/v1/workbooks/{workbookId}/versions/{versionId}/audits/{auditId}/review` |
| Submit decisions / poll | `POST /api/v1/workbooks/{workbookId}/versions/{versionId}/remediations` / `GET /api/v1/remediations/{remediationId}` |
| Source / report | `GET /api/v1/workbooks/{workbookId}/versions/{versionId}/source` / append `/audits/{auditId}/report` to the version path |
| Generated workbook | `GET /api/v1/outputs/{outputId}/workbook` |
| History | `GET /api/v1/workbooks/{workbookId}/history` |
| Schedule | `GET` / `PUT /api/v1/workbooks/{workbookId}/schedule` |
| Notifications | `GET /api/v1/workbooks/{workbookId}/notifications` |
| Isolated clock | `POST /api/v1/test/clock` |
| Owner deletion | `DELETE /api/v1/workbooks/{workbookId}` |

Owners mutate their own resources; granted reviewers read. Test-admin credentials prepare/measure
owned namespaces and advance their clocks, with no business access. `X-Test-Session` selects an
isolated namespace; omission selects ordinary data. Authorization precedes workbook-byte reads.
Derived resources inherit ownership/namespace. Remediation uses `Idempotency-Key`: identical
key/body recovers the original acceptance; a changed request conflicts. The UI retains pending
recovery in memory; keep the tab open and use **Resume pending operation** where offered.
After uncertain uploads/re-audits, inspect history before retrying.

Fresh isolated sessions use a persistent simulated clock starting `2027-01-01T00:00:00Z`.
Clock advancement accepts jobs and records reminders atomically; poll returned audits before
subsequent assertions. Busy due work rejects the entire advance. Explicit identical-time replay
recovers a lost acknowledgment without reissuing accepted events. Ordinary data uses real UTC;
queues, leases, execution deadlines and suite budgets always use real time.

## Verification and acceptance

Fresh commands, results, integration fixes and limitations are recorded in
[the Phase 5 integration record](docs/evidence/phase5_integration.md). PR #16–#19 contribution
records remain historical evidence for their own source revisions. The combined local
regression passed **1,113 tests, 7 skipped**; the real Phase 5 HTTP suite returned
**392 pass / 0 fail / 0 error / 3 skip** in about eight minutes. These are local results,
not a hosted release or completed human acceptance. Browser checks passed **42 simulated
scenarios and 2 real-stack flows**, with **84 axe scans / zero violations**. Detailed boundary
measurements completed **15/15 successful runs**, with identical findings per workload and
a maximum engine time of **12.893 seconds**.

```sh
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy --platform linux app tester scripts
uv run pytest -q
uv run python docs/contracts/phase5/validate_pack.py
uv run python -m scripts.export_schemas --check
uv run python -m scripts.check_identity
uv run python -m scripts.check_phase4_policy
uv run python -m scripts.check_phase5_policy
uv run python -m scripts.build_phase4_manifest --check
uv run python -m scripts.build_phase5_manifest --check
uv run python -m scripts.build_phase5_catalogs --check
uv run python -m scripts.make_synthetic_fixtures --check
uv run python -m scripts.smoke_foundation
uv run python -m scripts.integration_selfcheck --phase5 --evidence-dir /tmp/team9-phase5-evidence
```

The real integration check creates private temporary credentials, loopback ports and isolated
stores, tests restart persistence, then runs the independent suite. It contacts no cloud/pod
endpoint. Its fail/error outcomes fail CI. Frozen Phase 4 suite 2.1.1 requires new-report label
2.1.0, so use Phase 5 suite 3.0.0 for the integrated system; its first 253 cases retain the
inherited assertions and identities. Explicit legacy Phase 3 regression uses
`RULESET_VERSION=1.0.0` and `FIXTURE_MANIFEST_PATH=tests/fixtures/manifest.json`.

`scripts.tester_selfcheck --phase5 --profile own` and `--profile cross` use a labeled fake to
verify tester behavior. They do not verify the system. Browser scripts under `tests/ryan/`
require optional Playwright/Chromium and axe tooling; see
[Ryan's instructions](docs/implementation/ryan_phase5.md). Runtime dependency scanning requires
network access:

```sh
uv export --locked --no-dev --extra measurement --no-emit-project --format requirements-txt --output-file /tmp/team9-runtime-requirements.txt > /dev/null
uv run pip-audit --require-hashes --disable-pip --cache-dir /tmp/team9-audit-cache -r /tmp/team9-runtime-requirements.txt
```

Automated results do not close the human acceptance gates: Excel no-repair inspections,
other-team fixture/answer-key review, keyboard/interface validation, five-real-workbook
precision adjudication and pod adoption. Live S3/version/backup deletion, restore and monitoring
response also require deployment evidence. The outstanding Team 12 rename investigation remains
visible; no rules or expectations were changed to suppress it.

## Ownership and deployment

| Owner | Scope | Phase 5 notes |
|---|---|---|
| Jackson | APIs, persistence, lifecycle, durable jobs, integration/deployment | [Backend](docs/implementation/jackson_phase5.md) |
| Dylan | Deterministic six-rule engine, preservation and evidence tooling | [Engine](docs/implementation/dylan_phase5.md) |
| Dixon | Independent tester, fixtures, coverage profiles and CI | [Tester](docs/implementation/dixon_phase5.md) |
| Ryan | Test clock, measurement, lifecycle UI and accessibility | [UI](docs/implementation/ryan_phase5.md) |

Use [the deployment runbook](deploy/README.md) and [automatic deployment procedure](deploy/AUTODEPLOY.md).
Development Compose is `docker compose -f deploy/compose.yaml up --build` with private inputs;
use `http://system:8000` in the Docker tester's exact target maps. Production uses persistent
storage, private S3, HTTPS, two worker slots and the installed Phase 5 manifest. Its private
URL→team identity map must be configured before tester setup. Push CI for the exact main SHA
controls automatic host activation; a local branch is not a deployment.

Schema 3 preserves earlier audits/reports. Successful deletion must also purge registered
managed content copies. Preserve the newest independent deletion journal across restore;
never restore an older journal or Phase 4 binary after an acknowledged Phase 5 deletion.
See the backend runbook before activation. This integration does not provision resources or
change the running host.
