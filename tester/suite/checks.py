"""Black-box Phase 3 checks. Each returns a pass message or raises one outcome signal."""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, JsonValue

from app.contracts.models import (
    AuditResponse,
    AuditStatus,
    CoverageMeasure,
    HealthResponse,
    ProjectError,
    SystemReport,
    TestEndpointError,
    TestResult,
    UploadResponse,
    VersionResponse,
)
from tester.suite.identity import finding_id
from tester.suite.manifest import LoadedSuite, PlannedTest
from tester.suite.workbooks import (
    cells_workbook,
    padded_clean_workbook,
    protected_workbook,
    worksheets_workbook,
)
from tester.target import DeadlineExceeded, Identity, TargetClient

UPLOAD_LIMIT_BYTES = 25_000_000
WORKSHEET_LIMIT = 50
NONEMPTY_CELL_LIMIT = 250_000
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
    current: PlannedTest | None = None
    # Outcomes recorded so far in this run (read-only for report-format self-checks).
    results: list[TestResult] = field(default_factory=list)
    # Nonzero while a check performs shared setup; setup calls never earn coverage markers.
    setup_depth: int = 0
    markers: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    code: CoverageMeasure = field(
        default_factory=lambda: CoverageMeasure(
            covered=None,
            total=None,
            basis="Target code coverage unavailable: retrieval not executed",
        )
    )

    def qualify(
        self, *, components: list[str] | None = None, acceptance: list[str] | None = None
    ) -> None:
        """Record only the predeclared assertions/calls that have actually been reached."""
        if self.current is None or self.setup_depth:
            return
        row = self.markers.setdefault(self.current.test_id, {"components": [], "acceptance": []})
        for key, requested, allowed in (
            ("components", components, self.current.components),
            ("acceptance", acceptance, self.current.acceptance),
        ):
            selected = allowed if requested is None else requested
            if set(selected) - set(allowed):
                raise RuntimeError("coverage marker was not predeclared")
            row[key] = sorted(set(row[key]) | set(selected))

    @property
    def parameters(self) -> dict[str, JsonValue]:
        return self.current.parameters if self.current else {}


def expect_status(response: httpx.Response, status: int, action: str) -> None:
    if response.status_code == status:
        return
    if response.status_code >= 500 and status < 500:
        raise SystemFault(f"{action} returned unexpected HTTP {response.status_code}.")
    raise CheckFailed(f"{action} returned HTTP {response.status_code}; expected {status}.")


def parse[ModelT: BaseModel](response: httpx.Response, model: type[ModelT], action: str) -> ModelT:
    try:
        if model in (VersionResponse, AuditResponse):
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("response body must be an object")
            fields = ("createdAt",) if model is VersionResponse else ("startedAt", "completedAt")
            for name in fields:
                value = body.get(name)
                if value is not None and (not isinstance(value, str) or not value.endswith("Z")):
                    raise ValueError("wire timestamps must be UTC RFC3339 ending Z")
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


def evaluate_project_error(
    ctx: SuiteContext, response: httpx.Response, status: int, action: str, code: str | None = None
) -> ProjectError:
    if response.status_code >= 500:
        expect_status(response, status, action)
    ctx.qualify()
    return project_error(response, status, action, code)


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


def accepted_ids(ctx: SuiteContext, response: httpx.Response, action: str) -> UploadResponse:
    ids = parse(response, UploadResponse, action)
    values = {ids.workbookId, ids.versionId, ids.auditId}
    seen: set[str] = ctx.state.setdefault("accepted_resource_ids", set())
    if len(values) != 3 or values & seen:
        raise CheckFailed(f"{action} did not allocate three distinct fresh resource IDs.")
    seen.update(values)
    return ids


def version_path(ids: UploadResponse) -> str:
    return f"/api/v1/workbooks/{ids.workbookId}/versions/{ids.versionId}"


def audit_path(ids: UploadResponse, audit_id: str | None = None) -> str:
    return f"{version_path(ids)}/audits/{audit_id or ids.auditId}"


def require(ctx: SuiteContext, key: str, producer: str) -> Any:
    if key not in ctx.state:
        raise Prerequisite(f"Not executed: {producer} produced no usable result.")
    return ctx.state[key]


