# Jackson Phase 3 evidence

Date: 2026-10-16 (America/Indianapolis). Target: local macOS ARM64 / Python 3.12.15,
plus ASGI and isolated subprocesses. Branch: `feature/phase3-service`. Reviewed baseline HEAD:
`ffa6947b6850a5df624e5f48837e029fab8c7272` (merged bootstrap). Verification was performed on working-tree
code before the PR commit; this base SHA is **not** a release identifier for the new implementation.
Use the PR head commit to identify the reviewed source. No tag, public
URL, deployment digest or CI run has been created. No cross-team target was contacted.

## Verification record

The bootstrap's pinned uv 0.13.1 was at `/private/tmp/team9-bootstrap-tools/bin/uv`;
`UV_CACHE_DIR=/private/tmp/team9-uv-cache` and
`UV_PYTHON_INSTALL_DIR=/private/tmp/team9-uv-python` were used where applicable.
Dependencies were reused from the existing lock, with no pyproject/uv.lock changes.

| Executed command | Actual outcome |
|---|---|
| `uv sync --locked --group dev` | Passed with authorized package-index access; editable project rebuilt/installed, 84 locked packages resolved |
| `uv lock --check --offline` | Passed, lock current |
| `uv run --locked pytest -q` | **195 passed, 1 skipped**, one existing Starlette/HTTPX TestClient deprecation warning; final full run 9.50s |
| `ruff check .` | Passed after formatting correction |
| `ruff format --check .` | Passed, 48 Python files |
| `mypy app tester scripts` | Passed, 37 source files, strict project settings |
| `uv run --locked python -m scripts.export_schemas --check` | Passed for system/tester catalogs; actual routes and required schemas validated by contract tests |
| `uv run --locked python -m scripts.check_identity` | Golden digest and two Unicode vectors passed; unchanged bootstrap standard-library checks |
| `uv run --locked python -m scripts.smoke_foundation` | Separate live ports 8000/8001: docs 200, system four paths and truthful health 503/auth 401; tester zero paths; missing test/report 404; owned processes stopped |
| `uv export --locked --no-dev --extra measurement --no-emit-project ...` followed by `uv run --locked pip-audit --require-hashes --disable-pip ...` | Passed: **No known vulnerabilities found**, including locked optional coverage runtime extra |
| `uv build --no-build-isolation` | Source distribution and wheel built successfully; wheel contains owner modules, excludes tests and environment files |
| PyYAML load of `deploy/aws.yaml`, development/production Compose | Parsed successfully with CloudFormation intrinsic-tag loader; syntax-only, not CloudFormation/IAM/Compose validation |
| `tests/unit/test_jackson_deploy.py` | Renderer generated valid policy JSON and preserved nginx runtime variables; refused overwrite and invalid hostname; no AWS calls |
| `git diff --check` | Passed |

Initial attempts recorded honestly: sandbox DNS blocked the local package rebuild; offline
non-isolated editable installation lacked the build backend's dynamic `editables` dependency.
A normal authorized locked sync succeeded without changing dependencies. Sandbox loopback binding
blocked the live HTTP test; rerunning with local socket permission passed. Neither environmental
failure was hidden by skipping its test. The Linux RLIMIT_AS test is the only deliberate skip
on macOS; **production memory enforcement remains unverified here**.

Final recheck includes preserved OpenAPI security schemes, IAM role enforcement and explicit dependency/storage failure paths; see below.
No test result in this document is a deployed workbook acceptance result.

## Owner test map and independent assertions

