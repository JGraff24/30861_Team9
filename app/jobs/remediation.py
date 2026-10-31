"""Bounded calls to the trusted correction engine, never to caller-selected code.

Workbook bytes cross this private boundary as bounded files. The engine must
provide real editing and verification methods; absent integrations fail closed.
This module does not implement workbook interpretation or corrections.
"""

import json
import os
import pickle
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from app.contracts.errors import EngineExecutionError, ProjectException
from app.contracts.models import (
    AuditEvaluation,
    AuditRecord,
    ExecutionErrorKind,
    ExecutionMeasurement,
    ProcessingLimits,
    ValidatedWorkbook,
)
from app.contracts.phase4 import Decision
from app.contracts.ports import WorkbookEngine
from app.jobs.execution import MAX_IPC_BYTES
from app.measurement.runner import MAX_MEASUREMENT_BYTES

CorrectionOperation = Literal[
    "validate_decisions", "remediate", "verify_preservation", "verify_corrections"
]
CORRECTION_OPERATIONS = (
    "validate_decisions",
    "remediate",
    "verify_preservation",
    "verify_corrections",
)
PREFLIGHT_CODES = {
    "INVALID_CORRECTION",
    "UNSUPPORTED_CORRECTION",
    "CONFLICTING_CORRECTIONS",
}


@dataclass(frozen=True)
class CorrectionResult:
    candidate: bytes | None
    measurement: ExecutionMeasurement | None


def require_correction_engine(engine: WorkbookEngine | None) -> WorkbookEngine:
    if engine is None or any(
        not callable(getattr(engine, operation, None)) for operation in CORRECTION_OPERATIONS
    ):
        raise ProjectException(
            503, "SERVICE_UNAVAILABLE", "Workbook correction integration is unavailable."
        )
    return engine


def ordered_engine_decisions(audit: AuditRecord, decisions: list[Decision]) -> list[Decision]:
    indexed = {
        finding.findingId: (position, finding.ruleId)
        for position, finding in enumerate(audit.findings)
    }
    families = {"ALT_TEXT": 0, "LINK_TEXT": 1, "TABLE_HEADER": 2, "WORKSHEET_NAME": 3}

    def key(decision: Decision) -> tuple[int, int]:
        position, rule = indexed[decision.findingId]
        return (families.get(rule, 4) if decision.action == "APPLY" else 4, position)

    try:
        return sorted(decisions, key=key)
    except KeyError:
        raise ProjectException(
            500, "ENGINE_RESULT_INVALID", "A correction target is missing from the source audit."
        ) from None