def poll_audit(ctx: SuiteContext, ids: UploadResponse, action: str) -> AuditResponse:
    limits = ctx.suite.manifest.limits
    seconds = float(limits.audit_poll_seconds) if limits is not None else AUDIT_POLL_SECONDS
    give_up = ctx.clock() + seconds
    while True:
        left = give_up - ctx.clock()
        if left <= 0:
            raise CheckFailed(f"{action} was still PROCESSING after {seconds:.0f} s.")
        response = ctx.client.request(
            "GET", audit_path(ids), identity="userA", timeout=min(10, left)
        )
        expect_status(response, 200, action)
        ctx.qualify(
            components=[c for c in (ctx.current.components if ctx.current else []) if c == "C5"],
            acceptance=[],
        )
        audit = parse(response, AuditResponse, action)
        if (audit.workbookId, audit.versionId, audit.auditId) != (
            ids.workbookId,
            ids.versionId,
            ids.auditId,
        ):
            raise CheckFailed(f"{action} response identifiers differ from the requested resource.")
        if audit.rulesetVersion != ctx.suite.manifest.ruleset_version:
            raise CheckFailed(f"{action} reports a different ruleset.")
        if audit.status == AuditStatus.PROCESSING_FAILED:
            ctx.state["processing_failure"] = audit
            code = audit.error.code[:64] if audit.error is not None else "missing error"
            raise SystemFault(f"{action} ended PROCESSING_FAILED ({code}).")
        if ctx.clock() > give_up:
            raise CheckFailed(f"{action} exceeded its audit polling deadline.")
        if audit.status != AuditStatus.PROCESSING:
            return audit
        if ctx.client.remaining() < POLL_INTERVAL_SECONDS and limits is not None:
            # Phase 4: running out of suite budget is the tester's deadline, not a target fault.
            raise DeadlineExceeded("suite deadline reached while polling an audit")
        if (
            ctx.clock() + POLL_INTERVAL_SECONDS > give_up
            or ctx.client.remaining() < POLL_INTERVAL_SECONDS
        ):
            raise CheckFailed(f"{action} was still PROCESSING after {seconds:.0f} s.")
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
    evaluate_project_error(ctx, response, 401, "Unauthenticated upload")
    return "Upload without a bearer token was rejected with 401 and the project error envelope."


def upload_invalid_token(ctx: SuiteContext) -> str:
    response = upload(
        ctx, ctx.suite.data["clean"], "clean.xlsx", raw_token="team9-invalid-negative-test-token"
    )
    evaluate_project_error(ctx, response, 401, "Upload with an invalid token")
    return "Upload with an unknown bearer token was rejected with 401."


def upload_missing_file(ctx: SuiteContext) -> str:
    response = upload(ctx, ctx.suite.data["clean"], "clean.xlsx", field_name="attachment")
    evaluate_project_error(ctx, response, 400, "Upload without a file field")
    return "Multipart upload without the file field returned 400 with the project envelope."


def upload_csv(ctx: SuiteContext) -> str:
    response = upload(ctx, b"name,amount\nrent,1200\n", "data.csv", content_type="text/csv")
    evaluate_project_error(ctx, response, 415, "CSV upload")
    return "CSV upload was rejected with 415."


def upload_no_extension(ctx: SuiteContext) -> str:
    response = upload(ctx, ctx.suite.data["clean"], "missing-extension")
    evaluate_project_error(ctx, response, 415, "Missing filename extension")
    return "A filename without a supported extension returned 415."


def upload_xls(ctx: SuiteContext) -> str:
    data = OLE_HEADER + bytes(504)
    response = upload(ctx, data, "legacy.xls", content_type="application/vnd.ms-excel")
    evaluate_project_error(ctx, response, 415, "Legacy .xls upload")
    return "Legacy .xls upload was rejected with 415."


def upload_xlsm(ctx: SuiteContext) -> str:
    response = upload(
        ctx,
        ctx.suite.data["clean"],
        "macro.xlsm",
        content_type="application/vnd.ms-excel.sheet.macroEnabled.12",
    )
    evaluate_project_error(
        ctx, response, 422, "Named .xlsm upload", "MACRO_ENABLED_WORKBOOK_UNSUPPORTED"
    )
    return "Named .xlsm upload returned 422 MACRO_ENABLED_WORKBOOK_UNSUPPORTED."


