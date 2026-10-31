"""Private child entry point. Never accepts engine configuration from HTTP."""

import json
import pickle
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.contracts.errors import WorkbookValidationError
from app.contracts.models import (
    AuditEvaluation,
    ExecutionMeasurement,
    ExecutionResult,
    ProcessingLimits,
    ValidatedWorkbook,
)
from app.measurement.runner import MAX_MEASUREMENT_BYTES, MAX_RESULT_BYTES


def main(directory: str) -> None:
    root = Path(directory)
    request = json.loads((root / "request.json").read_text())
    if sys.platform == "linux":
        import resource

        ceiling = request["memory_limit_mib"] * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RESULT_BYTES, MAX_RESULT_BYTES))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    source_root = Path(__file__).resolve().parents[2]
    scope = [source_root / "app" / name for name in ("workbooks", "audit")]
    coverage: Any = None
    reason = "Code measurement disabled."
    if request["measure"]:
        try:
            from coverage import Coverage

            coverage = Coverage(
                source=[str(path) for path in scope], data_file=None, config_file=False
            )
            coverage.start()
            reason = ""
        except Exception:
            coverage = None
            reason = "Code instrumentation unavailable."
    result: ExecutionResult | None = None
    error: dict[str, Any] | None = None
    try:
        engine = pickle.loads((root / "engine").read_bytes())
        data = b"".join(part.read_bytes() for part in sorted((root / "source").iterdir()))
        if request["operation"] == "validate":
            validated = ValidatedWorkbook.model_validate(
                engine.validate(
                    data, request["filename"], ProcessingLimits.model_validate(request["limits"])
                )
            )
            result = ExecutionResult(
                operation="validate", validated=validated, evaluation=None, measurement=None
            )
        elif request["operation"] == "audit":
            evaluation = AuditEvaluation.model_validate(
                engine.audit(
                    data,
                    ValidatedWorkbook.model_validate(request["validated"]),
                    request["ruleset_version"],
                )
            )
            result = ExecutionResult(
                operation="audit", validated=None, evaluation=evaluation, measurement=None
            )
        else:
            raise ValueError("Invalid operation")
    except WorkbookValidationError as exc:
        error = {
            "kind": "validation_rejected",
            "status_code": exc.status_code,
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        }
    except ValidationError:
        error = {"kind": "invalid_result"}
    except Exception:
        error = {"kind": "crash"}
    executable: dict[str, list[int]] = {}
    covered: dict[str, list[int]] = {}
    if coverage is not None:
        try:
            coverage.stop()
            # Analyze every deployed source, including files never imported by this request.
            for directory_path in scope:
                for path in sorted(directory_path.rglob("*.py")):
                    name = path.relative_to(source_root).as_posix()
                    _, statements, _, missing, _ = coverage.analysis2(str(path))
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
    payload: dict[str, Any] = {"measurement": measurement.model_dump() if measurement else None}
    if error:
        payload["error"] = error
    else:
        assert result is not None
        payload["result"] = result.model_copy(update={"measurement": measurement}).model_dump()
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if measurement is not None and (
        len(encoded) > MAX_RESULT_BYTES
        or len(json.dumps(measurement.model_dump()).encode()) > MAX_MEASUREMENT_BYTES
    ):
        # Optional instrumentation must not turn a bounded engine result into failure.
        measurement = measurement.model_copy(
            update={
                "executable_lines": {},
                "covered_lines": {},
                "unavailable_reason": "Code measurement exceeded the IPC limit.",
            }
        )
        payload["measurement"] = measurement.model_dump()
        if result is not None:
            payload["result"] = result.model_copy(update={"measurement": measurement}).model_dump()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_RESULT_BYTES:
        encoded = b'{"error":{"kind":"invalid_result"}}'
    (root / "result.json").write_bytes(encoded)


if __name__ == "__main__":
    main(sys.argv[1])