def execute_correction_bounded(
    engine: WorkbookEngine,
    operation: CorrectionOperation,
    data: bytes,
    validated: ValidatedWorkbook,
    audit: AuditRecord,
    decisions: list[Decision],
    *,
    candidate: bytes | None = None,
    evaluation: AuditEvaluation | None = None,
    session_id: str | None = None,
    revision: str = "unknown",
    measure: bool = False,
    timeout_seconds: float = 30,
    memory_limit_mib: int = 512,
) -> CorrectionResult:
    require_correction_engine(engine)
    if operation not in CORRECTION_OPERATIONS:
        raise ValueError("Unknown correction operation")
    if timeout_seconds <= 0:
        raise EngineExecutionError(
            500,
            "PROCESSING_TIMEOUT",
            "Remediation exceeded its execution deadline.",
            kind=ExecutionErrorKind.TIMEOUT,
        )
    limits = ProcessingLimits()
    if len(data) > limits.max_file_bytes or (
        candidate is not None and len(candidate) > limits.max_file_bytes
    ):
        raise ProjectException(
            500, "OUTPUT_VALIDATION_FAILED", "Candidate exceeds the workbook byte limit."
        )
    invocation = uuid4().hex
    with tempfile.TemporaryDirectory(prefix="team9-correction-") as directory:
        root = Path(directory)
        (root / "source").write_bytes(data)
        (root / "source").chmod(0o400)
        if candidate is not None:
            (root / "candidate-input").write_bytes(candidate)
            (root / "candidate-input").chmod(0o400)
        try:
            (root / "engine").write_bytes(pickle.dumps(engine))
        except Exception:
            raise ProjectException(
                503, "SERVICE_UNAVAILABLE", "Correction engine cannot be isolated."
            ) from None
        request = {
            "operation": operation,
            "validated": validated.model_dump(mode="json"),
            "audit": audit.model_dump(mode="json"),
            "decisions": [
                decision.model_dump(mode="json", exclude_unset=True)
                for decision in ordered_engine_decisions(audit, decisions)
            ],
            "evaluation": evaluation.model_dump(mode="json") if evaluation else None,
            "session_id": session_id,
            "revision": revision,
            "invocation_id": invocation,
            "measure": measure,
            "memory_limit_mib": memory_limit_mib,
        }
        (root / "request.json").write_text(json.dumps(request, ensure_ascii=False))
        environment = {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "TMPDIR": directory,
        }
        with subprocess.Popen(
            [sys.executable, "-m", "app.jobs.remediation", directory],
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ) as process:
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                _kill_group(process)
                raise EngineExecutionError(
                    500,
                    "PROCESSING_TIMEOUT",
                    "Remediation exceeded its execution deadline.",
                    kind=ExecutionErrorKind.TIMEOUT,
                ) from None
            finally:
                _kill_group(process)
        result_path = root / "result.json"
        try:
            if process.returncode != 0 or result_path.stat().st_size > MAX_IPC_BYTES:
                raise ValueError("Missing or oversized correction result")
            payload = json.loads(result_path.read_bytes())
            measurement = (
                ExecutionMeasurement.model_validate(payload["measurement"])
                if payload.get("measurement")
                else None
            )
            if measurement is not None and (
                measurement.session_id != session_id
                or measurement.revision != revision
                or measurement.invocation_id != invocation
            ):
                raise ValueError("Incorrect measurement attribution")
            if "error" in payload:
                code = payload["error"]
                if operation == "validate_decisions" and code in PREFLIGHT_CODES:
                    raise EngineExecutionError(
                        422,
                        code,
                        "The selected corrections are invalid or unsupported.",
                        kind=ExecutionErrorKind.VALIDATION_REJECTED,
                        measurement=measurement,
                    )
                if code == "SERVICE_UNAVAILABLE":
                    raise ProjectException(503, code, "Correction integration is unavailable.")
                raise EngineExecutionError(
                    500,
                    "ENGINE_RESULT_INVALID"
                    if code == "ENGINE_RESULT_INVALID"
                    else "PROCESSING_FAILED",
                    "Workbook correction or verification failed.",
                    kind=ExecutionErrorKind.INVALID_RESULT
                    if code == "ENGINE_RESULT_INVALID"
                    else ExecutionErrorKind.CRASH,
                    measurement=measurement,
                )
            output = None
            if operation == "remediate":
                candidate_path = root / "candidate-output"
                if candidate_path.stat().st_size > limits.max_file_bytes:
                    raise ValueError("Oversized candidate")
                output = candidate_path.read_bytes()
            elif payload.get("success") is not True:
                raise ValueError("Missing verification outcome")
            return CorrectionResult(candidate=output, measurement=measurement)
        except ProjectException:
            raise
        except Exception:
            raise EngineExecutionError(
                500,
                "ENGINE_RESULT_INVALID",
                "Correction engine returned invalid metadata.",
                kind=ExecutionErrorKind.INVALID_RESULT,
            ) from None


