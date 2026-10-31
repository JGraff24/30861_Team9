"""Phase 4 HTTP boundary checks, using real auth and isolated SQLite adapters."""

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openapi_spec_validator import validate
from starlette.requests import Request

from app.api.routes import attachment
from app.auth.service import TokenAuthService
from app.config import SystemSettings
from app.contracts.models import Principal, TestSession
from app.contracts.ports import SystemServices
from app.main import create_app
from app.storage.blobs import FilesystemBlobStore
from app.storage.sqlite import SQLiteRepository


@pytest.fixture
def boundary_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = SystemSettings(
        _env_file=None,
        ruleset_version="2.0.0",
        metadata_db_path=tmp_path / "metadata.sqlite3",
        blob_root=tmp_path / "sources",
        SYSTEM_TOKEN_HASHES_JSON={
            hashlib.sha256(token.encode()).hexdigest(): Principal(subject=token, role=role)
            for token, role in (
                ("owner", "user"),
                ("reviewer", "reviewer"),
                ("admin", "test_admin"),
            )
        },
        ui_router_factory=None,
        enable_test_interface=True,
    )
    services = SystemServices(
        settings=settings,
        repository=SQLiteRepository(settings.metadata_db_path),
        blob_store=FilesystemBlobStore(settings.blob_root),
        auth_service=TokenAuthService(settings),
    )
    with TestClient(create_app(services=services)) as client:
        yield client


@pytest.mark.parametrize(
    "suffix", ["", "/source", "/audits/a", "/audits/a/review", "/audits/a/report"]
)
def test_authentication_precedes_invalid_path_and_body(
    boundary_client: TestClient, suffix: str
) -> None:
    path = "/api/v1/workbooks/bad.id/versions/v" + suffix
    response = boundary_client.get(path)
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
    assert response.headers["x-request-id"] == response.json()["requestId"]
    response = boundary_client.get(path, headers={"Authorization": "Bearer admin"})
    assert response.status_code == 403
    response = boundary_client.get(path, headers={"Authorization": "Bearer owner"})
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize("suffix", ["/audits", "/remediations"])
def test_write_resource_authorization_precedes_json_body(
    boundary_client: TestClient, suffix: str
) -> None:
    path = "/api/v1/workbooks/missing/versions/missing" + suffix
    response = boundary_client.post(
        path, content=b"not-json", headers={"Authorization": "Bearer owner"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
    response = boundary_client.post(path, content=b"not-json")
    assert response.status_code == 401


def test_public_health_ignores_session_but_requires_real_engine(
    boundary_client: TestClient,
) -> None:
    response = boundary_client.get("/api/v1/health", headers={"X-Test-Session": "bad.id"})
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
    assert response.headers["x-request-id"]


def test_new_routes_are_present_and_catalog_is_valid(boundary_client: TestClient) -> None:
    document = boundary_client.get("/openapi.json").json()
    validate(document)
    assert document["openapi"] == "3.1.1"
    assert document["x-specification-version"] == "2.1.0"
    business = {path for path in document["paths"] if path.startswith("/api/v1/")}
    assert len(business) == 13
    assert document["paths"]["/api/v1/health"]["get"]["security"] == []
    for path in business - {"/api/v1/health"}:
        for operation in document["paths"][path].values():
            assert operation["security"] == [{"BearerAuth": []}]
            assert operation["responses"]["409"]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/ProjectError"
            }
            assert (
                "X-Request-ID"
                in operation["responses"]["200" if "get" in document["paths"][path] else "202"][
                    "headers"
                ]
            )


def test_download_disposition_preserves_unicode_without_header_injection() -> None:
    response = attachment(b"source", 'Q4 /研究 "notes"\r\n.xlsx', "application/example")
    assert response.body == b"source"
    assert response.headers["content-length"] == "6"
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="')
    assert "filename*=UTF-8''" in disposition
    assert "%E7%A0%94%E7%A9%B6" in disposition
    assert "\r" not in disposition and "\n" not in disposition


def request_with_body(data: bytes, content_type: str = "application/json") -> Request:
    received = False

    async def receive() -> dict[str, object]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": data, "more_body": False}

    return Request({"type": "http", "headers": [(b"content-type", content_type.encode())]}, receive)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [b'{"auditId":"a","auditId":"b"}', b"{", b'"\xff"', b"NaN", b"Infinity", b""],
)
async def test_json_boundary_rejects_ambiguous_or_malformed_bodies(data: bytes) -> None:
    from app.api.routes import read_json
    from app.contracts.errors import ProjectException

    with pytest.raises(ProjectException) as error:
        await read_json(request_with_body(data))
    assert error.value.status_code == 400
    assert error.value.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_json_size_and_absent_reaudit_body_are_distinct_from_null() -> None:
    from app.api.routes import MAX_JSON_REQUEST_BYTES, read_json
    from app.contracts.errors import ProjectException

    assert await read_json(request_with_body(b""), allow_absent=True) == {}
    assert await read_json(request_with_body(b"null"), allow_absent=True) is None
    with pytest.raises(ProjectException) as error:
        await read_json(request_with_body(b" " * (MAX_JSON_REQUEST_BYTES + 1)))
    assert (error.value.status_code, error.value.code) == (413, "REQUEST_TOO_LARGE")


