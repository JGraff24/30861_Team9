# 30861_Team9

Excel Accessibility Hub — Phase 4 workbook auditing, human review and supported corrections.

The system accepts authenticated `.xlsx` uploads, preserves their original bytes, and runs
six accessibility checks through a durable worker. Owners review findings and approve
supported corrections, then download a separate generated workbook and its JSON audit
report. An independent HTTP tester exposes the instructor setup/run/report interface.

Results support human review. An empty finding list or a generated output is not a
compliance determination. Scheduling, reminders, test-clock controls and ordinary workbook
deletion remain Phase 5 work.

## Shared specification and versions

The current intra-pod contract is the
[Phase 4 Shared Implementation Specification](docs/contracts/phase4/Phase4_Shared_Specification.md).
Read it with the [pack README](docs/contracts/phase4/README.md) and
[correction note](docs/contracts/phase4/CORRECTIONS.md).

| Version | Current target |
|---|---|
| Shared specification | **2.1.0** |
| Audit ruleset | **2.0.0** |
| Corrected context pack | **2.1.1** |
| Executable suite manifest | **2.1.1**, 253 cases |
| Business API generation | `/api/v1`, health `apiVersion: "1.0"` |

The correction note supersedes the baseline document's suite target of 2.1.0. It fixes
the HSL half-up tint expectations for base `336699`: `-0.5` produces `1A334D`, and `0.5`
produces `8CB3D9`. The intended formula, specification version and ruleset are unchanged.
The [wire schemas](docs/contracts/phase4/phase4-wire-schemas.json),
[examples](docs/contracts/phase4/phase4-examples.json),
[policy vectors](docs/contracts/phase4/phase4-policy-vectors.json) and
[test inventory](docs/contracts/phase4/phase4-test-plan.json) accompany the specification.
The inventory describes required assertions; the
[executable manifest](tests/fixtures/manifest-phase4.json) binds the runnable cases.

Historical audits and retained reports keep their original versions and results.
The [Phase 3 contract](docs/contracts/phase3.md) and
[Phase 3 integration record](docs/evidence/phase3_integration.md) are historical references.

## Phase 4 capabilities

| Rule | Screening scope | Supported correction after approval |
|---|---|---|
| `ALT_TEXT` | Missing alternative-text titles/descriptions and objects needing manual review | Set title and description on supported DrawingML objects |
| `WORKSHEET_NAME` | Generic worksheet names | Rename supported sheets and update understood references |
| `TABLE_HEADER` | Table headers and qualifying plain ranges | Confirm a safe proposed header range or enable a supported table header |
| `MERGED_CELLS` | Populated merged cells | Manual review |
| `LINK_TEXT` | Ambiguous hyperlink labels | Replace the label of a supported simple cell link |
| `COLOR_CONTRAST` | Color-only meaning and contrast within the declared display policy | Manual review |

Unsafe or unsupported corrections remain manual findings. The system does not invent
alternative text, evaluate formulas, follow hyperlink destinations, run macros, or edit an
external live workbook. Only explicitly approved supported changes are applied.

New uploads receive fresh version identities. Manual re-audits create new immutable audits
on the selected version. Review context comes from the same inventory and safety assessment
as the audit. Remediation builds a separate candidate, validates it, verifies preservation
and approved effects, and audits the output before publication. Downloads verify stored
integrity. Reports retain findings, decisions, committed changes, unresolved items,
limitations and provenance; later work does not rewrite an earlier report.

`REMEDIATION_AVAILABLE` means supported corrections await decisions. `REVIEW_REQUIRED`
means the findings require manual review. A generated version's `REMEDIATED` status means
an output was committed; its audit can still contain unresolved findings. `DECLINE` and
`MARK_REVIEWED` record decisions without changing workbook content.

## Instructor guides and hosted access

Use the **Phase 4 guide** for current instructor access, review/correction steps and hosted
testing. The original Phase 3 PDF remains available at its existing path for historical reference.

