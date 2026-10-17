"""Trusted test engines imported in real child interpreters."""

import hashlib
import os
import time
from pathlib import Path

from app.contracts.errors import WorkbookValidationError
from app.contracts.models import AuditEvaluation, ProcessingLimits, ValidatedWorkbook


class Engine:
    def __init__(self, mode: str = "success") -> None:
        self.mode = mode

    def validate(self, data: bytes, filename: str, limits: ProcessingLimits) -> ValidatedWorkbook:
        if self.mode == "timeout":
            time.sleep(10)
        if self.mode == "crash":
            os._exit(9)
        if self.mode == "reject":
            raise WorkbookValidationError(
                422, "INVALID_WORKBOOK", "Rejected workbook.", {"safe": True}
            )
        if self.mode == "bad_ipc":
            Path("result.json").write_text("invalid json")
            os._exit(0)
        if self.mode == "invalid":
            return None  # type: ignore[return-value]
        if self.mode == "environment":
            assert "AWS_SECRET_ACCESS_KEY" not in os.environ
            assert "SYSTEM_TOKEN_HASHES_JSON" not in os.environ
            assert os.getpgrp() == os.getpgid(os.getppid())
            assert all(part.stat().st_mode & 0o777 == 0o400 for part in Path("source").iterdir())
        if self.mode == "memory":
            bytearray(1024 * 1024 * 1024)
        return ValidatedWorkbook(
            source_sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            worksheet_count=1,
            nonempty_cell_count=0,
            worksheet_parts={},
        )

    def audit(
        self, data: bytes, validated: ValidatedWorkbook, ruleset_version: str
    ) -> AuditEvaluation:
        return AuditEvaluation(
            findings=[], limitations=["x" * 5000000] if self.mode == "large" else []
        )
