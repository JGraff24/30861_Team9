import pickle
import sys
from pathlib import Path

import pytest

from app.contracts.errors import EngineExecutionError
from app.contracts.models import ProcessingLimits
from app.measurement.runner import MeasuredEngineRunner
from tests.ryan.engines import Engine


def execute(mode="success", session="one", enabled=True, timeout=5):
    runner = pickle.loads(pickle.dumps(MeasuredEngineRunner(Engine(mode), enabled)))
    return runner.execute(
        "validate",
        b"data",
        "hostile.xlsx",
        ProcessingLimits(),
        "1.0.0",
        None,
        session,
        "revision-a",
        timeout,
        512,
    )


def test_real_children_attribution_and_fixed_scope():
    first, second = execute(), execute(session="two")
    assert first.validated.source_sha256 == second.validated.source_sha256
    assert first.measurement.session_id == "one"
    assert second.measurement.session_id == "two"
    assert first.measurement.invocation_id != second.measurement.invocation_id
    assert first.measurement.revision == "revision-a"
    assert first.measurement.executable_lines == second.measurement.executable_lines
    root = Path(__file__).resolve().parents[2]
    expected = {
        path.relative_to(root).as_posix()
        for name in ("audit", "workbooks")
        for path in (root / "app" / name).rglob("*.py")
    }
    assert set(first.measurement.executable_lines) == expected
    assert first.measurement.unavailable_reason is None


def test_disabled_and_non_session():
    assert execute(enabled=False).measurement.unavailable_reason
    assert execute(session=None).measurement is None


@pytest.mark.parametrize(
    "mode,kind",
    [
        ("crash", "crash"),
        ("timeout", "timeout"),
        ("invalid", "invalid_result"),
        ("bad_ipc", "invalid_result"),
        pytest.param(
            "memory",
            "crash",
            marks=pytest.mark.skipif(
                sys.platform != "linux", reason="RLIMIT_AS enforcement requires Linux"
            ),
        ),
    ],
)
def test_failures(mode, kind):
    with pytest.raises(EngineExecutionError) as error:
        execute(mode, timeout=0.2 if mode == "timeout" else 5)
    assert error.value.kind == kind
    assert "Traceback" not in error.value.message


def test_rejection_and_environment(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-cross")
    execute("environment")
    with pytest.raises(EngineExecutionError) as error:
        execute("reject")
    assert error.value.status_code == 422
    assert error.value.details == {"safe": True}
    assert error.value.measurement.session_id == "one"


def test_large_output():
    validated = execute().validated
    runner = MeasuredEngineRunner(Engine("large"))
    result = runner.execute(
        "audit",
        b"data",
        "x.xlsx",
        ProcessingLimits(),
        "2.0.0",
        validated,
        "one",
        "revision-a",
        10,
        512,
    )
    assert len(result.evaluation.limitations[0]) == 5_000_000


def test_output_exceeding_phase4_ipc_budget_is_rejected():
    validated = execute().validated
    runner = MeasuredEngineRunner(Engine("oversized"))
    with pytest.raises(EngineExecutionError):
        runner.execute(
            "audit",
            b"data",
            "x.xlsx",
            ProcessingLimits(),
            "1.0.0",
            validated,
            "one",
            "revision-a",
            10,
            1024,
        )


@pytest.mark.parametrize("instrumentation", ["available", "missing", "oversized"])
def test_deployment_scope_and_unavailable_instrumentation(tmp_path, instrumentation):
    import json
    import os
    import shutil
    import subprocess
    import sys

    source = Path(__file__).resolve().parents[2]
    shutil.copytree(source / "app", tmp_path / "app", ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "app/audit/probe.py").write_text("""
from app.contracts.models import ValidatedWorkbook
class Engine:
    def __init__(self, count):
        self.count = count
    def validate(self, data, filename, limits):
        if data == b"branch":
            count = self.count
        else:
            count = 0
        return ValidatedWorkbook(source_sha256="a" * 64, size_bytes=len(data),
            worksheet_count=count, nonempty_cell_count=0, worksheet_parts={})
""")
    (tmp_path / "app/workbooks/unused.py").write_text("def unused():\n    return 42\n")
    if instrumentation == "missing":
        (tmp_path / "coverage.py").write_text("raise ImportError('unavailable in deployment')\n")
    elif instrumentation == "oversized":
        (tmp_path / "coverage.py").write_text("""
class Coverage:
    def __init__(self, **kwargs): pass
    def start(self): pass
    def stop(self): pass
    def analysis2(self, filename):
        return filename, list(range(1, 100001)), [], [], ""
""")
    driver = """
import json
from app.audit.probe import Engine
from app.measurement.runner import MeasuredEngineRunner
from app.contracts.models import ProcessingLimits
runner = MeasuredEngineRunner(Engine(7), True)
results = [runner.execute("validate", data, "x.xlsx", ProcessingLimits(), "1.0.0",
    None, sid, "revision-probe", 5, 512).model_dump() for data, sid in
    [(b"branch", "a"), (b"other", "b")]]
print(json.dumps(results))
"""
    run = subprocess.run(
        [sys.executable, "-c", driver],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    first, second = json.loads(run.stdout)
    assert first["validated"]["worksheet_count"] == 7
    assert second["validated"]["worksheet_count"] == 0
    a, b = first["measurement"], second["measurement"]
    if instrumentation != "available":
        assert a["unavailable_reason"]
        assert a["executable_lines"] == {}
    else:
        assert a["executable_lines"] == b["executable_lines"]
        assert a["covered_lines"] != b["covered_lines"]
        assert a["executable_lines"]["app/workbooks/unused.py"] == [1, 2]
        assert a["covered_lines"]["app/workbooks/unused.py"] == []


def test_inherited_file_ceiling_large_input():
    import subprocess
    import sys

    driver = """
import resource
from tests.ryan.engines import Engine
from app.measurement.runner import MeasuredEngineRunner, MAX_RESULT_BYTES
from app.contracts.models import ProcessingLimits
resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_RESULT_BYTES, MAX_RESULT_BYTES))
result = MeasuredEngineRunner(Engine()).execute("validate", b"x" * 25000000,
    "x.xlsx", ProcessingLimits(), "1.0.0", None, "large", "rev", 10, 512)
assert result.validated.size_bytes == 25000000
"""
    subprocess.run([sys.executable, "-c", driver], check=True, timeout=20)


def test_temporary_files_removed_on_timeout(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with pytest.raises(EngineExecutionError):
        execute("timeout", timeout=0.2)
    assert list(tmp_path.iterdir()) == []
