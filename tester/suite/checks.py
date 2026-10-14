"""Black-box Phase 3 checks. Each returns a pass message or raises one outcome signal."""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel

from app.contracts.models import (
    AuditResponse,
    AuditStatus,
    HealthResponse,
    ProjectError,
    SystemReport,
    TestEndpointError,
    UploadResponse,
    VersionResponse,
)
from tester.suite.identity import finding_id
from tester.suite.manifest import LoadedSuite
from tester.suite.workbooks import padded_clean_workbook
from tester.target import Identity, TargetClient

UPLOAD_LIMIT_BYTES = 25_000_000
AUDIT_POLL_SECONDS = 75.0
POLL_INTERVAL_SECONDS = 1.0
DETERMINISM_RUNS = 5
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
OLE_HEADER = bytes.fromhex("D0CF11E0A1B11AE1")


class CheckFailed(Exception):
    """An evaluable response differed from the expectation: outcome fail."""


class SystemFault(Exception):
    """The target returned an unexpected internal error: outcome error, source system."""


class Prerequisite(Exception):
    """An earlier step produced nothing to evaluate: outcome skip (unexecuted)."""


@dataclass
class SuiteContext:
    client: TargetClient
    suite: LoadedSuite
    session_id: str
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    state: dict[str, Any] = field(default_factory=dict)


def expect_status(response: httpx.Response, status: int, action: str) -> None:
    if response.status_code == status:
        return
    if response.status_code >= 500 and status < 500:
        raise SystemFault(f"{action} returned unexpected HTTP {response.status_code}.")
    raise CheckFailed(f"{action} returned HTTP {response.status_code}; expected {status}.")


def parse[ModelT: BaseModel](response: httpx.Response, model: type[ModelT], action: str) -> ModelT:
    try:
        return model.model_validate_json(response.content)
    except ValueError:
        raise CheckFailed(f"{action} body does not match {model.__name__}.") from None


def project_error(
    response: httpx.Response, status: int, action: str, code: str | None = None
) -> ProjectError:
    expect_status(response, status, action)
    error = parse(response, ProjectError, action)
    if code is not None and error.code != code:
        raise CheckFailed(f"{action} returned code {error.code}; expected {code}.")
    return error


def upload(
    ctx: SuiteContext,
    data: bytes,
    name: str,
    *,
    identity: Identity | None = "userA",
    raw_token: str | None = None,
    content_type: str = XLSX,
    field_name: str = "file",
) -> httpx.Response:
    return ctx.client.request(
        "POST",
        "/api/v1/workbooks",
        identity=identity,
        raw_token=raw_token,
        files={field_name: (name, data, content_type)},
        timeout=35,
    )


def version_path(ids: UploadResponse) -> str:
    return f"/api/v1/workbooks/{ids.workbookId}/versions/{ids.versionId}"


def audit_path(ids: UploadResponse, audit_id: str | None = None) -> str:
    return f"{version_path(ids)}/audits/{audit_id or ids.auditId}"


def require(ctx: SuiteContext, key: str, producer: str) -> Any:
    if key not in ctx.state:
        raise Prerequisite(f"Not executed: {producer} produced no usable result.")
    return ctx.state[key]


def poll_audit(ctx: SuiteContext, ids: UploadResponse, action: str) -> AuditResponse:
    give_up = ctx.clock() + AUDIT_POLL_SECONDS
    while True:
        response = ctx.client.request("GET", audit_path(ids), identity="userA")
        expect_status(response, 200, action)
        audit = parse(response, AuditResponse, action)
        if audit.status != AuditStatus.PROCESSING:
            return audit
        if (
            ctx.clock() + POLL_INTERVAL_SECONDS > give_up
            or ctx.client.remaining() < POLL_INTERVAL_SECONDS
        ):
            raise CheckFailed(f"{action} was still PROCESSING after {AUDIT_POLL_SECONDS:.0f} s.")
        ctx.sleep(POLL_INTERVAL_SECONDS)


def finding_keys(audit: AuditResponse) -> list[tuple[str, ...]]:
    return [
        (f.findingId, f.ruleId, f.category, f.worksheet, f.target, f.classification)
        for f in audit.findings
    ]


