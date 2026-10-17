"""Parent-enforced deadlines around the configured Ryan EngineRunner port.

IPC pickles only the trusted, server-constructed runner, never an HTTP-selected
object. Workbook input and output are bounded files, output is validated JSON.
"""

import json
import os
import pickle
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

from app.contracts.errors import EngineExecutionError
from app.contracts.models import (
    ExecutionErrorKind,
    ExecutionMeasurement,
    ExecutionResult,
    ProcessingLimits,
    ValidatedWorkbook,
)
from app.contracts.ports import EngineRunner

MAX_IPC_BYTES = 4 * 1024 * 1024


def execute_bounded(
    runner: EngineRunner,
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
    if len(data) > limits.max_file_bytes:
        raise EngineExecutionError(
            413,
            "FILE_TOO_LARGE",
            "Upload exceeds the byte limit.",
            kind=ExecutionErrorKind.VALIDATION_REJECTED,
        )
    with tempfile.TemporaryDirectory(prefix="team9-execution-") as directory:
        root = Path(directory)
        (root / "source").write_bytes(data)
        (root / "source").chmod(0o400)
        # Protocol objects must be serializable configuration; no live DB/network handles.
        try:
            (root / "runner").write_bytes(pickle.dumps(runner))
        except Exception as exc:
            raise EngineExecutionError(
                503,
                "RUNNER_CONFIGURATION_INVALID",
                "Configured runner cannot be isolated.",
                kind=ExecutionErrorKind.INVALID_RESULT,
            ) from exc
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
                    "timeout_seconds": timeout_seconds,
                    "memory_limit_mib": memory_limit_mib,
                }
            )
        )
        # Deliberate allowlist: no auth, AWS credential, proxy or dotenv inputs.
        environment = {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "TMPDIR": directory,
        }
        with subprocess.Popen(
            [sys.executable, "-m", "app.jobs._guard", directory],
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ) as process:
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise EngineExecutionError(
                    500,
                    "AUDIT_TIMEOUT" if operation == "audit" else "VALIDATION_TIMEOUT",
                    "Workbook processing exceeded its deadline.",
                    kind=ExecutionErrorKind.TIMEOUT,
                ) from exc
            finally:
                # Remove descendants even if a broken runner returned while a child survived.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        output = root / "result.json"
        if process.returncode != 0 or not output.exists() or output.stat().st_size > MAX_IPC_BYTES:
            raise EngineExecutionError(
                500, "ENGINE_CRASH", "Workbook processing failed.", kind=ExecutionErrorKind.CRASH
            )
        try:
            payload = json.loads(output.read_text())
            if "result" in payload:
                result = ExecutionResult.model_validate(payload["result"])
                if result.operation != operation:
                    raise ValueError("wrong operation")
                return result
            failure = payload["error"]
            measurement = (
                ExecutionMeasurement.model_validate(failure["measurement"])
                if failure.get("measurement")
                else None
            )
            kind = ExecutionErrorKind(failure["kind"])
            # Only validation rejections may carry engine-selected public detail.
            if operation == "validate" and kind == ExecutionErrorKind.VALIDATION_REJECTED:
                if failure["status_code"] not in (400, 413, 415, 422):
                    raise ValueError("Invalid validation rejection status")
                raise EngineExecutionError(
                    failure["status_code"],
                    failure["code"],
                    failure["message"],
                    failure["details"],
                    kind=kind,
                    measurement=measurement,
                )
            raise EngineExecutionError(
                500,
                "AUDIT_TIMEOUT"
                if kind == ExecutionErrorKind.TIMEOUT
                else "ENGINE_EXECUTION_FAILED",
                "Workbook processing failed.",
                kind=kind,
                measurement=measurement,
            )
        except EngineExecutionError:
            raise
        except Exception as exc:
            raise EngineExecutionError(
                500,
                "ENGINE_RESULT_INVALID",
                "Workbook processor returned invalid metadata.",
                kind=ExecutionErrorKind.INVALID_RESULT,
            ) from exc