| Guide | Updated | Scope |
|---|---|---|
| [Phase 4 Class Access Guide (PDF)](docs/Team9_Class_Access_Guide_Phase4.pdf) | November 1, 2026 | Six-rule review, remediation, version history, downloads and the 2.1.1 tester suite |
| [Phase 3 Class Access Guide (PDF, historical)](docs/Team9_Class_Access_Guide.pdf) | October 18, 2026 | Original two-rule upload/audit workflow and Phase 3 tester timing |

Both guides retain the shared class application credentials for the deployed instance;
local installations use their own generated credentials. The Phase 4 guide includes a
target-health check before running the suite.

| Service | URL |
|---|---|
| Website | https://3.139.178.70.sslip.io/system/ |
| System base URL | https://3.139.178.70.sslip.io/system |
| System API documentation | https://3.139.178.70.sslip.io/system/docs |
| Tester base URL | https://3.139.178.70.sslip.io/tester |
| Tester API documentation | https://3.139.178.70.sslip.io/tester/docs |

Before a Phase 4 exercise, check the selected target's `GET /api/v1/health` relative to its
base URL. Team 9's endpoint is
https://3.139.178.70.sslip.io/system/api/v1/health. The public Phase 4 response is exactly:

```json
{"status":"AVAILABLE","apiVersion":"1.0","rulesetVersion":"2.0.0"}
```

For website uploads, enter the Phase 4 guide's **userA** token in **User access token** without
a `Bearer` prefix and choose **Connect**. Then select an `.xlsx` workbook and choose
**Upload and check**.

Review the findings and limitations, select actions, and provide the requested values for
**APPLY**. Choose **Submit selected decisions**, wait for verification, and download the
separate remediated workbook and its JSON audit report. Save the workbook ID and use
**Open history** to select versions/audits. Choose **New version of workbook in history**
for another upload, or **Re-audit selected version** before correcting older work.

Owners can mutate their own resources. A **reviewer** connects with an explicitly granted
read-only identity and opens a granted workbook by ID. **test-admin** credentials authorize
system testing endpoints and grant no business access. **operator** credentials authorize
the independent tester. People using the same token share the same application identity.

Credentials, drafts and pending recovery state stay in the browser tab's memory. Keep the
tab open and use **Resume pending operation** after uncertain remediation acceptance.
After an uncertain upload or re-audit, inspect history before retrying.

For hosted testing, open the tester API documentation and **Authorize** with Team 9's
**operator** token without a `Bearer` prefix. Execute `POST /test/setup` with one of
these exact approved targets:

```json
{"target_url":"https://13.184.14.226.sslip.io"}
```

Use `https://3.139.178.70.sslip.io/system` as `target_url` for a Team 9 self-test.
Team 12's target credentials are configured privately on the server; keep using Team 9's
operator token to authenticate to Team 9's tester. The setup body needs no token fields.

Require setup to return 201, retain its `session_id`, then execute `POST /test/run` once
with `{"session_id":"PASTE_RETURNED_SESSION_ID"}`. Retrieve the stored report through
`GET /test/report` with the same session ID. Use a client read timeout of at least
1,830 seconds for Phase 4 (630 for a retained Phase 3 installation). After a lost run response,
retrieve the report rather than repeating the run POST; poll 409 report responses within
the original deadline. Do not automatically retry an uncertain setup POST. Each prepared
session runs once.

For reciprocal testing, Team 12 uses its own tester and operator token, targeting
`https://3.139.178.70.sslip.io/system` with Team 9's system credentials.

## Phase 4 tester

The independent suite has 253 hosted cases generated from the corrected 2.1.1 pack,
Team 9's synthetic fixture packs and its own package comparator. It uses frozen fixture
bytes and expectations independent of production rule/editor helpers. Phase 4 is the default.
Phase 4 runs take up to 1,800 seconds, so callers need a read
timeout of at least 1,830 seconds. See
[the Phase 4 implementation notes](docs/implementation/dixon_phase4.md) and the
[adopted specification pack](docs/contracts/phase4/).