def upload_corrupt(ctx: SuiteContext) -> str:
    fixture = ctx.suite.rejected("corrupt")
    response = upload(ctx, ctx.suite.data["corrupt"], fixture.upload_name)
    # ProjectError forbids extra fields, so an accepted workbookId would also fail here.
    evaluate_project_error(
        ctx, response, fixture.http_status, "Corrupt workbook upload", fixture.error_code
    )
    return f"Corrupt workbook returned {fixture.http_status} with no accepted identifiers."


def upload_over_limit(ctx: SuiteContext) -> str:
    response = upload(ctx, bytes(UPLOAD_LIMIT_BYTES + 1), "over-limit.xlsx")
    evaluate_project_error(ctx, response, 413, "Upload one byte over the limit")
    return "An upload of 25,000,001 bytes was rejected with 413."


def upload_at_limit(ctx: SuiteContext) -> str:
    data = padded_clean_workbook(UPLOAD_LIMIT_BYTES)
    response = upload(ctx, data, "at-limit.xlsx")
    if response.status_code >= 500:
        expect_status(response, 202, "Upload exactly at the byte limit")
    ctx.qualify()
    expect_status(response, 202, "Upload exactly at the byte limit")
    ids = accepted_ids(ctx, response, "Upload exactly at the byte limit")
    drain_intake_audit(ctx, ids, "Byte boundary audit")
    return "A valid workbook of exactly 25,000,000 bytes was accepted with 202."


def drain_intake_audit(ctx: SuiteContext, ids: UploadResponse, action: str) -> None:
    """Finish boundary-case work before submitting unrelated Phase 4 accuracy cases.

    A large accepted boundary workbook can legally use most of the 60-second
    audit budget. Leaving it queued/running would make this sequential suite
    accidentally test overload against the separate 15-second queue bound.
    Preparatory polling earns no coverage; genuine audit failures remain errors.
    Legacy suite execution and independent finding expectations stay unchanged.
    """
    if ctx.suite.manifest.phase != "4":
        return
    ctx.setup_depth += 1
    try:
        poll_audit(ctx, ids, action)
    finally:
        ctx.setup_depth -= 1


def _accepted_upload(ctx: SuiteContext, name: str) -> str:
    fixture = ctx.suite.accepted(name)
    response = upload(ctx, ctx.suite.data[name], fixture.upload_name)
    if response.status_code >= 500:
        expect_status(response, 202, f"{name} upload")
    ctx.qualify(components=["C2"], acceptance=["AC-01"])
    expect_status(response, 202, f"{name} upload")
    ctx.state[name] = accepted_ids(ctx, response, f"{name} upload")
    ctx.qualify()
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
    ctx.qualify()
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
    ctx.qualify()
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
    # Repeatability uses an observed valid completed audit, independently of accuracy.
    ctx.state["seeded_findings"] = finding_keys(audit)
    ctx.state["seeded_prose"] = [(f.message, f.recommendation) for f in audit.findings]
    ctx.qualify()
    expected_status = ctx.suite.accepted("seeded_issues").expected_status
    if audit.status != expected_status:
        raise CheckFailed(f"Seeded audit ended {audit.status}; expected {expected_status}.")
    if audit.rulesetVersion != ctx.suite.manifest.ruleset_version:
        raise CheckFailed(f"Seeded audit used ruleset {audit.rulesetVersion}.")
    actual, expected = finding_keys(audit), expected_keys(ctx, "seeded_issues")
    if actual != expected:
        raise CheckFailed(describe_difference(actual, expected))
    return f"Seeded audit returned exactly the {len(expected)} expected findings in contract order."


def hash_after_audit(ctx: SuiteContext) -> str:
    ids: UploadResponse = require(ctx, "seeded_issues", "the seeded upload")
    audit: AuditResponse = require(ctx, "seeded_audit", "the seeded audit")
    response = ctx.client.request("GET", version_path(ids), identity="userA")
    expect_status(response, 200, "Seeded version after audit")
    version = parse(response, VersionResponse, "Seeded version after audit")
    ctx.qualify(components=["C3"], acceptance=[])
    if (version.workbookId, version.versionId) != (ids.workbookId, ids.versionId):
        raise CheckFailed(
            "Post-audit version response identifiers differ from the requested resource."
        )
    ctx.qualify()
    if version.sha256 != ctx.suite.accepted("seeded_issues").sha256:
        raise CheckFailed("Source SHA256 changed after auditing.")
    if version.latestAuditId != audit.auditId or version.status.value != audit.status.value:
        raise CheckFailed("Version latestAuditId/status do not reflect the completed audit.")
    return "Source SHA256 was unchanged after the audit and the version reflects it."


