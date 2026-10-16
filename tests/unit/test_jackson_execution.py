"""Real child deadline/secret-isolation/memory checks, not mocked timeout outcomes."""

import sys
import time

import pytest

from app.contracts.errors import EngineExecutionError
from app.contracts.models import ProcessingLimits
from app.jobs.execution import execute_bounded
from tests.unit.jackson_support import TestEngine, TestRunner


def test_child_does_not_inherit_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AWS_ACCESS_KEY_ID", "PRIVATE_TEST_SECRET", "SYSTEM_TOKEN_HASHES_JSON"):
        monkeypatch.setenv(name, "private-never-forward")
    result = execute_bounded(
        TestRunner("secrets"),
        "validate",
        b"data",
        "file.xlsx",
        ProcessingLimits(),
        "1.0.0",
        None,
        None,
        "unknown",
        3,
        512,
    )
    assert result.validated is not None


def test_real_parent_kill_of_stalled_runner() -> None:
    started = time.monotonic()
    with pytest.raises(EngineExecutionError) as failure:
        execute_bounded(
            TestRunner("validation_hang"),
            "validate",
            b"data",
            "file.xlsx",
            ProcessingLimits(),
            "1.0.0",
            None,
            None,
            "unknown",
            0.3,
            512,
        )
    assert failure.value.kind == "timeout"
    assert time.monotonic() - started < 3


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS enforcement requires Linux")
def test_linux_memory_cap_is_enforced() -> None:
    data = b"data"
    validated = TestEngine().validate(data, "file.xlsx", ProcessingLimits())
    with pytest.raises(EngineExecutionError):
        execute_bounded(
            TestRunner("memory"),
            "audit",
            data,
            "file.xlsx",
            ProcessingLimits(),
            "1.0.0",
            validated,
            None,
            "unknown",
            5,
            256,
        )