def expected_keys(ctx: SuiteContext, name: str) -> list[tuple[str, ...]]:
    fixture = ctx.suite.accepted(name)
    ruleset = ctx.suite.manifest.ruleset_version
    keys = [
        (
            finding_id(fixture.sha256, ruleset, f.ruleId, f.worksheet, f.target),
            f.ruleId,
            f.category,
            f.worksheet,
            f.target,
            f.classification,
        )
        for f in fixture.findings
    ]
    # Contract order: (ruleId, worksheet, target) by Unicode code point.
    return sorted(keys, key=lambda key: (key[1], key[3], key[4]))


def describe_difference(actual: list[tuple[str, ...]], expected: list[tuple[str, ...]]) -> str:
    missing = [key for key in expected if key not in actual]
    unexpected = [key for key in actual if key not in expected]
    if missing or unexpected:
        sample = (missing or unexpected)[0]
        label = "missing" if missing else "unexpected"
        return (
            f"{len(missing)} expected finding(s) missing and {len(unexpected)} unexpected; "
            f"first {label}: {sample[1]} {sample[3]!r} {sample[4]} {sample[5]}."
        )
    return "Findings match as a set but are not in (ruleId, worksheet, target) order."


# --- checks, referenced by name from the manifest ---------------------------------------


def health(ctx: SuiteContext) -> str:
    response = ctx.client.request("GET", "/api/v1/health")
    expect_status(response, 200, "Health")
    body = parse(response, HealthResponse, "Health")
    if body.rulesetVersion != ctx.suite.manifest.ruleset_version:
        raise CheckFailed(
            f"Health reports ruleset {body.rulesetVersion}; "
            f"expectations use {ctx.suite.manifest.ruleset_version}."
        )
    return "Health returned AVAILABLE with the expected schema and ruleset."


def upload_without_token(ctx: SuiteContext) -> str:
    response = upload(ctx, ctx.suite.data["clean"], "clean.xlsx", identity=None)
    project_error(response, 401, "Unauthenticated upload")
    return "Upload without a bearer token was rejected with 401 and the project error envelope."


def upload_invalid_token(ctx: SuiteContext) -> str:
    response = upload(
        ctx, ctx.suite.data["clean"], "clean.xlsx", raw_token="team9-invalid-negative-test-token"
    )
    project_error(response, 401, "Upload with an invalid token")
    return "Upload with an unknown bearer token was rejected with 401."


def upload_missing_file(ctx: SuiteContext) -> str:
    response = upload(ctx, ctx.suite.data["clean"], "clean.xlsx", field_name="attachment")
    project_error(response, 400, "Upload without a file field")
    return "Multipart upload without the file field returned 400 with the project envelope."


def upload_csv(ctx: SuiteContext) -> str:
    response = upload(ctx, b"name,amount\nrent,1200\n", "data.csv", content_type="text/csv")
    project_error(response, 415, "CSV upload")
    return "CSV upload was rejected with 415."


def upload_xls(ctx: SuiteContext) -> str:
    data = OLE_HEADER + bytes(504)
    response = upload(ctx, data, "legacy.xls", content_type="application/vnd.ms-excel")
    project_error(response, 415, "Legacy .xls upload")
    return "Legacy .xls upload was rejected with 415."


def upload_xlsm(ctx: SuiteContext) -> str:
    response = upload(
        ctx,
        ctx.suite.data["clean"],
        "macro.xlsm",
        content_type="application/vnd.ms-excel.sheet.macroEnabled.12",
    )
    project_error(response, 422, "Named .xlsm upload", "MACRO_ENABLED_WORKBOOK_UNSUPPORTED")
    return "Named .xlsm upload returned 422 MACRO_ENABLED_WORKBOOK_UNSUPPORTED."


def upload_corrupt(ctx: SuiteContext) -> str:
    fixture = ctx.suite.rejected("corrupt")
    response = upload(ctx, ctx.suite.data["corrupt"], fixture.upload_name)
    # ProjectError forbids extra fields, so an accepted workbookId would also fail here.
    project_error(response, fixture.http_status, "Corrupt workbook upload", fixture.error_code)
    return f"Corrupt workbook returned {fixture.http_status} with no accepted identifiers."