@pytest.mark.asyncio
async def test_multipart_file_size_precedes_overhead_for_single_large_asgi_chunk() -> None:
    from app.api.intake import read_upload
    from app.contracts.errors import ProjectException

    data = (
        b'--phase4\r\nContent-Disposition: form-data; name="file"; filename="source.xlsx"\r\n\r\n'
        + b"x" * 25_065_537
        + b"\r\n--phase4--\r\n"
    )
    with pytest.raises(ProjectException) as error:
        await read_upload(request_with_body(data, "multipart/form-data; boundary=phase4"))
    assert error.value.status_code == 413


def test_session_checks_precede_path_lookup_and_validation(boundary_client: TestClient) -> None:
    services = boundary_client.app.state.services
    repository = services.repository
    assert repository is not None
    for sid, subjects, ready in (
        ("foreign", ["someone-else"], True),
        ("preparing", ["owner"], False),
    ):
        repository.create_test_session(
            TestSession(
                session_id=sid,
                owner_subject="admin",
                allowed_subjects=subjects,
                state="PREPARING",
                created_at=datetime.now(UTC),
                revision="unknown",
                cleanup_error=None,
            )
        )
        if ready:
            repository.mark_session_ready(sid)
    for session, status in (("unknown", 400), ("foreign", 403), ("preparing", 409)):
        response = boundary_client.get(
            "/api/v1/workbooks/bad.id/versions/missing",
            headers={"Authorization": "Bearer owner", "X-Test-Session": session},
        )
        assert response.status_code == status
    response = boundary_client.get(
        "/api/v1/workbooks/bad.id/versions/missing", headers={"X-Test-Session": "foreign"}
    )
    assert response.status_code == 401


def test_setup_catalog_constrains_faults_without_changing_instructor_envelope(
    boundary_client: TestClient,
) -> None:
    from jsonschema import Draft202012Validator

    document = boundary_client.get("/openapi.json").json()
    schema = document["paths"]["/test/setup"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ]
    validator = Draft202012Validator(schema | {"components": document["components"]})
    validator.validate({"session_id": "s", "setup": {"faults": {"audit_failures": 1}}})
    validator.validate({"session_id": "s"})
    for setup in (
        {"faults": {}},
        {"faults": {"audit_failures": True}},
        {"faults": {"audit_failures": 0}},
        {"faults": {"audit_failures": "1"}},
        {"faults": {"audit_failures": 1, "other": 1}},
        {"other": 1},
    ):
        assert list(validator.iter_errors({"session_id": "s", "setup": setup}))


