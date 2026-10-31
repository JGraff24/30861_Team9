"""Serializable trusted engine configuration and bounded, fresh-interpreter execution."""

import json
import os
import pickle
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal
from uuid import uuid4

from app.contracts.errors import EngineExecutionError
from app.contracts.models import (
    ExecutionErrorKind,
    ExecutionMeasurement,
    ExecutionResult,
    ProcessingLimits,
    ValidatedWorkbook,
)
from app.contracts.ports import WorkbookEngine

MAX_RESULT_BYTES = 74 * 1024 * 1024
MAX_MEASUREMENT_BYTES = 4 * 1024 * 1024


class MeasuredEngineRunner:
    def __init__(self, engine: WorkbookEngine, enable_code_measurement: bool = False) -> None:
        # Only deployment-selected objects are serialized. No caller-selected imports/pickle.
        self.engine_configuration = pickle.dumps(engine)
        self.enable_code_measurement = enable_code_measurement

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
        invocation = uuid4().hex

        def failure(kind: ExecutionErrorKind) -> EngineExecutionError:
            return EngineExecutionError(
                500,
                "ENGINE_" + kind.value.upper(),
                "Workbook processing failed.",
                kind=kind,
                measurement=ExecutionMeasurement(
                    session_id=session_id,
                    invocation_id=invocation,
                    revision=revision,
                    executable_lines={},
                    covered_lines={},
                    unavailable_reason="Execution did not finish.",
                )
                if session_id
                else None,
            )

        if len(data) > limits.max_file_bytes:
            raise EngineExecutionError(
                413,
                "FILE_TOO_LARGE",
                "Upload exceeds the byte limit.",
                kind=ExecutionErrorKind.VALIDATION_REJECTED,
            )
        with tempfile.TemporaryDirectory(prefix="team9-measured-") as directory:
            root = Path(directory)
            # Keep private input within the inherited per-file ceiling while
            # accepting legal workbooks up to 25 MB.
            source = root / "source"
            source.mkdir()
            for offset in range(0, len(data), MAX_RESULT_BYTES):
                part = source / f"{offset:012d}"
                part.write_bytes(data[offset : offset + MAX_RESULT_BYTES])
                part.chmod(0o400)
            (root / "engine").write_bytes(self.engine_configuration)
            (root / "request.json").write_text(
                json.dumps(
                    {
                        "operation": operation,
                        "filename": filename,
                        "limits": limits.model_dump(),
                        "ruleset_version": ruleset_version,
                        "validated": validated.model_dump() if validated else None,
                        "session_id": session_id,
                        "revision": revision,
                        "invocation_id": invocation,
                        "measure": self.enable_code_measurement,
                        "memory_limit_mib": memory_limit_mib,
                    }
                )
            )
            environment = {
                "PATH": os.defpath,
                "LANG": "C.UTF-8",
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                "TMPDIR": directory,
            }
            # Inherit Jackson's group: his outer guard can kill this child and descendants.
            with subprocess.Popen(
                [sys.executable, "-m", "app.measurement.child", directory],
                cwd=directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ) as process:
                try:
                    process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise failure(ExecutionErrorKind.TIMEOUT) from None
            output = root / "result.json"
            if process.returncode != 0:
                raise failure(ExecutionErrorKind.CRASH)
            try:
                if not output.exists() or output.stat().st_size > MAX_RESULT_BYTES:
                    raise ValueError("Unbounded or missing output")
                payload = json.loads(output.read_bytes())
                measurement = (
                    ExecutionMeasurement.model_validate(payload["measurement"])
                    if payload.get("measurement")
                    else None
                )
                if session_id and (
                    measurement is None
                    or measurement.session_id != session_id
                    or measurement.revision != revision
                    or measurement.invocation_id != invocation
                ):
                    raise ValueError("Incorrect attribution")
                if "error" in payload:
                    error = payload["error"]
                    kind = ExecutionErrorKind(error["kind"])
                    if kind == ExecutionErrorKind.VALIDATION_REJECTED:
                        if operation != "validate" or error["status_code"] not in (
                            400,
                            413,
                            415,
                            422,
                        ):
                            raise ValueError("Invalid rejection")
                        raise EngineExecutionError(
                            error["status_code"],
                            error["code"],
                            error["message"],
                            error["details"],
                            kind=kind,
                            measurement=measurement,
                        )
                    raise EngineExecutionError(
                        500,
                        "ENGINE_" + kind.value.upper(),
                        "Workbook processing failed.",
                        kind=kind,
                        measurement=measurement,
                    )
                result = ExecutionResult.model_validate(payload["result"])
                if result.operation != operation or result.measurement != measurement:
                    raise ValueError("Incorrect result")
                return result
            except EngineExecutionError:
                raise
            except Exception:
                raise failure(ExecutionErrorKind.INVALID_RESULT) from None