def upload_protected(ctx: SuiteContext) -> str:
    response = upload(ctx, protected_workbook(), "protected.xlsx")
    evaluate_project_error(ctx, response, 422, "Password-protected workbook upload")
    return "A workbook with password-protected structure was rejected with 422."


def _limit_upload(ctx: SuiteContext, data: bytes, name: str, status: int, action: str) -> None:
    response = upload(ctx, data, name)
    if response.status_code >= 500:
        expect_status(response, status, action)
    ctx.qualify()
    if status == 202:
        expect_status(response, 202, action)
        ids = accepted_ids(ctx, response, action)
        drain_intake_audit(ctx, ids, action + " audit")
    else:
        project_error(response, status, action)


def upload_worksheets_over(ctx: SuiteContext) -> str:
    data = worksheets_workbook(WORKSHEET_LIMIT + 1)
    _limit_upload(ctx, data, "too-many-sheets.xlsx", 422, "Worksheet limit + 1")
    return f"A workbook with {WORKSHEET_LIMIT + 1} worksheets was rejected with 422."


def upload_worksheets_at(ctx: SuiteContext) -> str:
    data = worksheets_workbook(WORKSHEET_LIMIT)
    _limit_upload(ctx, data, "sheet-limit.xlsx", 202, "Worksheet limit")
    return f"A workbook with exactly {WORKSHEET_LIMIT} worksheets was accepted with 202."


def upload_worksheets_chartsheet(ctx: SuiteContext) -> str:
    data = worksheets_workbook(WORKSHEET_LIMIT, chartsheet=True)
    _limit_upload(ctx, data, "sheet-limit-chartsheet.xlsx", 202, "Worksheet limit + chartsheet")
    return (
        f"A workbook with exactly {WORKSHEET_LIMIT} worksheets and one chartsheet was accepted "
        "with 202."
    )


def upload_cells_over(ctx: SuiteContext) -> str:
    data = cells_workbook(NONEMPTY_CELL_LIMIT + 1)
    _limit_upload(ctx, data, "too-many-cells.xlsx", 422, "Nonempty cell limit + 1")
    return f"A workbook with {NONEMPTY_CELL_LIMIT + 1:,} nonempty cells was rejected with 422."


def upload_cells_at(ctx: SuiteContext) -> str:
    data = cells_workbook(NONEMPTY_CELL_LIMIT)
    _limit_upload(ctx, data, "cell-limit.xlsx", 202, "Nonempty cell limit")
    return f"A workbook with exactly {NONEMPTY_CELL_LIMIT:,} nonempty cells was accepted with 202."


def version_unauthenticated(ctx: SuiteContext) -> str:
    ids: UploadResponse = require(ctx, "clean", "the clean upload")
    response = ctx.client.request("GET", version_path(ids))
    evaluate_project_error(ctx, response, 401, "Unauthenticated version")
    return "Version read without a bearer token was rejected with 401."


def audit_unauthenticated(ctx: SuiteContext) -> str:
    ids: UploadResponse = require(ctx, "clean", "the clean upload")
    response = ctx.client.request("GET", audit_path(ids))
    evaluate_project_error(ctx, response, 401, "Unauthenticated audit")
    return "Audit read without a bearer token was rejected with 401."


def audit_foreign_owner(ctx: SuiteContext) -> str:
    ids: UploadResponse = require(ctx, "seeded_issues", "the seeded upload")
    response = ctx.client.request("GET", audit_path(ids), identity="userB")
    evaluate_project_error(ctx, response, 403, "User B reading user A's audit")
    return "User B was refused user A's audit with 403."


def foreign_owner_forbidden(ctx: SuiteContext) -> str:
    ids: UploadResponse = require(ctx, "seeded_issues", "the seeded upload")
    response = ctx.client.request("GET", version_path(ids), identity="userB")
    evaluate_project_error(ctx, response, 403, "User B reading user A's version")
    return "User B was refused user A's version with 403."