@pytest.fixture
def workflow_client(tmp_path: Path) -> Iterator[TestClient]:
    """Real transport/DB/storage/children, with an explicitly synthetic audit/editor."""
    from app.services.assembly import assemble
    from tests.unit.test_phase4_worker import CorrectionEngineDouble

    settings = SystemSettings(
        _env_file=None,
        ruleset_version="2.0.0",
        metadata_db_path=tmp_path / "metadata.sqlite3",
        blob_root=tmp_path / "sources",
        SYSTEM_TOKEN_HASHES_JSON={
            hashlib.sha256(token.encode()).hexdigest(): Principal(subject=token, role=role)
            for token, role in (
                ("owner", "user"),
                ("other", "user"),
                ("reviewer", "reviewer"),
                ("ungranted", "reviewer"),
                ("admin", "test_admin"),
            )
        },
        REVIEWER_GRANTS_JSON={"reviewer": ["owner"]},
        TEST_SESSION_USERS_JSON={"admin": ["owner", "other", "reviewer", "ungranted"]},
        enable_test_interface=True,
        ui_router_factory=None,
    )
    with TestClient(
        create_app(services=assemble(settings, engine=CorrectionEngineDouble()))
    ) as client:
        yield client


def workflow_auth(token: str = "owner", session: str | None = None) -> dict[str, str]:
    result = {"Authorization": f"Bearer {token}"}
    if session is not None:
        result["X-Test-Session"] = session
    return result


def workflow_upload(
    client: TestClient, session: str | None = None
) -> tuple[bytes, dict[str, str], str]:
    data = (Path(__file__).parents[1] / "fixtures/synthetic/clean.xlsx").read_bytes()
    response = client.post(
        "/api/v1/workbooks",
        files={"file": ("Résumé.xlsx", data)},
        headers=workflow_auth(session=session),
    )
    assert response.status_code == 202, response.text
    accepted = response.json()
    path = f"/api/v1/workbooks/{accepted['workbookId']}/versions/{accepted['versionId']}"
    return data, accepted, path


def workflow_tick(client: TestClient) -> None:
    from app.jobs.worker import AuditWorker

    assert AuditWorker(client.app.state.services).run_once()