| Test file | Behavior demonstrated / acceptance trace |
|---|---|
| `tests/unit/test_jackson_api.py` | Exact four-route wire responses, 202/PROCESSING and terminal schemas, file/filename/multipart/byte-limit rejection invariants, authorization before blob access, reviewer grants and test-admin restrictions, unknown/mismatched IDs, namespace isolation, real source hash tampering before/after processing, retained failure source, completed history unchanged, compensated metadata failure, health 503, sanitized internal faults, actual validation/worker timeout and queue expiry; AC-01/02/03/14/15 delivered portions |
| `tests/unit/test_jackson_storage.py` | Schema/WAL/FKs/busy timeout, all-or-nothing upload and terminal writes using SQL-trigger faults, competing transactional claims, worker lease/terminal guards, stale queued/running recovery, duplicate-session race, scoped cleanup and tombstones, measurement set unions/revision/missing data, immutable filesystem keys and S3 SDK-stub encryption/checksum/precondition behavior; AC-03/14/15 |
| `tests/unit/test_jackson_execution.py` | Fresh subprocess excludes representative auth/AWS secrets, real parent kill of stalled execution, Linux-only allocation beyond OS cap test; AC-14 bounded execution portion |
| `tests/unit/test_jackson_processes.py` | Live HTTP upload with **test-only runner**, API process restart, source/metadata survive, separate worker CLI process completes, retrieval verifies hash/status; owner process persistence evidence |
| `tests/unit/test_jackson_deploy.py` | Local review-file rendering and error handling; no deployed AC-16 claim |
| Existing foundation/contract tests | 141 preserved bootstrap cases with narrow updated route/lifespan assertions, independent golden identity vectors, strict schemas and verbatim shared-profile checksum |

Independent oracles: hashes are computed with standard-library hashlib from known test bytes;
HTTP expected fields/statuses are authored from the shared contract; SQL faults happen after
earlier statements to prove rollback; concurrent claim/session tests observe one winner; timeout
expects actual elapsed wall-clock bounded despite a sleeping child; source integrity mutates real
stored bytes. S3 Stubber validates actual SDK request parameters without contacting AWS. No
expected workbook findings are derived from a real audit implementation. Test-only findings
merely exercise storage order/terminal status. Exact rule/fixture accuracy belongs to Dylan and
Dixon's independent expectations. Owner tests are not a replacement for those modules.

Actual runtime code coverage: unavailable because Ryan's measurement implementation is absent.
No whole-system code percentage, tester component/system percentage, rule precision or cross-team
pass rate is claimed. Repository aggregation tests only prove correct set union/null behavior.
The six-component/eight-AC catalogs remain shared Team 9 defaults; denominator agreement and
qualifying deployed assertions are pending Team 12/Dixon.

## All 14 business operations

“Service implemented” means proven local HTTP/service behavior with injected missing ports;
it does not imply real workbook rules or AWS readiness. Unspecified later operation labels are
not invented from the absent Phase 2 PDF; their numbers/statuses are preserved.

| Operation | Status | Success/error conformance and limits |
|---|---|---|
| 1 Health | Service implemented | 200 exact schema with ready injected ports, 503 ProjectError with missing/failing dependencies; public |
| 2 Initial upload | Service implemented | 202 after committed source/metadata/job; tested 400/401/403/413/415/422/500/503 paths; actual content checks depend on Dylan/Ryan |
| 3 | Unimplemented | Future scope; no success handler advertised |
| 4 Version retrieval | Service implemented | 200 exact metadata, hash from reread source; 400/401/403/404/500/503 controlled paths; mismatched IDs rejected |
| 5 Audit retrieval | Service implemented | 200 processing/complete/review/failed shape, no successful partial findings; auth/nested IDs/storage faults controlled; real rules pending |
| 6 | Unimplemented | Future scope |
| 7 | Unimplemented | Future scope |
| 8 | Unimplemented | Future scope |
| 9 | Unimplemented | Future scope |
| 10 | Unimplemented | Future scope |
| 11 | Unimplemented | Future scope |
| 12 | Unimplemented | Future scope |
| 13 | Unimplemented | Future scope |
| 14 | Unimplemented | Future scope; test-namespace cleanup does not complete ordinary deletion |

System `/test/setup` and `/test/report`: unimplemented (Ryan). Tester `/test/setup`, `/test/run`,
`/test/report`: unimplemented (Dixon). UI: unimplemented (Ryan). Real factories are attachable;
placeholder success handlers were not added. Scheduling/remediation/history/reports remain later.

## Assignment Q1–Q9 evidence index

This index organizes the handoff's listed assignment themes. The original Phase 3 questionnaire
and Phase 2 PDF were not supplied in this checkout; verify exact question wording before submission.

