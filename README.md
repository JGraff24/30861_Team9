# 30861_Team9

Excel accessibility hub, Phase 3 service implementation on `feature/phase3-service`.

The four Phase 3 modules are being integrated: workbook validation and audits, measured
execution and testing sessions, durable services and storage, and the independent tester.
See docs/evidence/phase3_integration.md for the final review and verification results.


See [actual implementation](docs/implementation/jackson_phase3.md),
[test evidence and all 14 operation statuses](docs/evidence/jackson_phase3.md), and
[deployment runbook](deploy/README.md). The [shared contract](docs/contracts/phase3.md)
remains the Team 9 default requiring Team 12 confirmation.

## Local setup and processes

Use Python 3.12 and uv 0.13.1. Direct versions and artifact hashes are already pinned in
`pyproject.toml`/`uv.lock`. To install the uv tool if missing:

```sh
python3 -m venv .uv-tool
.uv-tool/bin/python -m pip install uv==0.13.1
export PATH="$PWD/.uv-tool/bin:$PATH"
uv python install 3.12
uv sync --locked --group dev
```

On Windows use `.uv-tool\Scripts\python.exe` and add `.uv-tool\Scripts` to PATH.
The service execution guard currently targets POSIX; use Linux/WSL for API+worker execution.
Both settings classes read `.env`; environment variables take precedence. No credential is
valid by default. For private loopback development credentials:

```sh
uv run python -m scripts.create_dev_tokens --local-only
```

This creates ignored `.env.local` with mode 0600, refuses production and overwrites, and prints
no tokens. If `.env` does not exist, copy the generated file to `.env` privately (preserve 0600).
Do not put credentials in commands, Git, images or evidence. Add real integration factory paths
as documented below. The generator's flags remain disabled until explicitly enabled.

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
The worker exits with an explicit configuration error until engine and runner are installed.
The tester currently exposes framework docs only. Stop processes with Ctrl+C. State under
`state/` survives restart; do not delete it to restart the service.

## Integrating teammate modules

`app/contracts/ports.py` remains the common API. Inject dependencies into `create_app` or
configure server-owned `module:factory` paths; paths are never accepted from HTTP callers:

| Environment input | Factory signature |
|---|---|
| `ENGINE_FACTORY` | `factory() -> WorkbookEngine` |
| `RUNNER_FACTORY` | `factory(*, engine, enable_code_measurement) -> EngineRunner` |
| `SESSION_SERVICE_FACTORY` | `factory(*, repository, blob_store, settings) -> SessionService` |
| `TEST_ROUTER_FACTORY` | `create_router(services) -> APIRouter`, attached only when testing enabled |
| `UI_ROUTER_FACTORY` | `create_router(services) -> APIRouter` |

These are integration hooks, not claims that those factories already exist. Confirm/adapt the
small factory signatures when Dylan/Ryan merge. The runner and engine must serialize as trusted
Python configuration without live sockets/DB connections/secrets. The parent guard gives the
runner only bounded source bytes, shared DTOs, revision/namespace/limits and a minimal child
environment. Ryan still owns measurement and his execution runner implementation.

The four paths in [system OpenAPI](docs/contracts/system.openapi.json) are actual service paths:
public health; authenticated POST `/api/v1/workbooks`; GET
`/api/v1/workbooks/{workbookId}/versions/{versionId}` and its `/audits/{auditId}`.
Project failures have the shared error envelope and generated `X-Request-ID`.
Reviewer grants are explicit; test-admin credentials cannot access ordinary project operations.
Version GET rereads/hashes source bytes. There are no remediation, history, ordinary deletion
or downloadable report endpoints. No finding means only that the implemented checks found none.

## Ryan's delivered modules

Register `app.test_interface.routes:create_router` and `app.ui.routes:create_router` through
Jackson's assembly. Construct `app.test_interface.service:PersistentSessionService` with
repository/blob/settings and `app.measurement.runner:MeasuredEngineRunner` with the trusted
engine and measurement flag. No shared assembly/config/dependency files were changed here.
See [Ryan's implementation](docs/implementation/ryan_phase3.md),
[design decisions](docs/implementation/ryan_design_decisions.md) and
[verification evidence](docs/evidence/ryan_phase3.md) for exact factory configuration,
current teammate PR compatibility, scope and pending human/deployment evidence.
Owner regression command: `uv run pytest tests/ryan tests/unit tests/contract`.
The optional Chromium script is `tests/ryan/browser_ui.py`; Playwright is test tooling,
not an application dependency.

## Verification

```sh
uv sync --locked --group dev
uv lock --check
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy app tester scripts
uv run --locked pytest -q
uv run --locked python -m scripts.export_schemas --check
uv run --locked python -m scripts.check_identity
uv run --locked python -m scripts.smoke_foundation
```

The live-process test and smoke need permission to bind loopback sockets. A Linux-only test
checks actual `RLIMIT_AS` enforcement and skips on macOS; it must pass on the production image
before deployment. Runtime dependency scanning needs network access:

```sh
uv export --locked --no-dev --extra measurement --no-emit-project --format requirements-txt --output-file /tmp/team9-runtime-requirements.txt > /dev/null
uv run --locked pip-audit --require-hashes --disable-pip --cache-dir /tmp/team9-audit-cache -r /tmp/team9-runtime-requirements.txt
```

Generate schemas after compatible DTO/route changes with `uv run python -m scripts.export_schemas`.
The system catalog combines actual routes with shared serialization schemas; tester paths stay
empty until Dixon delivers them. Unit tests inject narrow doubles and do not prove real workbook
rules, runtime coverage or deployed acceptance. No new dependencies were needed in this branch.

## Ownership and deployment

| Owner | Implementation | Continuation |
|---|---|---|
| Jackson | API/auth/storage/jobs/services/assembly/deploy | [jackson_phase3.md](docs/implementation/jackson_phase3.md) |
| Dylan | workbooks/audit | `docs/implementation/dylan_phase3.md` pending |
| Ryan | test interface/measurement/UI | `docs/implementation/ryan_phase3.md` pending |
| Dixon | tester/contract/acceptance/CI/fixtures | `docs/implementation/dixon_phase3.md` pending |

Bootstrap is merged at `ffa6947`; its [implementation record](docs/implementation/bootstrap_phase3.md)
is historical. Jackson coordinates shared contracts/settings/dependencies. Teammate implementation
packages and tester were preserved. Existing bootstrap assertions were updated only where the
foundation's empty-route assumptions changed.

Development Compose: `docker compose -f deploy/compose.yaml up --build` (private `.env.local`
required). Named volumes retain system/tester state independently. Containers need their own
approved target URL mapping, for example the explicitly configured Docker system base URL;
loopback inside tester refers to tester itself. Production uses the standalone production Compose
file, EC2, EBS metadata and private S3; it exposes only HTTPS. See the runbook for inputs,
preflight gates, TLS, IAM, releases, backups, scoped resets and rollback. Cloud provisioning,
Git push/merge and external messaging have not been performed.