def test_http_workflow_reports_replay_history_and_generated_reaudit(
    workflow_client: TestClient,
) -> None:
    """Backend integration only: synthetic findings, real immutable storage and jobs."""
    client = workflow_client
    data, accepted, path = workflow_upload(client)
    ap = f"{path}/audits/{accepted['auditId']}"
    assert client.get(ap + "/review", headers=workflow_auth()).json()["code"] == "AUDIT_NOT_READY"
    assert client.get(ap + "/report", headers=workflow_auth()).json()["code"] == "REPORT_NOT_READY"
    workflow_tick(client)
    audit = client.get(ap, headers=workflow_auth()).json()
    review = client.get(ap + "/review", headers=workflow_auth()).json()
    assert audit["status"] == "REVIEW_REQUIRED"
    assert review["items"][0]["allowedActions"] == ["DECLINE", "MANUAL_REVIEW"]
    source_report = client.get(ap + "/report", headers=workflow_auth())
    assert source_report.status_code == 200
    assert source_report.json()["remediationId"] is None
    assert source_report.json()["approvedChanges"] == []
    assert source_report.headers["cache-control"] == "no-store"
    body = {
        "auditId": accepted["auditId"],
        "decisions": [{"findingId": audit["findings"][0]["findingId"], "action": "DECLINE"}],
    }
    missing_key = client.post(path + "/remediations", json=body, headers=workflow_auth())
    assert (missing_key.status_code, missing_key.json()["code"]) == (
        400,
        "IDEMPOTENCY_KEY_REQUIRED",
    )
    headers = workflow_auth() | {"Idempotency-Key": "same-request"}
    response = client.post(path + "/remediations", json=body, headers=headers)
    assert response.status_code == 202, response.text
    original_acceptance = response.json()
    assert (
        client.post(path + "/remediations", json=body, headers=headers).json()
        == original_acceptance
    )
    conflicting = {
        "auditId": body["auditId"],
        "decisions": [{**body["decisions"][0], "action": "MANUAL_REVIEW"}],
    }
    conflict = client.post(path + "/remediations", json=conflicting, headers=headers)
    assert (conflict.status_code, conflict.json()["code"]) == (409, "IDEMPOTENCY_CONFLICT")
    active = client.post(path + "/audits", json={}, headers=workflow_auth())
    assert (active.status_code, active.json()["code"]) == (409, "ACTIVE_OPERATION")
    status_path = "/api/v1/remediations/" + response.json()["remediationId"]
    pending = client.get(status_path, headers=workflow_auth()).json()
    assert pending["status"] == "REMEDIATING"
    assert (
        pending["outputId"] is None
        and pending["outputVersionId"] is None
        and pending["auditId"] is None
    )
    workflow_tick(client)
    completed = client.get(status_path, headers=workflow_auth()).json()
    assert completed["status"] == "REMEDIATED", completed
    assert completed["outputVersionId"] != accepted["versionId"]
    output_path = (
        f"/api/v1/workbooks/{accepted['workbookId']}/versions/{completed['outputVersionId']}"
    )
    output = client.get(
        "/api/v1/outputs/" + completed["outputId"] + "/workbook", headers=workflow_auth()
    )
    source = client.get(path + "/source", headers=workflow_auth())
    generated_source = client.get(output_path + "/source", headers=workflow_auth())
    assert output.content == source.content == generated_source.content == data
    assert output.headers["content-length"] == str(len(data))
    assert output.headers["cache-control"] == "no-store"
    assert "filename*=UTF-8''" in output.headers["content-disposition"]
    output_metadata = client.get(output_path, headers=workflow_auth()).json()
    assert output_metadata["sha256"] == hashlib.sha256(data).hexdigest()
    assert output_metadata["sourceVersionId"] == accepted["versionId"]
    assert output_metadata["status"] == "REMEDIATED"
    output_ap = f"{output_path}/audits/{completed['auditId']}"
    generated_report = client.get(output_ap + "/report", headers=workflow_auth()).json()
    assert generated_report["remediationId"] == original_acceptance["remediationId"]
    assert generated_report["outputId"] == completed["outputId"]
    assert generated_report["decisions"][0]["sourceAuditId"] == accepted["auditId"]
    assert generated_report["decisions"][0]["value"] is None
    assert generated_report["approvedChanges"] == []
    assert generated_report["unresolvedFindings"] == generated_report["findings"]
    assert client.get(ap + "/report", headers=workflow_auth()).content == source_report.content
    assert client.get(ap, headers=workflow_auth()).json() == audit
    # A generated version remains REMEDIATED after a later successful audit, while
    # the new audit accurately exposes unresolved findings and original provenance.
    reaudit = client.post(output_path + "/audits", headers=workflow_auth())
    assert reaudit.status_code == 202
    assert (
        client.get(output_ap + "/review", headers=workflow_auth()).json()["code"] == "STALE_AUDIT"
    )
    workflow_tick(client)
    assert client.get(output_path, headers=workflow_auth()).json()["status"] == "REMEDIATED"
    later_report = client.get(
        output_path + "/audits/" + reaudit.json()["auditId"] + "/report", headers=workflow_auth()
    ).json()
    assert later_report["decisions"] == generated_report["decisions"]
    assert later_report["remediationId"] == generated_report["remediationId"]
    # A replay remains recoverable even after the source accepts a fresh audit.
    assert client.post(path + "/audits", json={}, headers=workflow_auth()).status_code == 202
    assert (
        client.post(path + "/remediations", json=body, headers=headers).json()
        == original_acceptance
    )
    workflow_tick(client)
    history = client.get(
        f"/api/v1/workbooks/{accepted['workbookId']}/history", headers=workflow_auth()
    ).json()
    assert [entry["versionId"] for entry in history["versions"]] == [
        accepted["versionId"],
        completed["outputVersionId"],
    ]
    assert [entry["auditId"] for entry in history["audits"]][:2] == [
        accepted["auditId"],
        completed["auditId"],
    ]
    assert len(history["audits"]) == 4
    # A new upload remains a user-supplied input even when its bytes match both
    # the earlier upload and generated output.
    new_upload = client.post(
        f"/api/v1/workbooks/{accepted['workbookId']}/versions",
        files={"file": ("Résumé.xlsx", data)},
        headers=workflow_auth(),
    )
    assert new_upload.status_code == 202, new_upload.text
    newer = new_upload.json()
    assert newer["workbookId"] == accepted["workbookId"]
    assert newer["versionId"] not in (accepted["versionId"], completed["outputVersionId"])
    assert newer["auditId"] != accepted["auditId"]
    newer_path = f"/api/v1/workbooks/{newer['workbookId']}/versions/{newer['versionId']}"
    assert client.get(newer_path, headers=workflow_auth()).json()["sourceVersionId"] is None
    workflow_tick(client)
    assert (
        client.get(newer_path + "/audits/" + newer["auditId"], headers=workflow_auth()).json()[
            "findings"
        ]
        == audit["findings"]
    )
    for definition, value in (
        ("UploadResponse", accepted),
        ("VersionResponse", output_metadata),
        ("AuditResponse", audit),
        ("ReviewResponse", review),
        ("RemediationRequest", body),
        ("RemediationAccepted", original_acceptance),
        ("RemediationResponse", pending),
        ("RemediationResponse", completed),
        ("AuditReport", source_report.json()),
        ("AuditReport", generated_report),
        ("AuditReport", later_report),
        ("HistoryResponse", history),
    ):
        shared_schema_validate(definition, value)