def upload_over_limit(ctx: SuiteContext) -> str:
    response = upload(ctx, bytes(UPLOAD_LIMIT_BYTES + 1), "over-limit.xlsx")
    project_error(response, 413, "Upload one byte over the limit")
    return "An upload of 25,000,001 bytes was rejected with 413."


def upload_at_limit(ctx: SuiteContext) -> str:
    data = padded_clean_workbook(UPLOAD_LIMIT_BYTES)
    response = upload(ctx, data, "at-limit.xlsx")
    expect_status(response, 202, "Upload exactly at the byte limit")
    parse(response, UploadResponse, "Upload exactly at the byte limit")
    return "A valid workbook of exactly 25,000,000 bytes was accepted with 202."


def _accepted_upload(ctx: SuiteContext, name: str) -> str:
    fixture = ctx.suite.accepted(name)
    response = upload(ctx, ctx.suite.data[name], fixture.upload_name)
    expect_status(response, 202, f"{name} upload")
    ctx.state[name] = parse(response, UploadResponse, f"{name} upload")
    return f"{fixture.upload_name} was accepted with 202 and PROCESSING identifiers."


def upload_clean(ctx: SuiteContext) -> str:
    return _accepted_upload(ctx, "clean")


def upload_seeded(ctx: SuiteContext) -> str:
    return _accepted_upload(ctx, "seeded_issues")


def version_clean(ctx: SuiteContext) -> str:
    ids: UploadResponse = require(ctx, "clean", "the clean upload")
    fixture = ctx.suite.accepted("clean")
    response = ctx.client.request("GET", version_path(ids), identity="userA")
    expect_status(response, 200, "Clean version")
    version = parse(response, VersionResponse, "Clean version")
    if (version.workbookId, version.versionId) != (ids.workbookId, ids.versionId):
        raise CheckFailed("Version response identifiers differ from the upload response.")
    if version.sha256 != fixture.sha256:
        raise CheckFailed("Version sha256 differs from the tester's hash of the uploaded bytes.")
    if version.fileName != fixture.upload_name or version.sourceVersionId is not None:
        raise CheckFailed("Version fileName/sourceVersionId differ from the initial upload.")
    return "Version metadata matched the upload and the independently computed SHA256."


def audit_clean(ctx: SuiteContext) -> str:
    ids: UploadResponse = require(ctx, "clean", "the clean upload")
    audit = poll_audit(ctx, ids, "Clean audit")
    expected = ctx.suite.accepted("clean").expected_status
    if audit.status != expected or audit.findings:
        raise CheckFailed(
            f"Clean audit ended {audit.status} with {len(audit.findings)} finding(s)."
        )
    return f"Clean fixture audit ended {expected} with no findings."


def audit_seeded(ctx: SuiteContext) -> str:
    ids: UploadResponse = require(ctx, "seeded_issues", "the seeded upload")
    audit = poll_audit(ctx, ids, "Seeded audit")
    ctx.state["seeded_audit"] = audit
    expected_status = ctx.suite.accepted("seeded_issues").expected_status
    if audit.status != expected_status:
        raise CheckFailed(f"Seeded audit ended {audit.status}; expected {expected_status}.")
    if audit.rulesetVersion != ctx.suite.manifest.ruleset_version:
        raise CheckFailed(f"Seeded audit used ruleset {audit.rulesetVersion}.")
    actual, expected = finding_keys(audit), expected_keys(ctx, "seeded_issues")
    if actual != expected:
        raise CheckFailed(describe_difference(actual, expected))
    ctx.state["seeded_findings"] = actual
    return f"Seeded audit returned exactly the {len(expected)} expected findings in contract order."


def hash_after_audit(ctx: SuiteContext) -> str:
    ids: UploadResponse = require(ctx, "seeded_issues", "the seeded upload")
    audit: AuditResponse = require(ctx, "seeded_audit", "the seeded audit")
    response = ctx.client.request("GET", version_path(ids), identity="userA")
    expect_status(response, 200, "Seeded version after audit")
    version = parse(response, VersionResponse, "Seeded version after audit")
    if version.sha256 != ctx.suite.accepted("seeded_issues").sha256:
        raise CheckFailed("Source SHA256 changed after auditing.")
    if version.latestAuditId != audit.auditId or version.status.value != audit.status.value:
        raise CheckFailed("Version latestAuditId/status do not reflect the completed audit.")
    return "Source SHA256 was unchanged after the audit and the version reflects it."


