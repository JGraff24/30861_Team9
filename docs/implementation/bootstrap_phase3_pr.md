Adds the shared foundation that four Phase 3 owners can branch from: Python 3.12/uv dependencies,
validated DTOs and Protocols, typed settings, injection containers, local setup helpers and two
docs-only ASGI factories. Business and instructor feature endpoints remain pending. The preserved
contract profile and bootstrap implementation record explain integration boundaries and limitations.

Validation: 141 foundation tests passed; Ruff and strict mypy passed; OpenAPI/JSON Schema and hash
vectors passed; both real local processes started with docs 200 and pending routes 404; source/wheel
build passed; audits of 33 runtime/measurement and all 83 dev/build/runtime packages found no known
vulnerabilities. One documented
Starlette/HTTPX TestClient deprecation remains. No AWS resources or deployment are included.