`pass`, `fail`, `error` and `skip` remain distinct report outcomes. HTTP-only component
coverage excludes the review UI. Code coverage measures `app/workbooks` and `app/audit`,
including editing and preservation, and excludes API, storage and UI code. Coverage
percentages alone do not establish acceptance.

## Local setup

Use Python 3.12 and uv 0.13.1. Install uv if needed:

```sh
python3 -m venv .uv-tool
.uv-tool/bin/python -m pip install uv==0.13.1
export PATH="$PWD/.uv-tool/bin:$PATH"
uv python install 3.12
uv sync --locked --group dev
```

On Windows, use `.uv-tool\Scripts\python.exe` and add `.uv-tool\Scripts` to PATH.
Run the system and worker on Linux/WSL or macOS; Linux enforces the process memory cap.
The independent tester also has Windows CI checks. Use one system API, one durable worker
and one tester API process with separate persistent SQLite stores.
Production runs that worker with `--concurrency 2`: two bounded execution slots prevent
one large audit from blocking every other session. The default local command uses one slot.

Both settings classes read `.env`; environment variables take precedence. No credential is
valid by default. Generate private loopback development credentials explicitly:

```sh
uv run python -m scripts.create_dev_tokens --local-only
```

This creates ignored `.env.local` with POSIX mode 0600 and refuses overwrites/production.
Copy it privately to `.env` if that file does not exist, preserving its permissions.
The target token map includes user A/B, reviewer and test-admin credentials; the local operator
token is stored in the private file. Keep these generated local credentials out of commands,
Git, images and evidence.

For full Phase 4 tester runs against an isolated local target, set these trusted server
options in the private configuration:

```dotenv
ENABLE_TEST_INTERFACE=true
ENABLE_TEST_AUDIT_FAULTS=true
```

The generator leaves these disabled. The one-shot fault control applies only through an
authorized test-admin setup for a fresh isolated session. Set
`ENABLE_CODE_MEASUREMENT=true` if actual line measurement is wanted. The development
dependency group includes coverage; production uses the optional `measurement` extra.
Use ordinary user credentials in the UI, available at `http://127.0.0.1:8000/`.

Run in three terminals from the repository root:

```sh
./scripts/run_system.sh
./scripts/run_worker.sh
./scripts/run_tester.sh
```

Equivalent commands:

```sh
uv run --locked uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
uv run --locked python -m app.jobs.worker
uv run --locked uvicorn tester.main:create_app --factory --host 127.0.0.1 --port 8001 --no-access-log
```

System docs: `http://127.0.0.1:8000/docs`; tester docs: `http://127.0.0.1:8001/docs`.
State under `state/` survives restart. Stop processes with Ctrl+C.

System and tester use separate persistent SQLite stores. Phase 4 is the default. For
explicit legacy Phase 3 development/regression runs, select both `RULESET_VERSION=1.0.0`
and `FIXTURE_MANIFEST_PATH=tests/fixtures/manifest.json`. This does not change the versions
or results of existing records.

## Assembly and routes

Concrete server-owned defaults now assemble the four owners' implementations:

| Setting | Default factory |
|---|---|
| `ENGINE_FACTORY` | `app.audit.engine:OoxmlWorkbookEngine` |
| `RUNNER_FACTORY` | `app.measurement.runner:MeasuredEngineRunner` |
| `SESSION_SERVICE_FACTORY` | `app.test_interface.service:PersistentSessionService` |
| `TEST_ROUTER_FACTORY` | `app.test_interface.routes:create_router` |
| `UI_ROUTER_FACTORY` | `app.ui.routes:create_router` |

Explicit injected services remain available for tests. Factory overrides are trusted server
configuration. No HTTP caller selects a factory. Disabled/missing integrations fail explicitly.
Coverage is optional at runtime: install `uv sync --locked --no-dev --extra measurement` when
enabled, or build the Docker `measurement` target for both system and worker.

