"""Live tester responses validated against the committed tester.openapi.json schemas."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from scripts.export_schemas import CONTRACT_DIR
from tester.config import TesterSettings as SettingsForTester
from tester.main import TesterServices as ServicesForTester
from tester.main import create_app
from tester.suite import checks
from tests.support.fake_system import BASE_URL, FakeSystem, access_map, address_resolver

OPERATOR = "contract-operator-token"
AUTH = {"Authorization": f"Bearer {OPERATOR}"}
MANIFEST = Path(__file__).resolve().parents[1] / "fixtures" / "manifest.json"
CATALOG = json.loads((CONTRACT_DIR / "tester.openapi.json").read_text(encoding="utf-8"))


def validator(name: str) -> Draft202012Validator:
    schema = {**CATALOG["components"]["schemas"][name], "components": CATALOG["components"]}
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(checks, "UPLOAD_LIMIT_BYTES", 60_000)
    settings = SettingsForTester(
        tester_db_path=tmp_path / "tester.sqlite3",
        allowed_target_urls=[BASE_URL],
        TARGET_AUTH_MAP_JSON=access_map(),
        TESTER_OPERATOR_TOKEN_HASHES_JSON={hashlib.sha256(OPERATOR.encode()).hexdigest(): "op"},
        fixture_manifest_path=MANIFEST,
    )
    services = ServicesForTester(
        settings=settings,
        transport=FakeSystem(upload_limit=60_000).transport(),
        resolver=address_resolver(),
        sleep=lambda seconds: None,
    )
    with TestClient(create_app(services=services)) as test_client:
        yield test_client


def test_success_bodies_match_catalog(client: TestClient) -> None:
    setup = client.post("/test/setup", json={"target_url": BASE_URL}, headers=AUTH)
    assert setup.status_code == 201
    validator("SetupResponse").validate(setup.json())
    session_id = setup.json()["session_id"]
    run = client.post("/test/run", json={"session_id": session_id}, headers=AUTH)
    assert run.status_code == 200 and run.headers["content-type"] == "application/json"
    validator("SuiteReport").validate(run.json())
    report = client.get("/test/report", params={"session_id": session_id}, headers=AUTH)
    assert report.content == run.content


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "status"),
    [
        ("post", "/test/setup", {"json": {"target_url": BASE_URL}}, 401),
        ("post", "/test/setup", {"json": {"target_url": 1}, "headers": AUTH}, 400),
        ("post", "/test/run", {"json": {"session_id": "missing"}, "headers": AUTH}, 404),
        ("get", "/test/report", {"headers": AUTH}, 400),
    ],
)
def test_error_bodies_match_catalog(
    client: TestClient, method: str, path: str, kwargs: dict[str, Any], status: int
) -> None:
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == status
    validator("TestEndpointError").validate(response.json())
    documented = CATALOG["paths"][path][method]["responses"]
    assert str(status) in documented


def test_catalog_declares_bearer_security_and_no_422() -> None:
    assert CATALOG["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    for operations in CATALOG["paths"].values():
        for operation in operations.values():
            assert operation["security"] == [{"HTTPBearer": []}]
            assert "422" not in operation["responses"]
