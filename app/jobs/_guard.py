"""Private child entry point; accepts only files written by execute_bounded."""

import json
import pickle
import sys
from pathlib import Path

from app.contracts.errors import EngineExecutionError
from app.contracts.models import ProcessingLimits, ValidatedWorkbook
from app.jobs.execution import MAX_IPC_BYTES


def main() -> None:
    root = Path(sys.argv[1])
    request = json.loads((root / "request.json").read_text())
    if sys.platform == "linux":
        import resource

        memory = request["memory_limit_mib"] * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_IPC_BYTES, MAX_IPC_BYTES))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    runner = pickle.loads((root / "runner").read_bytes())  # trusted local configuration only
    request["limits"] = ProcessingLimits.model_validate(request["limits"])
    request["validated"] = (
        ValidatedWorkbook.model_validate(request["validated"]) if request["validated"] else None
    )
    try:
        result = runner.execute(data=(root / "source").read_bytes(), **request)
        payload = {"result": result.model_dump(mode="json")}
    except EngineExecutionError as exc:
        payload = {
            "error": {
                "status_code": exc.status_code,
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
                "kind": exc.kind,
                "measurement": exc.measurement.model_dump(mode="json") if exc.measurement else None,
            }
        }
    except Exception:
        payload = {
            "error": {
                "status_code": 500,
                "code": "ENGINE_CRASH",
                "message": "Processing failed.",
                "details": {},
                "kind": "crash",
                "measurement": None,
            }
        }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_IPC_BYTES:
        raise ValueError("Execution output exceeds IPC limit")
    (root / "result.json").write_bytes(encoded)


if __name__ == "__main__":
    main()