The [system](docs/contracts/system.openapi.json) and
[tester](docs/contracts/tester.openapi.json) OpenAPI catalogs use version 3.1.1.
These thirteen business paths are relative to the registered system base URL; retain
`/system` for the hosted target.

| Business operation | Method and path |
|---|---|
| Public health | `GET /api/v1/health` |
| Initial upload | `POST /api/v1/workbooks` |
| New upload version | `POST /api/v1/workbooks/{workbookId}/versions` |
| Version retrieval | `GET /api/v1/workbooks/{workbookId}/versions/{versionId}` |
| Audit retrieval | `GET /api/v1/workbooks/{workbookId}/versions/{versionId}/audits/{auditId}` |
| Manual re-audit | `POST /api/v1/workbooks/{workbookId}/versions/{versionId}/audits` |
| Stored review context | `GET /api/v1/workbooks/{workbookId}/versions/{versionId}/audits/{auditId}/review` |
| Submit decisions | `POST /api/v1/workbooks/{workbookId}/versions/{versionId}/remediations` |
| Remediation polling | `GET /api/v1/remediations/{remediationId}` |
| Source/selected-version download | `GET /api/v1/workbooks/{workbookId}/versions/{versionId}/source` |
| Generated output download | `GET /api/v1/outputs/{outputId}/workbook` |
| JSON audit report download | `GET /api/v1/workbooks/{workbookId}/versions/{versionId}/audits/{auditId}/report` |
| History | `GET /api/v1/workbooks/{workbookId}/history` |

Owner/reviewer permissions and namespace checks precede blob reads. Derived records
inherit their source owner and namespace. Remediation requires an `Idempotency-Key`:
identical retries with the same key/body recover the original acceptance; changed requests
under that key conflict. Processing failure does not publish a successful partial output.

With the test interface enabled, the system also exposes POST `/test/setup` and GET
`/test/report`. The tester exposes POST `/test/setup`, POST `/test/run`, and GET `/test/report`.
Configure its exact `ALLOWED_TARGET_URLS`, `TARGET_AUTH_MAP_JSON` and operator hashes privately.
Never retry uncertain setup/upload/run POSTs; recover a lost run response by reading its report.

Tester business calls use `X-Test-Session` for isolation, apart from deliberate negative
probes. Ordinary UI data is outside those namespaces. Cleanup is caller-owned and refuses
active work; ordinary deletion remains unavailable.

## Verification