| Question/theme | Contribution and honest remaining evidence |
|---|---|
| Q1 scope/milestones | Owner module delivered; bootstrap preserved; engine/runner/tester integration milestone pending owners |
| Q2 hosting/release/configuration | Local startup/restart tested, three commands, deployment templates/runbook; Jackson must supply actual AWS account/region/domain/cert/budget and release SHA/tag/image mapping |
| Q3 operations | Complete 1–14 table above; no future capability represented as delivered |
| Q4 tests/regression/coverage | Tests/static/schema/dependency scan results above, owner-to-AC trace; Dixon supplies fixture catalog, real suite/CI links and independent oracle reviews |
| Q5 deployed results | None; Jackson/Dixon must execute AWS and retain sanitized real reports after authorization/integration |
| Q6 models/consistency | Actual component/data/job/session models in implementation doc; planned AWS model explicitly labeled; real OpenAPI security-scheme correction described below |
| Q7 cross-team agreement/testing | Both directions pending Team 12 URLs/access/fixture delivery and serialization/session/catalog/default confirmation; no pod review meeting or instructor approval fabricated |
| Q8 limitations/risk revisions | Severity/owner/workaround table, storage compensation window, Linux/Docker gaps, constructor serialization constraints, backups/rollback in implementation/runbook |
| Q9 GenAI work/verification | This Codex session implemented Jackson-owned code and tests; no subagents or external messages; verification and actual correction below |

AI assistance/reflection: Codex read the entire supplied handoff, inspected the merged bootstrap,
implemented owner code and tests, and generated configuration/docs. The user authorized sequential
autonomous stages, not teammate implementations or cloud provisioning. Tools used were local shell,
uv/Python/Ruff/mypy/pytest, SDK stubs, and official AWS documentation browsing for preparation.
No skill/plugin performed external writes, no agent delegation occurred, and no review/approval
meeting is claimed. Verification had to distinguish service doubles from real workbook behavior;
passing owner tests cannot establish whole-system readiness.

One real correction: final review found that bootstrap schema export replaced the entire
`components` dictionary, dropping the runtime BearerAuth security scheme when adding DTO schemas.
Changed export to merge schemas into existing components and added an assertion that the actual
upload security requirement resolves to the bearer scheme. This was caught by reviewing actual
generated artifacts, not by assuming a passing OpenAPI validator proved authentication documentation.
A separate review added queue expiration to authorized polling so a missing worker cannot leave
accepted jobs indefinitely PROCESSING while the API remains available.

## Pending external evidence and operator inputs

Jackson: real AWS account/region/VPC/subnet/AMI/instance and volume size, budget and authorized
recipient, bucket/DNS/trusted certificate+renewal, private deployment credentials, registry/image
access, release SHA/tag/digest mapping, operator SSM/IAM review, validated backup/restore and
Linux/Docker/CloudFormation preflight. Full instructions: `deploy/README.md`.
Dylan/Ryan: real engine/runner/measurements/session/UI modules, compatible factory registration,
all content/rule checks and platform verification. Dixon: actual shared fixture manifests, live
full-system tester reports/CI and both cross-team runs. Team 12: wire/default/catalog/URL/access
confirmation. Ryan/pod: five deidentified workbooks, two human expected-result reviews, Excel
checker/preservation experiments, five repeated real audits, precision/performance evidence.
No missing human evidence was substituted with generated claims.

## Final verification note

Final full `uv run --locked pytest -q`: **195 passed, 1 skipped, 1 warning in 9.50s**.
This comprises the 141 preserved foundation cases plus 54 passing owner cases and one Linux-only
owner memory-limit skip. `ruff check .`, `ruff format --check .` (48 files), strict
`mypy app tester scripts` (37 source files), schema `--check`, and final wheel/sdist build passed.
Final source includes the IAM-role-only S3 default-client check and a stable snapshot of queued
rows during stale recovery. A new SDK-mock test verifies credential selection without contacting
AWS. The shared specification's preserved SHA256 is
`f999185e49e1d109129e5b65db173e48fbc3dec1e4b8e62300ef015c56545468`.

Changed-file groups: owner API/auth/storage/jobs/services implementations; shared main/settings
and one optional internal model field; actual system schema and contract delivery preamble;
local startup/schema/smoke/deployment-render scripts; Docker/Compose/AWS/IAM/HTTPS artifacts;
owner unit/process tests and narrow existing bootstrap assertions; README/environment example;
implementation/evidence documents. No teammate feature package or tester implementation changed.
Verification finished before publication. The user subsequently authorized opening a PR,
including committing and pushing this branch. No merge or cloud provisioning is authorized.
The reviewable continuation record and next Codex prompt are complete.