def shared_schema_validate(definition: str, value: object) -> None:
    import json

    from jsonschema import Draft202012Validator, FormatChecker

    path = Path(__file__).parents[2] / "docs/contracts/phase4/phase4-wire-schemas.json"
    definitions = json.loads(path.read_text(encoding="utf-8"))["$defs"]
    Draft202012Validator(
        {"$defs": definitions, "$ref": f"#/$defs/{definition}"}, format_checker=FormatChecker()
    ).validate(value)


def test_http_readonly_reviewer_and_business_admin_access(workflow_client: TestClient) -> None:
    client = workflow_client
    data, accepted, path = workflow_upload(client)
    workflow_tick(client)
    ap = path + "/audits/" + accepted["auditId"]
    audit = client.get(ap, headers=workflow_auth()).json()
    body = {
        "auditId": accepted["auditId"],
        "decisions": [{"findingId": audit["findings"][0]["findingId"], "action": "DECLINE"}],
    }
    acceptance = client.post(
        path + "/remediations", json=body, headers=workflow_auth() | {"Idempotency-Key": "review"}
    )
    assert acceptance.status_code == 202
    status_path = "/api/v1/remediations/" + acceptance.json()["remediationId"]
    workflow_tick(client)
    status = client.get(status_path, headers=workflow_auth()).json()
    output_path = "/api/v1/outputs/" + status["outputId"] + "/workbook"
    history_path = f"/api/v1/workbooks/{accepted['workbookId']}/history"
    for read_path in (
        path,
        path + "/source",
        ap,
        ap + "/review",
        ap + "/report",
        status_path,
        output_path,
        history_path,
    ):
        assert client.get(read_path, headers=workflow_auth("reviewer")).status_code == 200
        for identity in ("other", "ungranted", "admin"):
            assert client.get(read_path, headers=workflow_auth(identity)).status_code == 403
    reviewer_context = client.get(ap + "/review", headers=workflow_auth("reviewer")).json()
    assert all(item["allowedActions"] == [] for item in reviewer_context["items"])
    assert client.get(output_path, headers=workflow_auth("reviewer")).content == data
    for mutate_path, json_body in ((path + "/audits", {}), (path + "/remediations", body)):
        response = client.post(mutate_path, json=json_body, headers=workflow_auth("reviewer"))
        assert response.status_code == 403
    response = client.post(
        f"/api/v1/workbooks/{accepted['workbookId']}/versions",
        content=b"invalid",
        headers=workflow_auth("reviewer"),
    )
    assert response.status_code == 403


