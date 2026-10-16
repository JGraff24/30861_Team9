"""Serializable test-only engine/runner doubles; never registered in production."""

import hashlib
import os
import time
from typing import Literal

from app.contracts.errors import EngineExecutionError
from app.contracts.models import (
    AuditEvaluation,
    ExecutionErrorKind,
    ExecutionResult,
    Finding,
    ProcessingLimits,
    ValidatedWorkbook,
)


class TestEngine:
    __test__ = False

    def validate(self, data: bytes, filename: str, limits: ProcessingLimits) -> ValidatedWorkbook:
        return ValidatedWorkbook(
            source_sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            worksheet_count=1,
            nonempty_cell_count=1,
            worksheet_parts={"Expenses": "xl/worksheets/sheet1.xml"},
        )

    def audit(
        self, data: bytes, validated: ValidatedWorkbook, ruleset_version: str
    ) -> AuditEvaluation:
        return AuditEvaluation(findings=[], limitations=[])


class TestRunner:
    __test__ = False

    def __init__(self, mode: str = "clean") -> None:
        self.mode = mode

    def execute(
        self,
        operation: Literal["validate", "audit"],
        data: bytes,
        filename: str,
        limits: ProcessingLimits,
        ruleset_version: str,
        validated: ValidatedWorkbook | None,
        session_id: str | None,
        revision: str,
        timeout_seconds: float,
        memory_limit_mib: int,
    ) -> ExecutionResult:
        if self.mode == "secrets":
            assert not any(
                key in os.environ
                for key in (
                    "AWS_ACCESS_KEY_ID",
                    "SYSTEM_TOKEN_HASHES_JSON",
                    "PRIVATE_TEST_SECRET",
                )
            )
        if self.mode == "memory" and operation == "audit":
            # Linux subprocess limit must prevent this allocation.
            bytearray(1024 * 1024 * 1024)
        if self.mode == "crash" and operation == "audit":
            os._exit(9)
        if (self.mode == "hang" and operation == "audit") or self.mode == "validation_hang":
            time.sleep(20)
        if self.mode == "reject":
            raise EngineExecutionError(
                422,
                "INVALID_WORKBOOK",
                "Workbook content is invalid.",
                kind=ExecutionErrorKind.VALIDATION_REJECTED,
            )
        if operation == "validate":
            result = TestEngine().validate(data, filename, limits)
            if self.mode == "bad_hash":
                result = result.model_copy(update={"source_sha256": "0" * 64})
            if self.mode == "bad_count":
                result = result.model_copy(update={"worksheet_count": 51})
            if self.mode == "bad_parts":
                result = result.model_copy(update={"worksheet_parts": {"Expenses": "../evil"}})
            return ExecutionResult(
                operation="validate", validated=result, evaluation=None, measurement=None
            )
        findings = []
        if self.mode == "findings":
            for character in ("b", "a"):
                findings.append(
                    Finding(
                        findingId=character * 64,
                        ruleId="WORKSHEET_NAME",
                        category="WORKSHEET_NAME",
                        worksheet=character,
                        target=f"xl/worksheets/{character}.xml#sheetId=1",
                        classification="REQUIRES_USER_INPUT",
                        message="Rename worksheet.",
                        recommendation="Supply a descriptive name.",
                    )
                )
        return ExecutionResult(
            operation="audit",
            validated=None,
            evaluation=AuditEvaluation(findings=findings, limitations=[]),
            measurement=None,
        )


def create_runner(*, engine: TestEngine, enable_code_measurement: bool) -> TestRunner:
    return TestRunner()
