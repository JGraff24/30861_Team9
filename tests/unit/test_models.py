"""Foundation validation: independent payloads, malformed boundaries and states."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from app.contracts.errors import EngineExecutionError, WorkbookValidationError
from app.contracts.models import (
    AuditEvaluation,
    AuditRecord,
    AuditResponse,
    AuditStatus,
    CoverageMeasure,
    ExecutionErrorKind,
    ExecutionMeasurement,
    ExecutionResult,
    Finding,
    FindingCategory,
    FindingClassification,
    Identifier,
    JobRecord,
    Principal,
    ProcessingLimits,
    ProjectError,
    RunRequest,
    SemVer,
    SetupResponse,
    SuiteCoverage,
    SuiteReport,
    SystemReport,
    SystemSetupRequest,
    TestResult,
    TestSession,
    UploadResponse,
    ValidatedWorkbook,
    VersionRecord,
    VersionResponse,
    VersionStatus,
    WorkbookRecord,
)
from app.contracts.models import TestEndpointError as EndpointError
from app.contracts.models import TesterSetupRequest as TargetSetupRequest

NOW = datetime(2026, 10, 14, 16, tzinfo=UTC)


def finding_payload() -> dict[str, object]:
    return {
        "findingId": "a" * 64,
        "ruleId": "ALT_TEXT",
        "category": "NON_TEXT_CONTENT",
        "worksheet": "Expenses",
        "target": "xl/drawings/drawing1.xml#id=7",
        "classification": "REQUIRES_USER_INPUT",
        "message": "Missing alternative text.",
        "recommendation": "Provide a title and description.",
    }


def version_payload() -> dict[str, object]:
    return {
        "workbookId": "w_1",
        "versionId": "v-1",
        "fileName": "budget.xlsx",
        "sha256": "a" * 64,
        "createdAt": "2026-10-14T16:00:00Z",
        "status": "PROCESSING",
        "latestAuditId": None,
        "sourceVersionId": None,
    }


def audit_payload() -> dict[str, object]:
    return {
        "auditId": "a-1",
        "workbookId": "w_1",
        "versionId": "v-1",
        "rulesetVersion": "1.0.0",
        "status": "PROCESSING",
        "startedAt": "2026-10-14T16:00:00Z",
        "completedAt": None,
        "findings": [],
        "limitations": [],
        "error": None,
    }


@pytest.mark.parametrize("value", ["x", "A_b-9", "a" * 64])
def test_valid_identifiers(value: str) -> None:
    assert TypeAdapter(Identifier).validate_python(value) == value


@pytest.mark.parametrize("value", ["", "a" * 65, "a/b", "a b", "é", "a\n", "../x", 5])
def test_invalid_identifiers(value: object) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Identifier).validate_python(value)


@pytest.mark.parametrize("value", ["1.0.0", "0.0.0", "1.2.3-alpha.1+build.01"])
def test_semver(value: str) -> None:
    assert TypeAdapter(SemVer).validate_python(value) == value


@pytest.mark.parametrize("value", ["1.0", "01.0.0", "1.0.0-01", "v1.0.0", "1.0.0-"])
def test_invalid_semver(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(SemVer).validate_python(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("findingId", "a" * 63),
        ("findingId", "A" * 64),
        ("findingId", "g" * 64),
        ("category", "IMAGE"),
        ("classification", "AUTO"),
        ("message", ""),
        ("recommendation", " "),
        ("message", "x" * 501),
        ("recommendation", "x" * 501),
    ],
)
def test_finding_rejects_invalid_fields(field: str, value: str) -> None:
    payload = finding_payload() | {field: value}
    with pytest.raises(ValidationError):
        Finding.model_validate(payload)


@pytest.mark.parametrize("category", list(FindingCategory))
@pytest.mark.parametrize("classification", list(FindingClassification))
def test_full_phase2_finding_enums(category: str, classification: str) -> None:
    finding = Finding.model_validate(
        finding_payload()
        | {
            "category": category,
            "classification": classification,
            "message": "x" * 500,
            "recommendation": "x" * 500,
        }
    )
    assert finding.category == category
    assert finding.classification == classification


def test_business_casing_and_nullable_fields() -> None:
    version = VersionResponse.model_validate(version_payload())
    wire = version.model_dump(mode="json")
    assert wire["latestAuditId"] is None and wire["sourceVersionId"] is None
    assert wire["createdAt"] == "2026-10-14T16:00:00Z"
    assert "workbook_id" not in wire
    audit = AuditResponse.model_validate(audit_payload()).model_dump(mode="json")
    assert audit["completedAt"] is None and audit["error"] is None
    assert audit["findings"] == [] and audit["limitations"] == []
    with pytest.raises(ValidationError):
        VersionResponse.model_validate(version_payload() | {"workbook_id": "w"})
    with pytest.raises(ValidationError):
        VersionResponse.model_validate(
            {k: v for k, v in version_payload().items() if k != "sourceVersionId"}
        )


@pytest.mark.parametrize("status", list(VersionStatus))
def test_full_version_statuses(status: str) -> None:
    assert VersionResponse.model_validate(version_payload() | {"status": status}).status == status


@pytest.mark.parametrize(
    "field,value",
    [
        ("fileName", ""),
        ("fileName", "x" * 256),
        ("sha256", "A" * 64),
        ("createdAt", "2026-10-14T16:00:00"),
        ("status", "REJECTED"),
    ],
)
def test_version_boundaries(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        VersionResponse.model_validate(version_payload() | {field: value})


def test_dates_normalize_to_utc_and_file_name_at_limit() -> None:
    wire = VersionResponse.model_validate(
        version_payload()
        | {
            "createdAt": "2026-10-14T12:00:00-04:00",
            "fileName": "x" * 255,
        }
    ).model_dump(mode="json")
    assert wire["createdAt"] == "2026-10-14T16:00:00Z"


@pytest.mark.parametrize("covered,total", [(None, None), (0, 0), (0, 6), (6, 6)])
def test_coverage_valid(covered: int | None, total: int | None) -> None:
    assert (
        CoverageMeasure(covered=covered, total=total, basis="Measured source lines").covered
        == covered
    )


@pytest.mark.parametrize(
    "covered,total,basis",
    [
        (None, 6, "Unavailable"),
        (0, None, "Unavailable"),
        (7, 6, "Lines"),
        (-1, 6, "Lines"),
        (0, -1, "Lines"),
        (0, 6, ""),
        (0, 6, " "),
        (True, 6, "Lines"),
        (1.5, 6, "Lines"),
        ("1", 6, "Lines"),
    ],
)
def test_coverage_rejects_invalid_counts(covered: object, total: object, basis: str) -> None:
    with pytest.raises(ValidationError):
        CoverageMeasure.model_validate({"covered": covered, "total": total, "basis": basis})


@pytest.mark.parametrize("outcome", ["pass", "fail", "skip"])
def test_result_source_omitted_only_on_nonerrors(outcome: str) -> None:
    result = TestResult.model_validate(
        {"test_id": "P3-01", "outcome": outcome, "message": "Observed"}
    )
    assert "source" not in result.model_dump(mode="json")
    with pytest.raises(ValidationError):
        TestResult.model_validate(
            {"test_id": "P3-01", "outcome": outcome, "message": "Observed", "source": "system"}
        )


@pytest.mark.parametrize("source", ["tester", "system", "communication", "unknown"])
def test_error_result_sources(source: str) -> None:
    result = TestResult.model_validate(
        {
            "test_id": "P3-01",
            "outcome": "error",
            "message": "Evaluation prevented",
            "source": source,
        }
    )
    assert result.model_dump(mode="json")["source"] == source


@pytest.mark.parametrize(
    "changes",
    [
        {"test_id": ""},
        {"test_id": " "},
        {"message": ""},
        {"message": " "},
        {"outcome": "unknown"},
        {"outcome": "error"},
        {"source": "network"},
    ],
)
def test_result_required_fields(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TestResult.model_validate(
            {"test_id": "P3-01", "outcome": "pass", "message": "Observed"} | changes
        )


def test_instructor_names_and_duplicate_report_ids() -> None:
    coverage = CoverageMeasure(covered=None, total=None, basis="Instrumentation unavailable")
    result = TestResult(test_id="P3-01", outcome="pass", message="Matched")
    report = SuiteReport(
        session_id="run-123",
        target_url="http://127.0.0.1:8000",
        results=[result],
        coverage=SuiteCoverage(code=coverage, component=coverage, system=coverage),
    )
    assert set(report.model_dump(mode="json")) == {
        "session_id",
        "target_url",
        "results",
        "coverage",
    }
    assert "source" not in report.model_dump(mode="json")["results"][0]
    with pytest.raises(ValidationError):
        SuiteReport.model_validate(report.model_dump() | {"results": [result, result]})
    assert SystemSetupRequest(session_id="run-123").setup == {}
    for model in (SetupResponse, RunRequest):
        assert model.model_validate({"session_id": "run-123"}).session_id == "run-123"
        with pytest.raises(ValidationError):
            model.model_validate({"sessionId": "run-123"})
    assert TargetSetupRequest(target_url="http://127.0.0.1:8000").target_url
    assert (
        SystemReport.model_validate(
            {
                "session_id": "run-123",
                "coverage": {"code": coverage.model_dump()},
            }
        ).coverage.code.total
        is None
    )


@pytest.mark.parametrize("setup", [{"unexpected": 1}, {"cleanup_session_ids": ["../x"]}])
def test_system_setup_rejects_unknown_or_malformed_cleanup(setup: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SystemSetupRequest.model_validate({"session_id": "s", "setup": setup})


def test_typed_errors_have_distinct_envelopes() -> None:
    error = WorkbookValidationError(
        422, "INVALID_WORKBOOK", "Invalid workbook", {"reason": "corrupt"}
    )
    assert set(error.to_envelope("request-123").model_dump()) == {
        "code",
        "message",
        "requestId",
        "details",
    }
    assert error.status_code == 422
    assert EndpointError(message="Setup failed", source="system").model_dump(mode="json") == {
        "message": "Setup failed",
        "source": "system",
    }
    execution = EngineExecutionError(
        500, "ENGINE_TIMEOUT", "Timed out", kind=ExecutionErrorKind.TIMEOUT
    )
    assert execution.kind == "timeout" and execution.measurement is None


@pytest.mark.parametrize(
    "changes",
    [
        {"completedAt": "2026-10-14T16:00:01Z"},
        {"findings": [finding_payload()]},
        {"status": "AUDIT_COMPLETE"},
        {"status": "PROCESSING_FAILED"},
        {"status": "REVIEW_REQUIRED", "completedAt": "2026-10-14T16:00:01Z"},
        {"status": "AUDIT_COMPLETE", "completedAt": "2026-10-14T15:59:59Z"},
    ],
)
def test_invalid_audit_states(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AuditResponse.model_validate(audit_payload() | changes)


def test_terminal_audit_states_and_frozen_models() -> None:
    for status, findings, error in (
        ("AUDIT_COMPLETE", [], None),
        ("REVIEW_REQUIRED", [finding_payload()], None),
        ("REMEDIATION_AVAILABLE", [], None),
        (
            "PROCESSING_FAILED",
            [],
            {"code": "TIMEOUT", "message": "Timed out", "requestId": "req", "details": {}},
        ),
    ):
        audit = AuditResponse.model_validate(
            audit_payload()
            | {
                "status": status,
                "completedAt": "2026-10-14T16:00:01Z",
                "findings": findings,
                "error": error,
            }
        )
        assert audit.status == status
        with pytest.raises(ValidationError):
            audit.status = AuditStatus.PROCESSING


def test_all_internal_records_and_execution_results_are_concrete() -> None:
    common = {"owner_subject": "owner", "session_id": None}
    workbook = WorkbookRecord(workbook_id="w", created_at=NOW, **common)
    version = VersionRecord(
        version_id="v",
        workbook_id="w",
        file_name="book.xlsx",
        source_sha256="0" * 64,
        blob_key="sources/w/v",
        size_bytes=10,
        worksheet_count=1,
        nonempty_cell_count=0,
        created_at=NOW,
        status="PROCESSING",
        latest_audit_id="a",
        source_version_id=None,
        **common,
    )
    audit = AuditRecord(
        audit_id="a",
        workbook_id="w",
        version_id="v",
        ruleset_version="1.0.0",
        status="PROCESSING",
        started_at=NOW,
        completed_at=None,
        findings=[],
        limitations=[],
        error=None,
        **common,
    )
    job = JobRecord(
        job_id="j",
        audit_id="a",
        workbook_id="w",
        version_id="v",
        session_id=None,
        state="QUEUED",
        created_at=NOW,
        queue_deadline=NOW + timedelta(seconds=15),
        worker_id=None,
        lease_expires_at=None,
    )
    session = TestSession(
        session_id="s",
        owner_subject="admin",
        allowed_subjects=["owner"],
        state="PREPARING",
        created_at=NOW,
        revision="unknown",
        cleanup_error=None,
    )
    validated = ValidatedWorkbook(
        source_sha256="0" * 64,
        size_bytes=10,
        worksheet_count=1,
        nonempty_cell_count=0,
        worksheet_parts={"Expenses": "xl/worksheets/sheet1.xml"},
    )
    assert workbook.workbook_id == version.workbook_id == audit.workbook_id == job.workbook_id
    assert session.state == "PREPARING"
    assert Principal(subject="owner", role="user").subject == "owner"
    assert ProcessingLimits().max_file_bytes == 25_000_000
    assert ExecutionResult(
        operation="validate", validated=validated, evaluation=None, measurement=None
    )
    assert ExecutionResult(
        operation="audit",
        validated=None,
        evaluation=AuditEvaluation(findings=[], limitations=[]),
        measurement=None,
    )
    with pytest.raises(ValidationError):
        ExecutionResult(operation="audit", validated=validated, evaluation=None, measurement=None)


@pytest.mark.parametrize(
    "path,lines,covered",
    [
        ("/app/audit.py", [1], [1]),
        ("../audit.py", [1], [1]),
        ("C:\\audit.py", [1], [1]),
        ("app/audit.py", [2, 1], [1]),
        ("app/audit.py", [1, 1], [1]),
        ("app/audit.py", [1], [2]),
        ("app/audit.py", [0], []),
    ],
)
def test_measurement_path_and_line_constraints(
    path: str, lines: list[int], covered: list[int]
) -> None:
    with pytest.raises(ValidationError):
        ExecutionMeasurement(
            session_id="s",
            invocation_id="i",
            revision="unknown",
            executable_lines={path: lines},
            covered_lines={path: covered},
            unavailable_reason=None,
        )


def test_measurement_and_error_json_values() -> None:
    measure = ExecutionMeasurement(
        session_id="s",
        invocation_id="i",
        revision="unknown",
        executable_lines={"app/audit.py": [1, 2]},
        covered_lines={"app/audit.py": [1]},
        unavailable_reason=None,
    )
    assert measure.covered_lines == {"app/audit.py": [1]}
    error = ProjectError(
        code="E", message="Failed", requestId="r", details={"limit": 25, "value": None}
    )
    assert error.model_dump(mode="json")["details"] == {"limit": 25, "value": None}
    with pytest.raises(ValidationError):
        UploadResponse(workbookId="w", versionId="v", auditId="a", status="AUDIT_COMPLETE")