def validate_decisions_bounded(
    engine: WorkbookEngine,
    data: bytes,
    validated: ValidatedWorkbook,
    audit: AuditRecord,
    decisions: list[Decision],
    *,
    timeout_seconds: float = 30,
    memory_limit_mib: int = 512,
    session_id: str | None = None,
    revision: str = "unknown",
    measure: bool = False,
) -> ExecutionMeasurement | None:
    return execute_correction_bounded(
        engine,
        "validate_decisions",
        data,
        validated,
        audit,
        decisions,
        timeout_seconds=timeout_seconds,
        memory_limit_mib=memory_limit_mib,
        session_id=session_id,
        revision=revision,
        measure=measure,
    ).measurement


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _child(directory: str) -> None:
    root = Path(directory)
    request = json.loads((root / "request.json").read_bytes())
    if sys.platform == "linux":
        import resource

        ceiling = request["memory_limit_mib"] * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_IPC_BYTES, MAX_IPC_BYTES))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    coverage: Any = None
    reason = "Code measurement disabled."
    source_root = Path(__file__).resolve().parents[2]
    scope = [source_root / "app" / name for name in ("workbooks", "audit")]
    if request["measure"]:
        try:
            from coverage import Coverage

            coverage = Coverage(
                source=[str(path) for path in scope], data_file=None, config_file=False
            )
            coverage.start()
            reason = ""
        except Exception:
            reason = "Code instrumentation unavailable."
    error = None
    try:
        engine = require_correction_engine(pickle.loads((root / "engine").read_bytes()))
        operation = request["operation"]
        if operation not in CORRECTION_OPERATIONS:
            raise ValueError("Unknown correction operation")
        data = (root / "source").read_bytes()
        validated = ValidatedWorkbook.model_validate(request["validated"])
        audit = AuditRecord.model_validate(request["audit"])
        decisions = [Decision.model_validate(decision) for decision in request["decisions"]]
        method = getattr(engine, operation)
        if operation in ("validate_decisions", "remediate"):
            outcome = method(data, validated, audit, decisions)
        else:
            candidate = (root / "candidate-input").read_bytes()
            if operation == "verify_preservation":
                outcome = method(data, candidate, validated, audit, decisions)
            else:
                evaluation = AuditEvaluation.model_validate(request["evaluation"])
                outcome = method(data, candidate, audit, decisions, evaluation)
        if operation == "remediate":
            if not isinstance(outcome, bytes) or len(outcome) > ProcessingLimits().max_file_bytes:
                raise ValueError("Invalid candidate bytes")
            (root / "candidate-output").write_bytes(outcome)
        elif outcome is not None:
            # A false boolean or a plausible-looking result is not a passed verifier.
            raise ValueError("Verification methods return None only after success")
    except ProjectException as exc:
        error = (
            exc.code
            if exc.code in PREFLIGHT_CODES | {"SERVICE_UNAVAILABLE"}
            else "PROCESSING_FAILED"
        )
    except (ValueError, TypeError):
        error = "ENGINE_RESULT_INVALID"
    except Exception:
        error = "PROCESSING_FAILED"
    executable: dict[str, list[int]] = {}
    covered: dict[str, list[int]] = {}
    if coverage is not None:
        try:
            coverage.stop()
            for directory_path in scope:
                for path in sorted(directory_path.rglob("*.py")):
                    _, statements, _, missing, _ = coverage.analysis2(str(path))
                    name = path.relative_to(source_root).as_posix()
                    executable[name] = sorted(set(statements))
                    covered[name] = sorted(set(statements) - set(missing))
        except Exception:
            executable, covered = {}, {}
            reason = "Code instrumentation unavailable."
    measurement = (
        ExecutionMeasurement(
            session_id=request["session_id"],
            invocation_id=request["invocation_id"],
            revision=request["revision"],
            executable_lines=executable,
            covered_lines=covered,
            unavailable_reason=reason or None,
        )
        if request["session_id"]
        else None
    )
    if measurement is not None and (
        len(json.dumps(measurement.model_dump(mode="json")).encode()) > MAX_MEASUREMENT_BYTES
    ):
        measurement = measurement.model_copy(
            update={
                "executable_lines": {},
                "covered_lines": {},
                "unavailable_reason": "Code measurement exceeded its IPC budget.",
            }
        )
    payload = {
        "measurement": measurement.model_dump(mode="json") if measurement else None,
        **({"error": error} if error else {"success": True}),
    }
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    if len(encoded) > MAX_IPC_BYTES:
        encoded = b'{"error":"ENGINE_RESULT_INVALID","measurement":null}'
    (root / "result.json").write_bytes(encoded)


if __name__ == "__main__":
    _child(sys.argv[1])