[Phase 4 integration CI run 39](https://github.com/JGraff24/30861_Team9/actions/runs/41323937522)
passed on the source merged in [PR #12](https://github.com/JGraff24/30861_Team9/pull/12):

- Linux: **972 passed, 4 skipped**; Windows portable checks: **318 passed**.
  The four ordinary-pytest skips require live configuration exercised by the self-checks.
- Real Phase 4 API/worker/engine/SQLite/filesystem/independent HTTP suite:
  **253/253 cases and 4 live tester acceptance tests passed**.
- Legacy real HTTP suite: **63 passed, 1 intentional observational-fault skip**, plus
  **4 live tester acceptance tests**. Separately labelled fake-target self-checks passed.
- Browser verification: **26 simulated cases and 2 real workflows** passed.
- Lint, formatting, strict typing, catalogs, policy vectors, fixture/manifest checks,
  the actual production Docker build/smoke and locked runtime dependency audit passed.

The [integration record](docs/evidence/phase4_integration.md) describes the changes and
verification scope. The merged PR retains the final CI results and the resolved
boundary-upload queue contention. CI retains the real Phase 4 report and worker events
as an artifact; simulated targets are labelled separately from actual integration.

Reproduce the repository checks:

```sh
uv sync --locked --group dev
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy --platform linux app tester scripts
uv run pytest -q
uv run python -m scripts.export_schemas --check
uv run python -m scripts.check_identity
uv run python -m scripts.check_phase4_policy
uv run python -m scripts.build_phase4_manifest --check
uv run python -m scripts.make_synthetic_fixtures --check
uv run python -m scripts.smoke_foundation
uv run python -m scripts.integration_selfcheck --evidence-dir /tmp/team9-integration-evidence
uv run python -m scripts.integration_selfcheck --phase4 --evidence-dir /tmp/team9-phase4-evidence
```

The integration self-check uses random temporary loopback ports, isolated databases and private
credentials. It tests the real engine/API/worker, restart persistence, the selected tester suite,
report retention and four live tester acceptance checks. It contacts no AWS or Team 12 endpoint.
Two Linux memory-limit tests skip on macOS. Live acceptance tests skip in an ordinary pytest run;
the integration self-check configures and runs them. CI runs the full suite and real integration
on Linux; portable tester/schema checks also run on Windows.

The separate `scripts.tester_selfcheck` uses a labeled fake target to test the tester itself.
Fixture reviews and review records were excluded by Jackson; no such reviews are claimed.
Browser automation lives in `tests/ryan/browser_ui.py` and `tests/ryan/browser_real.py`
and requires optional Playwright tooling; automated API results do
not establish manual keyboard/screen-reader conformance.

Runtime dependency scanning (requires network):

```sh
uv export --locked --no-dev --extra measurement --no-emit-project --format requirements-txt --output-file /tmp/team9-runtime-requirements.txt > /dev/null
uv run pip-audit --require-hashes --disable-pip --cache-dir /tmp/team9-audit-cache -r /tmp/team9-runtime-requirements.txt
```

For an approved target, set `TEAM9_TESTER_TOKEN` privately and run:

```sh
uv run python -m scripts.run_suite --tester-url http://127.0.0.1:8001 --target-url http://127.0.0.1:8000 --target-revision unknown --manifest tests/fixtures/manifest-phase4.json --output report.json --record run-record.json
```

This selects the Phase 4 manifest and its read timeout. The opt-in GitHub Actions
`live-suite` workflow requires configured access secrets; select **phase4** explicitly
because its manual form retains a legacy Phase 3 default. Hosted reciprocal runs and
manual accessibility assessments are distinct from local/CI evidence. Automated checks
alone do not establish full Phase 4 acceptance.

## Ownership and deployment

| Owner | Responsibility | Phase 4 implementation notes |
|---|---|---|
| Jackson | Backend APIs, authorization, persistence, immutable storage, durable jobs, integration and deployment | [Backend](docs/implementation/jackson_phase4.md) |
| Dylan | Workbook interpretation, six audit rules, safe OOXML corrections and preservation | [Engine](docs/implementation/dylan_phase4.md) |
| Dixon | Independent HTTP tester, fixtures, answer keys and tester CI | [Tester](docs/implementation/dixon_phase4.md) |
| Ryan | System testing interface, session measurement, execution limits and review UI | [UI and measurement](docs/implementation/ryan_phase4.md) |

Owner handoffs retain their original contribution-time context. Use the integration
record, merged PR and current shared pack for later resolutions, including the tint
correction; earlier handoff statements about absent modules or pending tint agreement
are historical.

Development Compose: `docker compose -f deploy/compose.yaml up --build` with private `.env.local`.
Configure the tester target/access map for `http://system:8000`; container loopback refers to
itself. Use `TEAM9_BUILD_TARGET=measurement` when enabling coverage. Named volumes preserve
system/tester state independently. The existing production deployment uses EC2, persistent
storage, private S3 and HTTPS. See the deployment runbook for operation and the original
provisioning templates, and [automatic deployment](deploy/AUTODEPLOY.md) for CI-gated updates
of the existing host. Additional provisioning requires separate operator authorization.

System schema 2 migrations preserve Phase 3 records; tester sessions retain immutable
fixture-plan snapshots. Original and generated workbooks, historical audits and retained
reports survive restart and release. The CD poller requires successful **push CI for the
exact main SHA**. It drains active work, stops writers before backup/migration, and checks
the candidate privately before opening ingress. Pre-publication rollback can restore the
quiescent database snapshots. Once ingress may have opened, recovery retains the new data
and pinned release. Green CI verifies source; confirm host health and deployment state
through the runbook before assuming that a particular release is active.
