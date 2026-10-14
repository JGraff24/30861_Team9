# Bootstrap evidence — 2026-10-14

Scope: shared repository foundation only. Baseline `88ae6165973021523d8448787b90c9ac92049f1d`;
branch `bootstrap/phase3`; no foundation commit, PR, remote publication or deployment was created.
Actual models, module map and handoff steps: [bootstrap implementation](../implementation/bootstrap_phase3.md).

| Evidence area | Actual record / remaining owner |
|---|---|
| Delivered scope/milestones | DTOs, settings, ports, docs-only app factories and local checks delivered; four feature handoffs pending |
| Release/hosting/startup | Local CPython 3.12.15/macOS ARM64, uv 0.13.1; separate Uvicorn ports 8000/8001 checked; no AWS URL/tag/release |
| Operation status | All 14 business operations unimplemented; 1/2/4/5 have DTOs only; all instructor routes unimplemented |
| Test/AC mapping | 141 foundation cases pass; schema/startup evidence supports later contracts but claims no completed workbook AC or acceptance suite |
| Regression/static checks | Ruff passes; strict mypy passes across 20 app/tester files; six contract tests validate schemas/payloads/vectors/profile copy |
| Dependencies/build | Runtime/measurement scan: 33 packages, no known vulnerabilities; complete dev/build/runtime scan: 83 packages, no known vulnerabilities; wheel/sdist built |
| Models/consistency | Delivered graph contains actual factory/settings/contract/check modules; separate target graph labels feature owners as pending |
| Deployed/cross-team runs | Not performed; Jackson supplies actual deployment/access; Dixon runs both cross-testing directions when integrated |
| Limitations/risk revisions | Pod confirmation, missing fixtures/reviews, CI/deployment, memory enforcement and real integration pending; one low-severity TestClient deprecation recorded |
| Human evidence | No fabricated pod approval, two-person review, Excel checker results, precision figure, meeting, CI link or coverage percentage |

Foundation verification commands (run from repository root after README setup):

```sh
uv sync --locked --group dev
uv lock --check
uv run ruff check .
uv run mypy app tester
uv run pytest tests/unit tests/contract
uv run python -m scripts.export_schemas --check
uv run python -m scripts.check_identity
uv run python -m scripts.smoke_foundation
uv build --no-build-isolation
git diff --check
```

Actual final pytest result: 141 passed, one StarletteDeprecationWarning for HTTPX TestClient.
Live processes returned docs 200, empty OpenAPI paths and pending routes 404; both were stopped.
Tests also prove injected ports are not invoked and default startup writes no state or contacts
network. A passing factory check does not prove authentication, workbook validation or storage.

Dependency scan scope is exported from uv.lock with exact versions and artifact hashes, excluding
the local editable project. Commands were:

```sh
uv export --locked --no-dev --extra measurement --no-emit-project --format requirements-txt --output-file /tmp/team9-runtime-requirements.txt > /dev/null
uv run pip-audit --require-hashes --no-deps --disable-pip -r /tmp/team9-runtime-requirements.txt
uv export --locked --group dev --extra measurement --no-emit-project --format requirements-txt --output-file /tmp/team9-all-requirements.txt > /dev/null
uv run pip-audit --require-hashes --disable-pip --cache-dir /tmp/team9-audit-cache -r /tmp/team9-all-requirements.txt
```

In this macOS session the temporary paths used `/private/tmp` (the actual temporary storage).
The first scanner command warns that --no-deps is redundant with --require-hashes; it completed
successfully. README uses the cleaner equivalent without --no-deps. No audit ignores or automatic
dependency upgrades were applied. The scanner's claims concern known published vulnerabilities
at execution time, not proof of future or feature-level security.

The full attached shared specification and appendix are preserved after the bootstrap clarification
prefix in `docs/contracts/phase3.md`. Normalized UTF-8/LF shared-body SHA256:
`f999185e49e1d109129e5b65db173e48fbc3dec1e4b8e62300ef015c56545468`.
The instructor Google document was read completely via its text export; web viewer access failed
but terminal retrieval succeeded. Its current prose/starter required no contract-field correction.

GenAI work: one Codex agent read the attachment and repository, implemented the foundation,
retried instructor-source access, resolved dependencies and ran all checks. No subagents were used.
A real correction occurred: the first validation run found an invalid Pydantic serializer type
import; conditional field exclusion replaced it and the full suite then passed. A final source
comparison also found Ruff had formatted copied Markdown code snippets; the original copy was
restored and protected with a content-hash assertion plus formatter exclusion.

Engineering reflection: shared boundaries and explicit missing integrations let each owner work
without pretending the service is ready. Independent payload and digest checks caught issues that
folder creation alone could not establish. Remaining acceptance evidence must come from real
workbooks, human review, integrated processes and deployed targets; this foundation does not
substitute passing schema tests for those outcomes. The four owners write their own Q1–Q9 evidence
when their features exist, and Jackson consolidates actual operation status at integration.

## Actual changed files

M means modified; ?? means new and untracked. Runtime environments, private test files, caches
and build output are ignored and absent from this list. No files have been staged or committed.

```text
 M README.md
?? .env.example
?? .gitignore
?? .python-version
?? app/__init__.py
?? app/api/__init__.py
?? app/audit/__init__.py
?? app/auth/__init__.py
?? app/config.py
?? app/contracts/__init__.py
?? app/contracts/errors.py
?? app/contracts/models.py
?? app/contracts/ports.py
?? app/jobs/__init__.py
?? app/main.py
?? app/measurement/__init__.py
?? app/services/__init__.py
?? app/storage/__init__.py
?? app/test_interface/__init__.py
?? app/ui/__init__.py
?? app/ui/static/.gitkeep
?? app/ui/templates/.gitkeep
?? app/workbooks/__init__.py
?? deploy/README.md
?? docs/contracts/phase3.md
?? docs/contracts/system.openapi.json
?? docs/contracts/tester.openapi.json
?? docs/evidence/bootstrap_phase3.md
?? docs/implementation/bootstrap_phase3.md
?? docs/implementation/bootstrap_phase3_pr.md
?? pyproject.toml
?? scripts/__init__.py
?? scripts/check_identity.py
?? scripts/create_dev_tokens.py
?? scripts/export_schemas.py
?? scripts/smoke_foundation.py
?? tester/__init__.py
?? tester/config.py
?? tester/main.py
?? tests/acceptance/.gitkeep
?? tests/contract/test_schemas.py
?? tests/fixtures/README.md
?? tests/unit/test_foundation.py
?? tests/unit/test_models.py
?? uv.lock
```
