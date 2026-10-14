# 30861_Team9

ADA Excel Hub — Phase 3 foundation and independent HTTP tester.

This branch delivers the shared foundation and Dixon's independent tester: authenticated
setup/run/report endpoints, persistent single-use sessions, an independent fixture suite,
coverage and operation-status records, and CI. The system business/test endpoints, engine,
storage, worker and UI are still pending integration from their owners. Local full-suite
evidence uses an explicitly labeled test-only fake target, not the Team 9 system.
See [Dixon implementation](docs/implementation/dixon_phase3.md) and the
[handoff verification](docs/evidence/dixon_handoff_verification.md).

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

Visit `http://127.0.0.1:8000/docs` and `http://127.0.0.1:8001/docs`. The system has empty
OpenAPI paths and logs its pending integrations; the tester documents its three instructor
endpoints. Configure `TESTER_OPERATOR_TOKEN_HASHES_JSON`, `ALLOWED_TARGET_URLS` and
`TARGET_AUTH_MAP_JSON` privately before using the tester. Use one tester API worker per SQLite
store so startup recovery applies only to a stopped process. There is no worker command yet; Jackson will provide it with
the durable worker implementation. Stop each server with Ctrl+C.

Optional local credential scaffolding for later feature integrations:

```sh
uv run python -m scripts.create_dev_tokens --local-only
```

This exclusively creates ignored `.env.local`, with POSIX mode 0600; never overwrites an existing
file, never prints tokens, refuses `APP_ENV=production`, and configures only loopback access.
To use it locally, pass `_env_file=".env.local"` when constructing settings, or copy the needed
entries privately to `.env`. On Windows, restrict the file ACL to your user. These credentials can enable tester operator authentication; the system
project/test endpoints are still pending. Test-interface/measurement flags stay
false unless explicitly set for an approved test deployment. Never deploy this local file.

coverage.py is a dev dependency and an optional `measurement` runtime extra. An enabled future
deployment installs `uv sync --locked --no-dev --extra measurement`; the foundation does not
collect workbook-execution measurements.

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
Their schemas describe the Team 9 profile; tester paths reflect delivered endpoints and system
paths remain empty until integration.
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
Scanning needs network access; report failures honestly. CI is defined in
[ci.yml](.github/workflows/ci.yml). Historical evidence reports branch protection unconfigured;
no protection settings were changed here and current remote state was not checked.

## Tester suite and run evidence

```sh
uv run python -m scripts.make_synthetic_fixtures --check
uv run python -m scripts.tester_selfcheck --evidence-dir docs/evidence/dixon_local_selfcheck
```

The self-check starts only loopback tester/fake-target processes, uses temporary private state,
and retains sanitized reports when requested. It refuses occupied ports. The suite has 51
planned cases: on a healthy fake, 50 pass and the controlled-failure observation skips.
Source preservation is checked through the target's version hash contract; the tester cannot
independently inspect target storage. Target code coverage comes only from `/test/report`.

For an approved configured target, supply the tester operator token privately in
`TEAM9_TESTER_TOKEN`, then run:

```sh
uv run python -m scripts.run_suite --tester-url http://127.0.0.1:8001 --target-url http://127.0.0.1:8000 --target-revision unknown --output report.json --record run-record.json
```

The CLI never repeats setup/run POSTs after a lost response. It polls the stored report after
an uncertain run and exits nonzero for failures/errors after writing evidence. Future/fault
observation skips do not by themselves fail CI. Set `TEAM9_TESTER_URL`, `TEAM9_TARGET_URL` and
`TEAM9_TESTER_TOKEN` to opt into live acceptance tests. Public URLs must use HTTPS.

The built wheel includes the manifest, expectations and synthetic fixtures; a separately
installed tester can use those defaults or an operator-provided `FIXTURE_MANIFEST_PATH`.
Never deploy `tests/support/` as the real system.

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