def foreign_owner_forbidden(ctx: SuiteContext) -> str:
    ids: UploadResponse = require(ctx, "seeded_issues", "the seeded upload")
    response = ctx.client.request("GET", version_path(ids), identity="userB")
    project_error(response, 403, "User B reading user A's version")
    return "User B was refused user A's version with 403."


def unknown_resources(ctx: SuiteContext) -> str:
    seeded: UploadResponse = require(ctx, "seeded_issues", "the seeded upload")
    clean: UploadResponse = require(ctx, "clean", "the clean upload")
    unknown = UploadResponse(
        workbookId="unknown-workbook",
        versionId="unknown-version",
        auditId="unknown-audit",
        status="PROCESSING",
    )
    for path, action in (
        (version_path(unknown), "Unknown version"),
        (f"/api/v1/workbooks/{seeded.workbookId}/versions/{clean.versionId}", "Mismatched IDs"),
        (audit_path(seeded, clean.auditId), "Mismatched audit ID"),
    ):
        project_error(ctx.client.request("GET", path, identity="userA"), 404, action)
    return "Unknown and mismatched version/audit identifiers each returned 404."


def determinism(ctx: SuiteContext) -> str:
    baseline: list[tuple[str, ...]] = require(ctx, "seeded_findings", "the seeded audit")
    fixture = ctx.suite.accepted("seeded_issues")
    for run in range(2, DETERMINISM_RUNS + 1):
        response = upload(ctx, ctx.suite.data["seeded_issues"], fixture.upload_name)
        expect_status(response, 202, f"Determinism upload {run}")
        ids = parse(response, UploadResponse, f"Determinism upload {run}")
        audit = poll_audit(ctx, ids, f"Determinism audit {run}")
        if finding_keys(audit) != baseline:
            raise CheckFailed(f"Audit {run} of identical bytes returned different findings.")
    return f"{DETERMINISM_RUNS} audits of identical bytes returned identical findings."


def unknown_session_header(ctx: SuiteContext) -> str:
    ids: UploadResponse = require(ctx, "seeded_issues", "the seeded upload")
    response = ctx.client.request(
        "GET",
        version_path(ids),
        identity="userA",
        headers={"X-Test-Session": str(uuid.uuid4())},
    )
    project_error(response, 400, "Project call with an unknown X-Test-Session")
    return "A project call naming an unknown test session returned 400."


def system_setup_duplicate(ctx: SuiteContext) -> str:
    response = ctx.client.request(
        "POST",
        "/test/setup",
        identity="test-admin",
        json={"session_id": ctx.session_id, "setup": {}},
        timeout=30,
    )
    expect_status(response, 409, "Duplicate system setup")
    parse(response, TestEndpointError, "Duplicate system setup")
    return "Repeating system setup with an existing session_id returned 409."


def system_report(ctx: SuiteContext) -> str:
    response = ctx.client.request(
        "GET", "/test/report", identity="test-admin", params={"session_id": ctx.session_id}
    )
    expect_status(response, 200, "System coverage report")
    report = parse(response, SystemReport, "System coverage report")
    if report.session_id != ctx.session_id:
        raise CheckFailed("System report names a different session_id.")
    return "System report returned this session's code coverage in the agreed schema."


CHECKS: dict[str, Callable[[SuiteContext], str]] = {
    check.__name__: check
    for check in (
        health,
        upload_without_token,
        upload_invalid_token,
        upload_missing_file,
        upload_csv,
        upload_xls,
        upload_xlsm,
        upload_corrupt,
        upload_over_limit,
        upload_at_limit,
        upload_clean,
        upload_seeded,
        version_clean,
        audit_clean,
        audit_seeded,
        hash_after_audit,
        foreign_owner_forbidden,
        unknown_resources,
        determinism,
        unknown_session_header,
        system_setup_duplicate,
        system_report,
    )
}