def reviewer_version(ctx: SuiteContext) -> str:
    ids: UploadResponse = require(ctx, "seeded_issues", "the seeded upload")
    response = ctx.client.request("GET", version_path(ids), identity="reviewer")
    if response.status_code >= 500:
        expect_status(response, 200, "Authorized reviewer version")
    ctx.qualify()
    expect_status(response, 200, "Authorized reviewer version")
    version = parse(response, VersionResponse, "Authorized reviewer version")
    fixture = ctx.suite.accepted("seeded_issues")
    if (version.workbookId, version.versionId) != (ids.workbookId, ids.versionId):
        raise CheckFailed("Authorized reviewer version names a different resource.")
    if version.sha256 != fixture.sha256 or version.fileName != fixture.upload_name:
        raise CheckFailed("Authorized reviewer version differs from the uploaded workbook.")
    return "An explicitly authorized reviewer retrieved user A's version and matching metadata."


def reviewer_audit(ctx: SuiteContext) -> str:
    ids: UploadResponse = require(ctx, "seeded_issues", "the seeded upload")
    response = ctx.client.request("GET", audit_path(ids), identity="reviewer")
    if response.status_code >= 500:
        expect_status(response, 200, "Authorized reviewer audit")
    ctx.qualify()
    expect_status(response, 200, "Authorized reviewer audit")
    audit = parse(response, AuditResponse, "Authorized reviewer audit")
    if (audit.workbookId, audit.versionId, audit.auditId) != (
        ids.workbookId,
        ids.versionId,
        ids.auditId,
    ):
        raise CheckFailed("Authorized reviewer audit names a different resource.")
    if audit.rulesetVersion != ctx.suite.manifest.ruleset_version:
        raise CheckFailed("Authorized reviewer audit reports a different ruleset.")
    observed: AuditResponse | None = ctx.state.get("seeded_audit")
    if observed is not None and audit.model_dump() != observed.model_dump():
        raise CheckFailed("Authorized reviewer audit differs from the owner's completed audit.")
    return "An explicitly authorized reviewer retrieved user A's audit and matching data."


def unknown_resources(ctx: SuiteContext) -> str:
    case = str(ctx.parameters.get("case", "version-unknown"))
    if case == "version-unknown":
        path = "/api/v1/workbooks/unknown-workbook/versions/unknown-version"
    else:
        seeded: UploadResponse = require(ctx, "seeded_issues", "the seeded upload")
        if case == "version-mismatch":
            clean: UploadResponse = require(ctx, "clean", "the clean upload")
            path = f"/api/v1/workbooks/{seeded.workbookId}/versions/{clean.versionId}"
        elif case == "audit-mismatch":
            clean = require(ctx, "clean", "the clean upload")
            path = audit_path(seeded, clean.auditId)
        else:
            path = audit_path(seeded, "unknown-audit")
    response = ctx.client.request("GET", path, identity="userA")
    evaluate_project_error(ctx, response, 404, case)
    return f"{case} returned 404 with the project error envelope."


def determinism(ctx: SuiteContext) -> str:
    baseline: list[tuple[str, ...]] = require(ctx, "seeded_findings", "the seeded audit")
    prose = require(ctx, "seeded_prose", "the seeded audit")
    fixture = ctx.suite.accepted("seeded_issues")
    run = ctx.parameters.get("run", 2)
    response = upload(ctx, ctx.suite.data["seeded_issues"], fixture.upload_name)
    expect_status(response, 202, f"Determinism upload {run}")
    ids = accepted_ids(ctx, response, f"Determinism upload {run}")
    audit = poll_audit(ctx, ids, f"Determinism audit {run}")
    ctx.qualify()
    if (
        finding_keys(audit) != baseline
        or [(f.message, f.recommendation) for f in audit.findings] != prose
    ):
        raise CheckFailed(f"Audit {run} of identical bytes returned different findings or prose.")
    if ctx.suite.manifest.phase == "4":
        first = require(ctx, "seeded_audit", "the valid first completed audit")
        if (audit.limitations, audit.status) != (first.limitations, first.status):
            raise CheckFailed(f"Audit {run} of identical bytes changed limitations or status.")
    return f"Audit {run} matched the first audit's semantic findings and prose."


