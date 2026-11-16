"""Shared helpers for Dylan's Phase 5 engine evidence scripts.

`local_system` assembles the real production stack in one process: the API (in-process
ASGI client, no network listener), SQLite metadata, filesystem object storage, the
production six-rule engine and the bounded child-process runner. Nothing here is a
double. Evidence produced with it is local evidence and must be labeled as such; it is
not a hosted or deployed result.
"""

import hashlib
import json
import os
import platform
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.audit.engine import OoxmlWorkbookEngine
from app.config import SystemSettings
from app.contracts.models import Principal
from app.jobs.worker import AuditWorker
from app.main import create_app
from app.services.assembly import assemble

ROOT = Path(__file__).resolve().parents[1]
OWNER = {"Authorization": "Bearer owner"}
ADMIN = {"Authorization": "Bearer admin"}


@contextmanager
def local_system(
    state: Path, *, measured: bool = False, audit_timeout_seconds: float | None = None
) -> Iterator[TestClient]:
    """Real API/SQLite/filesystem/engine/runner stack rooted in a private directory.

    `measured` enables the isolated test interface and per-session code measurement,
    which is how the hosted tester's namespaces run. `audit_timeout_seconds` may only
    lower the production 60-second bound, to demonstrate the controlled-timeout path.
    """
    extra: dict[str, Any] = {}
    if audit_timeout_seconds is not None:
        extra["audit_timeout_seconds"] = audit_timeout_seconds
    settings = SystemSettings(
        **extra,
        _env_file=None,
        ruleset_version="2.0.0",
        metadata_db_path=state / "metadata.sqlite3",
        blob_root=state / "sources",
        enable_test_interface=measured,
        enable_code_measurement=measured,
        ui_router_factory=None,
        TEST_SESSION_USERS_JSON={"admin": ["owner"]} if measured else {},
        SYSTEM_TOKEN_HASHES_JSON={
            hashlib.sha256(name.encode()).hexdigest(): Principal(subject=name, role=role)
            for name, role in (("owner", "user"), ("admin", "test_admin"))
        },
    )
    services = assemble(settings, engine=OoxmlWorkbookEngine())
    with TestClient(create_app(services=services)) as client:
        yield client


def drain(client: TestClient) -> None:
    """Run the production worker until no claimable audit or remediation remains."""
    app: Any = client.app
    worker = AuditWorker(app.state.services)
    while worker.run_once():
        pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_revision() -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()

    return {
        "commit": run("rev-parse", "HEAD") or None,
        "branch": run("rev-parse", "--abbrev-ref", "HEAD") or None,
        # Uncommitted engine/app changes mean the evidence is not tied to the commit alone.
        "dirtyPaths": [line[3:] for line in run("status", "--porcelain").splitlines()],
    }


def environment() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "cpuCount": os.cpu_count(),
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
    }


def canonical_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings exactly as served, in served order (order is part of the contract)."""
    return [dict(finding) for finding in findings]


def findings_digest(findings: list[dict[str, Any]], limitations: list[str]) -> str:
    payload = {"findings": canonical_findings(findings), "limitations": list(limitations)}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8"))