def test_http_namespace_derivation_and_cleanup_preserves_ordinary_data(
    workflow_client: TestClient,
) -> None:
    client = workflow_client
    ordinary_bytes, _, ordinary_path = workflow_upload(client)
    workflow_tick(client)
    for session in ("one", "two"):
        result = client.post(
            "/test/setup", json={"session_id": session, "setup": {}}, headers=workflow_auth("admin")
        )
        assert result.status_code == 201, result.text
    data, accepted, path = workflow_upload(client, "one")
    workflow_tick(client)
    headers = workflow_auth(session="one")
    ap = path + "/audits/" + accepted["auditId"]
    audit = client.get(ap, headers=headers).json()
    body = {
        "auditId": accepted["auditId"],
        "decisions": [{"findingId": audit["findings"][0]["findingId"], "action": "DECLINE"}],
    }
    result = client.post(
        path + "/remediations", json=body, headers=headers | {"Idempotency-Key": "namespace-key"}
    )
    assert result.status_code == 202, result.text
    status_path = "/api/v1/remediations/" + result.json()["remediationId"]
    active_cleanup = client.post(
        "/test/setup",
        json={"session_id": "blocked-cleanup", "setup": {"cleanup_session_ids": ["one"]}},
        headers=workflow_auth("admin"),
    )
    assert active_cleanup.status_code == 409
    assert client.app.state.services.repository.get_test_session("one").state == "READY"
    assert (
        client.app.state.services.repository.get_test_session("blocked-cleanup").state == "FAILED"
    )
    workflow_tick(client)
    status = client.get(status_path, headers=headers).json()
    assert status["status"] == "REMEDIATED", status
    output_path = "/api/v1/outputs/" + status["outputId"] + "/workbook"
    generated_path = (
        f"/api/v1/workbooks/{accepted['workbookId']}/versions/{status['outputVersionId']}"
    )
    generated_ap = generated_path + "/audits/" + status["auditId"]
    for scoped_path in (
        path + "/source",
        ap,
        ap + "/report",
        ap + "/review",
        status_path,
        output_path,
        generated_path,
        generated_ap + "/report",
    ):
        assert client.get(scoped_path, headers=headers).status_code == 200
        assert client.get(scoped_path, headers=workflow_auth()).status_code == 404
        assert client.get(scoped_path, headers=workflow_auth(session="two")).status_code == 404
        assert client.get(scoped_path, headers=workflow_auth("other", "one")).status_code == 403
        assert client.get(scoped_path, headers=workflow_auth("reviewer", "one")).status_code == 200
    assert client.get(output_path, headers=headers).content == data
    cleanup = client.post(
        "/test/setup",
        json={"session_id": "cleanup", "setup": {"cleanup_session_ids": ["one"]}},
        headers=workflow_auth("admin"),
    )
    assert cleanup.status_code == 201, cleanup.text
    assert client.get(path + "/source", headers=headers).status_code == 400
    assert client.get(ordinary_path + "/source", headers=workflow_auth()).content == ordinary_bytes
    services = client.app.state.services
    assert services.repository.get_workbook(accepted["workbookId"]) is None
    assert services.repository.get_remediation(result.json()["remediationId"]) is None
    assert services.repository.get_output(status["outputId"]) is None
    assert services.repository.get_report(accepted["auditId"]) is None
    assert not list((services.settings.blob_root / "tests" / "one").rglob("source"))


@pytest.mark.parametrize("identity", ["reviewer", "admin"])
def test_initial_upload_rejects_wrong_role_before_malformed_body(
    workflow_client: TestClient,
    identity: str,
) -> None:
    response = workflow_client.post(
        "/api/v1/workbooks", content=b"not a multipart request", headers=workflow_auth(identity)
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