def rejected_fixture(ctx: SuiteContext) -> str:
    name = str(ctx.parameters["fixture"])
    fixture = ctx.suite.rejected(name)
    response = upload(ctx, ctx.suite.data[name], fixture.upload_name)
    evaluate_project_error(ctx, response, fixture.http_status, name, fixture.error_code)
    return f"{name} returned {fixture.http_status} with no accepted identifiers."


def edge_upload(ctx: SuiteContext) -> str:
    return _accepted_upload(ctx, str(ctx.parameters["fixture"]))


def edge_audit(ctx: SuiteContext) -> str:
    name = str(ctx.parameters["fixture"])
    ids: UploadResponse = require(ctx, name, f"the {name} upload")
    audit = poll_audit(ctx, ids, f"{name} audit")
    ctx.qualify()
    fixture = ctx.suite.accepted(name)
    if audit.status != fixture.expected_status or finding_keys(audit) != expected_keys(ctx, name):
        raise CheckFailed(f"{name} audit differed from independent expected findings/status.")
    if name == "repeated_ids" and not audit.limitations:
        raise CheckFailed("Repeated object IDs were not disclosed in audit limitations.")
    return f"{name} returned exact independently expected findings."


def controlled_failure(ctx: SuiteContext) -> str:
    audit: AuditResponse = require(
        ctx, "processing_failure", "an observed controlled audit failure"
    )
    ctx.qualify()
    if audit.error is None or audit.completedAt is None or audit.findings:
        raise CheckFailed(
            "Controlled audit failure did not preserve the required failure envelope."
        )
    return "Observed PROCESSING_FAILED has a completion timestamp, error and no partial findings."


def unknown_session_header(ctx: SuiteContext) -> str:
    ids: UploadResponse = require(ctx, "seeded_issues", "the seeded upload")
    response = ctx.client.request(
        "GET",
        version_path(ids),
        identity="userA",
        headers={"X-Test-Session": str(uuid.uuid4())},
    )
    evaluate_project_error(ctx, response, 400, "Project call with an unknown X-Test-Session")
    return "A project call naming an unknown test session returned 400."


def system_setup_duplicate(ctx: SuiteContext) -> str:
    response = ctx.client.request(
        "POST",
        "/test/setup",
        identity="test-admin",
        json={"session_id": ctx.session_id, "setup": {}},
        timeout=30,
    )
    ctx.qualify()
    expect_status(response, 409, "Duplicate system setup")
    parse(response, TestEndpointError, "Duplicate system setup")
    return "Repeating system setup with an existing session_id returned 409."


def system_report(ctx: SuiteContext) -> str:
    response = ctx.client.request(
        "GET", "/test/report", identity="test-admin", params={"session_id": ctx.session_id}
    )
    ctx.code = CoverageMeasure(
        covered=None,
        total=None,
        basis=(
            f"Target code coverage unavailable: system report returned HTTP {response.status_code}"
        ),
    )
    expect_status(response, 200, "System coverage report")
    ctx.qualify()
    ctx.code = CoverageMeasure(
        covered=None,
        total=None,
        basis="Target code coverage unavailable: invalid system report schema/session",
    )
    report = parse(response, SystemReport, "System coverage report")
    if report.session_id != ctx.session_id:
        raise CheckFailed("System report names a different session_id.")
    ctx.code = report.coverage.code
    return "System report returned this session's code coverage in the agreed schema."


CHECKS: dict[str, Callable[[SuiteContext], str]] = {
    check.__name__: check
    for check in (
        health,
        upload_without_token,
        upload_invalid_token,
        upload_missing_file,
        upload_csv,
        upload_no_extension,
        upload_xls,
        upload_xlsm,
        upload_corrupt,
        upload_protected,
        upload_worksheets_over,
        upload_worksheets_at,
        upload_worksheets_chartsheet,
        upload_cells_over,
        upload_cells_at,
        version_unauthenticated,
        audit_unauthenticated,
        audit_foreign_owner,
        upload_over_limit,
        upload_at_limit,
        upload_clean,
        upload_seeded,
        version_clean,
        audit_clean,
        audit_seeded,
        hash_after_audit,
        foreign_owner_forbidden,
        reviewer_version,
        reviewer_audit,
        unknown_resources,
        determinism,
        controlled_failure,
        rejected_fixture,
        edge_upload,
        edge_audit,
        unknown_session_header,
        system_setup_duplicate,
        system_report,
    )
}
