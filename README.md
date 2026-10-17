# 30861_Team9

Excel accessibility hub — integrated Phase 3.

The system accepts authenticated `.xlsx` uploads, retains immutable sources, runs durable
asynchronous audits for missing alternative text and generic worksheet names, and exposes
version/audit results through HTTP and a small upload/results UI. The independent tester
provides setup, single-use runs and retained reports through the instructor interface.
No findings means only that these two checks found no issues; it is not a compliance decision.

See the [integration review and evidence](docs/evidence/phase3_integration.md),
[shared contract](docs/contracts/phase3.md) and [deployment runbook](deploy/README.md).
AWS deployment, public HTTPS and Team 12 cross-testing remain pending operator inputs.
Remediation, history, downloadable reports, scheduling and ordinary deletion are future work.

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
token is stored in the private file. Do not put credentials in commands, Git, images or evidence.
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

The four project operations are public GET `/api/v1/health`, authenticated POST
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
credentials. It tests the real engine/API/worker, restart persistence, the 53-case tester suite,
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
system/tester state independently. Production templates prepare EC2/EBS/private S3 and HTTPS;
operator input and separate deployment authorization are required. See the deployment runbook
for IAM, TLS, release mapping, backups, resets and rollback.
