# 30861_Team9

Excel accessibility hub — Phase 3 engine with a proposed Phase 4 backend.

The Phase 4 backend uses corrected context pack 2.1.1 (suite target 2.1.1),
with specification 2.1.0 and ruleset 2.0.0 unchanged, and adds version
uploads, re-audits, review context, durable remediation orchestration, downloads,
reports and history. **The real six-rule engine/editor is still missing**, so the
default 2.0.0 configuration refuses unsupported processing with 503. This work was
not deployed. See the [backend integration/handoff](docs/implementation/jackson_phase4.md)
and [verification evidence](docs/evidence/jackson_phase4.md). Use an explicit
`RULESET_VERSION=1.0.0` only for legacy Phase 3 development/regression checks.

The following Phase 3 operating reference describes the existing two-rule engine.

The system accepts authenticated `.xlsx` uploads, retains immutable sources, runs durable
asynchronous audits for missing alternative text and generic worksheet names, and exposes
version/audit results through HTTP and a small upload/results UI. The independent tester
provides setup, single-use runs and retained reports through the instructor interface.
No findings means only that these two checks found no issues; it is not a compliance decision.

See the [integration review and evidence](docs/evidence/phase3_integration.md),
[shared contract](docs/contracts/phase3.md) and [deployment runbook](deploy/README.md).
The system and tester are deployed on AWS over HTTPS. The hosted tester is configured
to test both Team 9's and Team 12's systems; see the access guide below.
Backend remediation orchestration, history and downloadable reports now have
Phase 4 interfaces; real corrections depend on the engine handoff above.
Scheduling and ordinary deletion remain Phase 5 work.

## Hosted access

Read the [Team 9 Class Access Guide (PDF)](docs/Team9_Class_Access_Guide.pdf), updated
October 18, 2026, for shared class application tokens, website instructions and the hosted
tester workflow. These credentials apply to the deployed instance; local installations
use their own generated credentials.

| Service | URL |
|---|---|
| Website | https://3.139.178.70.sslip.io/system/ |
| System base URL | https://3.139.178.70.sslip.io/system |
| System API documentation | https://3.139.178.70.sslip.io/system/docs |
| Tester base URL | https://3.139.178.70.sslip.io/tester |
| Tester API documentation | https://3.139.178.70.sslip.io/tester/docs |

For website uploads, enter the guide's **userA** token in **User access token** without
a `Bearer` prefix, select an `.xlsx` workbook and choose **Upload and check**.

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
630 seconds. After a lost run response, retrieve the report rather than repeating the
run POST; poll 409 report responses within a deadline. Do not automatically retry an
uncertain setup POST. Each prepared session runs once.

For reciprocal testing, Team 12 uses its own tester and operator token, targeting
`https://3.139.178.70.sslip.io/system` with Team 9's system credentials.

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
For the tester workflow, explicitly set `ENABLE_TEST_INTERFACE=true` and optionally
`ENABLE_CODE_MEASUREMENT=true` in the private configuration. The generator leaves both disabled.
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

The original four Phase 3 operations are public GET `/api/v1/health`, authenticated POST
`/api/v1/workbooks`, GET `/api/v1/workbooks/{workbookId}/versions/{versionId}` and its
`/audits/{auditId}`. Version retrieval rereads and hashes the source. Owner/reviewer permission
checks precede content reads; test-admin credentials have testing powers only.

With the test interface enabled, the system also exposes POST `/test/setup` and GET
`/test/report`. The tester exposes POST `/test/setup`, POST `/test/run`, and GET `/test/report`.
Configure its exact `ALLOWED_TARGET_URLS`, `TARGET_AUTH_MAP_JSON` and operator hashes privately.
Never retry uncertain setup/upload/run POSTs; recover a lost run response by reading its report.

## Verification

```sh
uv sync --locked --group dev
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy app tester scripts
uv run pytest -q
uv run python -m scripts.export_schemas --check
uv run python -m scripts.check_identity
uv run python -m scripts.make_synthetic_fixtures --check
uv run python -m scripts.smoke_foundation
uv run python -m scripts.integration_selfcheck --evidence-dir /tmp/team9-integration-evidence
```

The integration self-check uses random temporary loopback ports, isolated databases and private
credentials. It tests the real engine/API/worker, restart persistence, the 64-case tester suite,
report retention and four live tester acceptance checks. It contacts no AWS or Team 12 endpoint.
Two Linux memory-limit tests skip on macOS. Live acceptance tests skip in an ordinary pytest run;
the integration self-check configures and runs them. CI runs the full suite and real integration
on Linux; portable tester/schema checks also run on Windows.

The separate `scripts.tester_selfcheck` uses a labeled fake target to test the tester itself.
Synthetic fixture expectations still need human pod review. Browser automation lives in
`tests/ryan/browser_ui.py` and requires optional Playwright tooling; automated API results do
not establish manual keyboard/screen-reader conformance.

Runtime dependency scanning (requires network):

```sh
uv export --locked --no-dev --extra measurement --no-emit-project --format requirements-txt --output-file /tmp/team9-runtime-requirements.txt > /dev/null
uv run pip-audit --require-hashes --disable-pip --cache-dir /tmp/team9-audit-cache -r /tmp/team9-runtime-requirements.txt
```

For an approved target, set `TEAM9_TESTER_TOKEN` privately and run:

```sh
uv run python -m scripts.run_suite --tester-url http://127.0.0.1:8001 --target-url http://127.0.0.1:8000 --target-revision unknown --output report.json --record run-record.json
```

## Ownership and deployment

| Owner | Implementation | Continuation |
|---|---|---|
| Jackson | API/auth/storage/jobs/services/assembly/deploy | [jackson_phase3.md](docs/implementation/jackson_phase3.md) |
| Dylan | workbooks/audit | [dylan_phase3.md](docs/implementation/dylan_phase3.md) |
| Ryan | test interface/measurement/UI | [ryan_phase3.md](docs/implementation/ryan_phase3.md) |
| Dixon | tester/contract/acceptance/CI/fixtures | [dixon_phase3.md](docs/implementation/dixon_phase3.md) |

Owner documents retain their historical verification context. The integration record supersedes
statements that other modules are absent. Shared serialization remains the Team 9 1.0.0 profile;
Team 12 confirmation items remain listed in the contract.

Development Compose: `docker compose -f deploy/compose.yaml up --build` with private `.env.local`.
Configure the tester target/access map for `http://system:8000`; container loopback refers to
itself. Use `TEAM9_BUILD_TARGET=measurement` when enabling coverage. Named volumes preserve
system/tester state independently. The existing production deployment uses EC2, persistent
storage, private S3 and HTTPS. See the deployment runbook for operation and the original
provisioning templates, and [automatic deployment](deploy/AUTODEPLOY.md) for CI-gated updates
of the existing host. Additional provisioning requires separate operator authorization.
