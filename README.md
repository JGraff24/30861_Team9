# 30861_Team9

ADA Excel Hub — Phase 3 repository foundation.

This branch delivers Python packages, shared contracts, typed configuration, injection ports,
two minimal FastAPI factories and foundation checks. Ryan's injectable testing routes,
persistent-session orchestration, measured runner and upload/results UI are now available.
The foundation factories remain unassembled: business endpoints, authentication, engine,
persistence and worker require teammate integration.
Framework docs are runnable. No placeholder route claims successful business functionality.

## Local setup

Use Python 3.12 and uv 0.13.1. `pyproject.toml` pins resolver-selected direct dependencies;
`uv.lock` pins transitive versions and artifact hashes. Do not maintain a second pip/Poetry workflow.
If uv is already installed at the required version, skip its installation below. pip is used here
only to install the uv tool, never to resolve application dependencies.

macOS/Linux (requires an installed Python with venv support):

```sh
python3 -m venv .uv-tool
.uv-tool/bin/python -m pip install uv==0.13.1
export PATH="$PWD/.uv-tool/bin:$PATH"
uv python install 3.12
uv sync --locked --group dev
cp -n .env.example .env
```

Windows PowerShell:

```powershell
py -m venv .uv-tool
.\.uv-tool\Scripts\python.exe -m pip install uv==0.13.1
$env:Path = "$PWD\.uv-tool\Scripts;$env:Path"
uv python install 3.12
uv sync --locked --group dev
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Both settings classes read `.env`. Environment variables take precedence. Empty credential maps
authorize no users; no AWS credentials are required for the foundation. Do not source a private
environment into an untrusted shell or log tokens. See [.env.example](.env.example) for the entire
settings catalog and [contract profile](docs/contracts/phase3.md) for typed JSON map shapes.

Run in two terminals from the repository root:

```sh
uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
uv run uvicorn tester.main:create_app --factory --host 127.0.0.1 --port 8001
```

Visit `http://127.0.0.1:8000/docs` and `http://127.0.0.1:8001/docs`. Both have empty OpenAPI paths.
Feature URLs return framework 404 because they have no handlers. The startup log explicitly
identifies pending integrations. There is no worker command yet; Jackson will provide it with
the durable worker implementation. Stop each server with Ctrl+C.

Optional local credential scaffolding for later feature integrations:

```sh
uv run python -m scripts.create_dev_tokens --local-only
```

This exclusively creates ignored `.env.local`, with POSIX mode 0600; never overwrites an existing
file, never prints tokens, refuses `APP_ENV=production`, and configures only loopback access.
To use it locally, pass `_env_file=".env.local"` when constructing settings, or copy the needed
entries privately to `.env`. On Windows, restrict the file ACL to your user. These credentials
do not enable pending authentication or test endpoints. Test-interface/measurement flags stay
false unless explicitly set for an approved test deployment. Never deploy this local file.

coverage.py is a dev dependency and an optional `measurement` runtime extra. An enabled future
deployment installs `uv sync --locked --no-dev --extra measurement`; the foundation does not
collect workbook-execution measurements.

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
uv run ruff check .
uv run mypy app tester
uv run pytest tests/unit tests/contract
uv run python -m scripts.export_schemas --check
uv run python -m scripts.check_identity
uv run python -m scripts.smoke_foundation
```

Contract tests validate both OpenAPI catalogs and independent JSON payloads. Generate catalogs
after coordinated DTO changes with `uv run python -m scripts.export_schemas`, then review the diff.
Their schemas describe agreed wire shapes; their empty paths reflect actual foundation delivery.
Cross-field constraints such as covered<=total and chronological audit completion also require
Pydantic validation; JSON Schema cannot express every relational check.

Audit locked runtime dependencies, including the optional measurement extra (POSIX):

```sh
uv export --locked --no-dev --extra measurement --no-emit-project --format requirements-txt --output-file /tmp/team9-runtime-requirements.txt > /dev/null
uv run pip-audit --require-hashes --disable-pip --cache-dir /tmp/team9-audit-cache -r /tmp/team9-runtime-requirements.txt
```

On PowerShell replace the temporary path with `$env:TEMP\team9-runtime-requirements.txt`, the
cache path with `$env:TEMP\team9-audit-cache`, and `> /dev/null` with `| Out-Null`.
To scan dev/build tools too, omit `--no-dev` and add `--group dev` to the export.
Scanning needs network access; report failures honestly. CI and branch protection are not configured.

## Ownership and integration

| Owner | Implementation directories | Continuation document to write |
|---|---|---|
| Jackson | `app/api`, `app/auth`, `app/storage`, `app/jobs`, `app/services`, `deploy`, assembly | `docs/implementation/jackson_phase3.md` |
| Dylan | `app/workbooks`, `app/audit`, owner unit tests | `docs/implementation/dylan_phase3.md` |
| Ryan | `app/test_interface`, `app/measurement`, `app/ui`, templates/static, owner tests | `docs/implementation/ryan_phase3.md` |
| Dixon | `tester`, `tests/contract`, `tests/acceptance`, fixture manifests, CI | `docs/implementation/dixon_phase3.md` |

Jackson coordinates `app/main.py`, `app/config.py`, `app/contracts`, dependency files and README.
Owners use `WorkbookEngine`, `Repository`, `BlobStore`, `EngineRunner`, `SessionService`,
`CodeMeasurementStore`, `AuthService` and `SystemServices` from [ports.py](app/contracts/ports.py).
Feature routers will export `create_router(services)`. Test fakes belong only inside tests.

Review and merge the foundation before all four owners branch from its merged commit. Develop
independently; final integration needs Dylan's engine and Ryan's runner/session services, Jackson's
storage/worker/assembly, then Dixon's live HTTP suite and UI checks. Owner unit success does not
prove whole-system readiness. See [bootstrap implementation](docs/implementation/bootstrap_phase3.md)
for actual models, outcomes, limitations and exact sharing steps. Each owner must write their
own implementation and evidence documents when delivering their features.

AWS is the selected target, with EC2/SQLite/S3 as Team 9 defaults requiring pod confirmation.
No deployment resources, public URLs or cross-team results are delivered by this bootstrap.
