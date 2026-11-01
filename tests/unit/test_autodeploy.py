"""Deployment gates and rollback boundaries; no AWS, GitHub or Docker access."""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import deploy_host, deploy_poll

SHA = "a" * 40
OLD = "b" * 40
REPOSITORY = "JGraff24/30861_Team9"


def ci_run(**changes: Any) -> dict[str, Any]:
    return {
        "id": 1,
        "run_attempt": 1,
        "head_sha": SHA,
        "head_branch": "main",
        "event": "push",
        "head_repository": {"full_name": REPOSITORY},
        "status": "completed",
        "conclusion": "success",
        **changes,
    }


def github_responses(monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]]) -> None:
    def response(path: str) -> dict[str, Any]:
        assert "actions/workflows/ci.yml/runs?" in path
        assert "event=push" in path and "head_sha=" + SHA in path
        return {"workflow_runs": runs}

    monkeypatch.setattr(deploy_poll, "github", response)


def test_exact_successful_main_ci_is_eligible(monkeypatch: pytest.MonkeyPatch) -> None:
    github_responses(monkeypatch, [ci_run()])
    assert deploy_poll.eligible(REPOSITORY, SHA)


@pytest.mark.parametrize(
    "changes",
    [
        {"conclusion": "failure"},
        {"status": "in_progress", "conclusion": None},
        {"event": "pull_request"},
        {"head_branch": "feature"},
        {"head_sha": OLD},
        {"head_repository": {"full_name": "other/repository"}},
    ],
)
def test_failed_or_untrusted_ci_cannot_deploy(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    github_responses(monkeypatch, [ci_run(**changes)])
    assert not deploy_poll.eligible(REPOSITORY, SHA)


def test_failed_rerun_overrides_earlier_success(monkeypatch: pytest.MonkeyPatch) -> None:
    github_responses(monkeypatch, [ci_run(), ci_run(run_attempt=2, conclusion="failure")])
    assert not deploy_poll.eligible(REPOSITORY, SHA)


def test_missing_ci_does_not_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    github_responses(monkeypatch, [])
    assert not deploy_poll.eligible(REPOSITORY, SHA)


def test_release_env_preserves_all_other_inputs() -> None:
    text = (
        "# keep\nTEAM9_IMAGE='old'\nDEPLOYMENT_SHA='old'\nTEAM9_ENV_FILE='/private/runtime.env'\n"
    )
    updated = deploy_host.release_env(text, SHA)
    assert "TEAM9_IMAGE='team9-phase3:" + SHA + "'" in updated
    assert "DEPLOYMENT_SHA='" + SHA + "'" in updated
    assert updated.endswith("TEAM9_ENV_FILE='/private/runtime.env'\n")


class Host(deploy_host.Deployment):
    def __init__(self, root: Path) -> None:
        super().__init__(root, REPOSITORY, SHA)
        self.events: list[tuple[Path, str, tuple[str, ...]]] = []
        self.healthy: list[str] = []
        self.fail_build = False
        self.fail_health = False
        self.fail_private = False
        self.private_healthy: list[str] = []
        self.timeline: list[str] = []
        self.current = [True, True, True, True]

    def run(self, *command: str, capture: bool = False, timeout: int = 900) -> str:
        assert command[:3] == ("docker", "image", "inspect")
        return "old-image-id"

    def is_current(self) -> bool:
        return self.current.pop(0)

    def prepare_release(self, settings: dict[str, str]) -> None:
        if self.fail_build:
            raise RuntimeError("Build failed")
        (self.release / "deploy").mkdir(parents=True)

    def compose(self, directory: Path, env: Path, *args: str, capture: bool = False) -> str:
        image = deploy_host.read_env(env)["TEAM9_IMAGE"]
        self.events.append((directory, image, args))
        self.timeline.append(" ".join(args))
        return ""

    def backup(self, data: Path, original: str, previous: Path) -> Path:
        self.timeline.append("backup")
        return super().backup(data, original, previous)

    def wait_private_healthy(self, directory: Path, image: str) -> None:
        self.private_healthy.append(image)
        self.timeline.append("private checks")
        if self.fail_private:
            raise RuntimeError("New image is privately unhealthy")

    def wait_healthy(
        self, directory: Path, image: str, hostname: str, expected_ruleset: str | None = None
    ) -> None:
        self.healthy.append(image)
        if self.fail_health and image == "team9-phase3:" + SHA:
            raise RuntimeError("New image is unhealthy")


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    (tmp_path / "config").mkdir()
    (tmp_path / "repo/deploy").mkdir(parents=True)
    (tmp_path / "repo/deploy/compose.production.yaml").write_text("name: team9-production\n")
    data = tmp_path / "data"
    for folder, table, columns in (
        ("system", "jobs", "state TEXT"),
        ("tester", "tester_sessions", "state TEXT"),
    ):
        directory = data / folder
        directory.mkdir(parents=True)
        filename = "system.sqlite3" if folder == "system" else "tester.sqlite3"
        with sqlite3.connect(directory / filename) as database:
            database.execute(f"CREATE TABLE {table} ({columns})")
    (tmp_path / "config/compose.env").write_text(
        "TEAM9_IMAGE='team9-phase3:" + OLD + "'\nDEPLOYMENT_SHA='" + OLD + "'\n"
        "EBS_DATA_DIR='" + str(data) + "'\nSERVER_NAME='demo.example.org'\n"
        "TEAM9_ENV_FILE='/opt/team9/config/runtime.env'\n"
    )
    (tmp_path / "config/runtime.env").write_text("PRIVATE_UNCHANGED=yes\n")
    monkeypatch.setattr(deploy_host.os.path, "ismount", lambda path: path == data)
    monkeypatch.setattr(
        deploy_host.shutil, "disk_usage", lambda path: type("Usage", (), {"free": 4 * 1024**3})()
    )
    return Host(tmp_path)


def test_success_updates_all_services_and_keeps_sqlite_backups(host: Host) -> None:
    host.update()
    assert deploy_host.read_env(host.config)["DEPLOYMENT_SHA"] == SHA
    state = json.loads(host.state.read_text())
    assert state["sha"] == SHA and state["previous_image"] == "team9-phase3:" + OLD
    assert host.healthy == ["team9-phase3:" + SHA]
    starts = [event for event in host.events if event[2][0] == "up"]
    assert len(starts) == 2 and "--force-recreate" in starts[0][2]
    assert starts[0][2][-3:] == ("system", "worker", "tester")
    assert starts[1][2][-1] == "proxy"
    assert host.private_healthy == ["team9-phase3:" + SHA]
    assert host.timeline.index("stop --timeout 1900 proxy") < host.timeline.index("backup")
    assert host.timeline.index("stop --timeout 90 system worker tester") < host.timeline.index(
        "backup"
    )
    assert host.timeline.index("backup") < host.timeline.index("private checks")
    assert not host.publication.exists()
    assert (Path(state["backup"]) / "system.sqlite3").is_file()
    assert (Path(state["backup"]) / "tester.sqlite3").is_file()
    assert host.config.stat().st_mode & 0o777 == 0o600
    assert (host.root / "config/runtime.env").read_text() == "PRIVATE_UNCHANGED=yes\n"


def test_failed_build_does_not_stop_production(host: Host) -> None:
    original = host.config.read_bytes()
    host.fail_build = True
    with pytest.raises(RuntimeError, match="Build failed"):
        host.update()
    assert host.config.read_bytes() == original
    assert not host.events


def test_failed_private_health_restores_previous_config_and_image(host: Host) -> None:
    original = host.config.read_bytes()
    host.fail_private = True
    with pytest.raises(RuntimeError, match="unhealthy"):
        host.update()
    assert host.config.read_bytes() == original
    assert host.private_healthy == ["team9-phase3:" + SHA]
    assert host.healthy == ["team9-phase3:" + OLD]
    assert not host.state.exists()
    assert host.events[-1][1] == "team9-phase3:" + OLD
    assert not list(host.config.parent.glob("candidate-*.env"))


def test_failed_next_release_preserves_existing_release_record(host: Host) -> None:
    previous = host.root / "releases" / OLD
    (previous / "deploy").mkdir(parents=True)
    (previous / "deploy/compose.production.yaml").touch()
    original = json.dumps({"sha": OLD, "directory": str(previous)})
    host.state.write_text(original)
    host.fail_private = True
    with pytest.raises(RuntimeError):
        host.update()
    assert host.state.read_text() == original
    assert host.events[-1][0] == previous


def test_main_advancing_during_build_leaves_production_running(host: Host) -> None:
    host.current = [True, True, False]
    original = host.config.read_bytes()
    host.update()
    assert host.config.read_bytes() == original
    assert all(event[2][0] not in {"up", "stop"} for event in host.events)


def test_main_advancing_while_waiting_leaves_production_running(host: Host) -> None:
    host.current = [True, True, True, False]
    original = host.config.read_bytes()
    host.update()
    assert host.config.read_bytes() == original
    assert all(event[2][0] not in {"up", "stop"} for event in host.events)


def test_missing_mount_blocks_deployment(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy_host.os.path, "ismount", lambda path: False)
    with pytest.raises(RuntimeError, match="must be mounted"):
        host.update()
    assert not host.events


def test_busy_tester_defers_deployment_without_stopping_services(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = Path(deploy_host.read_env(host.config)["EBS_DATA_DIR"])
    with sqlite3.connect(data / "tester/tester.sqlite3") as database:
        database.execute("INSERT INTO tester_sessions VALUES ('RUNNING')")
    clock = iter([0.0, 1901.0])
    monkeypatch.setattr(deploy_host.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="Active audit/tester work"):
        host.update()
    assert all(event[2][0] != "stop" for event in host.events)


def test_host_script_parses_on_amazon_linux_python39() -> None:
    ast.parse(Path(deploy_host.__file__).read_text(), feature_version=(3, 9))
    ast.parse(Path(deploy_poll.__file__).read_text(), feature_version=(3, 9))


@pytest.fixture
def poller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "automation").mkdir()
    (tmp_path / "config/compose.env").write_text("DEPLOYMENT_SHA='" + OLD + "'\n")
    (tmp_path / "config/runtime.env").write_text("UNCHANGED=yes\n")
    monkeypatch.setattr(
        deploy_poll.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, SHA + "\trefs/heads/main\n"),
    )
    monkeypatch.setattr(deploy_poll, "eligible", lambda repository, sha: True)
    from io import BytesIO

    monkeypatch.setattr(
        deploy_poll, "urlopen", lambda *a, **kw: BytesIO(b"# tested deployment script\n")
    )
    return tmp_path


def test_current_release_needs_no_github_api(poller: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (poller / "config/compose.env").write_text("DEPLOYMENT_SHA='" + SHA + "'\n")
    monkeypatch.setattr(deploy_poll, "eligible", lambda *a: pytest.fail("No API call needed"))
    deploy_poll.poll(poller, REPOSITORY)


def test_poller_waits_for_ci_without_executing(
    poller: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy_poll, "eligible", lambda *a: False)
    monkeypatch.setattr(deploy_poll, "execute", lambda *a: pytest.fail("CI must pass first"))
    deploy_poll.poll(poller, REPOSITORY)


def test_poller_pins_source_and_updates_bootstrap_after_readiness(
    poller: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from io import BytesIO

    def source(request: Any, timeout: int) -> BytesIO:
        assert request.full_url.endswith("/" + SHA + "/scripts/deploy_host.py")
        assert not request.has_header("Authorization")
        return BytesIO(b"# tested deployment script\n")

    def execute(candidate: Path, sha: str, repository: str) -> int:
        assert candidate.read_text() == "# tested deployment script\n"
        assert sha == SHA and repository == REPOSITORY
        assert candidate.stat().st_mode & 0o777 == 0o600
        (poller / "config/deployment-state.json").write_text(json.dumps({"sha": SHA}))
        updated = poller / "releases" / SHA / "scripts/deploy_poll.py"
        updated.parent.mkdir(parents=True)
        updated.write_text("# updated polling bootstrap\n")
        return 0

    monkeypatch.setattr(deploy_poll, "urlopen", source)
    monkeypatch.setattr(deploy_poll, "execute", execute)
    deploy_poll.poll(poller, REPOSITORY)
    assert (poller / "automation/deploy_poll.py").read_text() == "# updated polling bootstrap\n"
    assert not list((poller / "config").glob("deploy-*.py"))
    assert (poller / "config/runtime.env").read_text() == "UNCHANGED=yes\n"


def test_failed_release_is_not_repeated_until_retry(
    poller: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def execute(*args: Any) -> int:
        calls.append(args)
        return 1

    monkeypatch.setattr(deploy_poll, "execute", execute)
    with pytest.raises(RuntimeError, match="Deployment failed"):
        deploy_poll.poll(poller, REPOSITORY)
    deploy_poll.poll(poller, REPOSITORY)
    assert len(calls) == 1
    with pytest.raises(RuntimeError):
        deploy_poll.poll(poller, REPOSITORY, retry=True)
    assert len(calls) == 2


def test_busy_host_is_retried_without_failed_release_marker(
    poller: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy_poll, "execute", lambda *a: 75)
    deploy_poll.poll(poller, REPOSITORY)
    assert not (poller / "config/deploy-failed.json").exists()


def test_api_failure_never_executes_a_deployment(
    poller: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: Any) -> bool:
        raise OSError("GitHub unavailable")

    monkeypatch.setattr(deploy_poll, "eligible", unavailable)
    monkeypatch.setattr(
        deploy_poll, "execute", lambda *a: pytest.fail("No deployment on API failure")
    )
    with pytest.raises(OSError):
        deploy_poll.poll(poller, REPOSITORY)
    assert not (poller / "config/deploy-failed.json").exists()


@pytest.mark.parametrize("force_kill", [False, True])
def test_deployment_timeout_allows_graceful_recovery_before_killing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force_kill: bool
) -> None:
    events = []

    class Process:
        def __enter__(self) -> Process:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def wait(self, timeout: int | None = None) -> int:
            events.append(("wait", timeout))
            if timeout == 2400 or (timeout == 180 and force_kill):
                raise subprocess.TimeoutExpired("deployer", timeout)
            return 1

        def terminate(self) -> None:
            events.append(("terminate", None))

        def kill(self) -> None:
            events.append(("kill", None))

    monkeypatch.setattr(deploy_poll.subprocess, "Popen", lambda *a, **kw: Process())
    assert deploy_poll.execute(tmp_path / "candidate.py", SHA, REPOSITORY) == 124
    assert events[:3] == [("wait", 2400), ("terminate", None), ("wait", 180)]
    assert (("kill", None) in events) == force_kill


@pytest.mark.parametrize("operation", ["remediation", "upload"])
def test_phase4_active_work_defers_deployment_without_stopping_services(
    host: Host, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    data = Path(deploy_host.read_env(host.config)["EBS_DATA_DIR"])
    with sqlite3.connect(data / "system/system.sqlite3") as database:
        if operation == "remediation":
            database.execute("CREATE TABLE remediation_jobs(state TEXT)")
            database.execute("INSERT INTO remediation_jobs VALUES ('RUNNING')")
        else:
            database.execute("CREATE TABLE upload_staging(deadline TEXT)")
            database.execute("INSERT INTO upload_staging VALUES ('2999-01-01T00:00:00+00:00')")
    monkeypatch.setattr(
        host, "backup", lambda *args: pytest.fail("backup began during active Phase 4 work")
    )
    clock = iter([0.0, 1901.0])
    monkeypatch.setattr(deploy_host.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="Active audit/tester work"):
        host.update()
    assert all(event[2][0] not in {"up", "stop"} for event in host.events)


def test_public_health_failure_keeps_new_release_and_data_for_safe_retry(host: Host) -> None:
    host.fail_health = True
    with pytest.raises(RuntimeError, match="unhealthy"):
        host.update()
    assert deploy_host.read_env(host.config)["DEPLOYMENT_SHA"] == SHA
    assert host.publication.exists()
    assert not host.state.exists()
    assert host.healthy == ["team9-phase3:" + SHA]
    assert all(image != "team9-phase3:" + OLD for _, image, args in host.events if args[0] == "up")
    # A post-publication acceptance must survive an explicit recovery attempt.
    data = Path(deploy_host.read_env(host.config)["EBS_DATA_DIR"])
    with sqlite3.connect(data / "system/system.sqlite3") as db:
        db.execute("CREATE TABLE accepted_after_publication(id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO accepted_after_publication VALUES ('retained')")
        db.execute("PRAGMA user_version=2")
    host.fail_health = False
    host.current = [True]
    host.events.clear()
    host.update()
    assert not host.publication.exists()
    assert json.loads(host.state.read_text())["sha"] == SHA
    with sqlite3.connect(data / "system/system.sqlite3") as db:
        assert db.execute("SELECT id FROM accepted_after_publication").fetchall() == [("retained",)]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert all("--force-recreate" not in args for _, _, args in host.events)
    assert all(args[0] not in {"stop"} for _, _, args in host.events)


def test_acceptance_racing_the_first_drain_defers_before_backup_or_migration(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = Path(deploy_host.read_env(host.config)["EBS_DATA_DIR"])
    compose = host.compose

    def stop_old_writers(directory: Path, env: Path, *args: str, capture: bool = False) -> str:
        result = compose(directory, env, *args, capture=capture)
        if args == ("stop", "--timeout", "90", "system", "worker", "tester"):
            # Simulate an upload finishing validation as graceful API shutdown ends.
            with sqlite3.connect(data / "system/system.sqlite3") as db:
                db.execute("INSERT INTO jobs VALUES ('QUEUED')")
        return result

    monkeypatch.setattr(host, "compose", stop_old_writers)
    with pytest.raises(TimeoutError, match="Work arrived during quiescence"):
        host.update()
    assert "backup" not in host.timeline
    assert not host.private_healthy
    assert not host.publication.exists()
    assert deploy_host.read_env(host.config)["DEPLOYMENT_SHA"] == OLD
    with sqlite3.connect(data / "system/system.sqlite3") as db:
        assert db.execute("SELECT state FROM jobs").fetchall() == [("QUEUED",)]
    assert host.events[-1][1] == "team9-phase3:" + OLD


def test_failed_marker_precedes_current_compose_sha_and_retry_still_requires_exact_ci(
    poller: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (poller / "config/compose.env").write_text("DEPLOYMENT_SHA='" + SHA + "'\n")
    (poller / "config/deploy-failed.json").write_text(json.dumps({"sha": SHA}))
    calls: list[str] = []
    monkeypatch.setattr(deploy_poll, "eligible", lambda *args: calls.append("ci") or False)
    monkeypatch.setattr(deploy_poll, "execute", lambda *args: pytest.fail("CI is not successful"))
    deploy_poll.poll(poller, REPOSITORY)
    assert calls == []
    deploy_poll.poll(poller, REPOSITORY, retry=True)
    assert calls == ["ci"]


def test_unfinished_publication_cannot_be_reported_as_current(
    poller: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (poller / "config/compose.env").write_text("DEPLOYMENT_SHA='" + SHA + "'\n")
    (poller / "config/publication-state.json").write_text(json.dumps({"sha": SHA}))
    monkeypatch.setattr(
        deploy_poll, "eligible", lambda *args: pytest.fail("Explicit retry required")
    )
    deploy_poll.poll(poller, REPOSITORY)


def test_private_failure_restores_real_phase3_schema_records_permissions_and_wal(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib
    from datetime import UTC, datetime

    from app.storage.sqlite import SCHEMA_V1, SQLiteRepository

    data = Path(deploy_host.read_env(host.config)["EBS_DATA_DIR"])
    path = data / "system/system.sqlite3"
    path.unlink()
    stamp = datetime.now(UTC).isoformat()
    digest = hashlib.sha256(b"retained immutable bytes").hexdigest()
    workbook = {
        "workbook_id": "wb",
        "owner_subject": "owner",
        "session_id": None,
        "created_at": stamp,
    }
    version = {
        "version_id": "v",
        "workbook_id": "wb",
        "owner_subject": "owner",
        "session_id": None,
        "file_name": "original.xlsx",
        "source_sha256": digest,
        "blob_key": "ordinary/wb/v/source",
        "size_bytes": 24,
        "worksheet_count": 1,
        "nonempty_cell_count": 0,
        "created_at": stamp,
        "status": "AUDIT_COMPLETE",
        "latest_audit_id": "a",
        "source_version_id": None,
    }
    audit = {
        "audit_id": "a",
        "workbook_id": "wb",
        "version_id": "v",
        "owner_subject": "owner",
        "session_id": None,
        "ruleset_version": "1.0.0",
        "status": "AUDIT_COMPLETE",
        "started_at": stamp,
        "completed_at": stamp,
        "findings": [],
        "limitations": [],
        "error": None,
    }
    with sqlite3.connect(path) as db:
        for statement in SCHEMA_V1:
            db.execute(statement)
        db.execute("PRAGMA user_version=1")
        db.execute(
            "INSERT INTO workbooks VALUES (?,?,?,?)", ("wb", None, "owner", json.dumps(workbook))
        )
        db.execute(
            "INSERT INTO versions VALUES (?,?,?,?)",
            ("v", "wb", version["blob_key"], json.dumps(version)),
        )
        db.execute("INSERT INTO audits VALUES (?,?,?)", ("a", "v", json.dumps(audit)))
    path.chmod(0o640)
    info = path.stat()
    retained = b'{"historicalReport":"unchanged"}'
    with sqlite3.connect(data / "tester/tester.sqlite3") as db:
        db.execute("CREATE TABLE retained_reports(id TEXT PRIMARY KEY, report BLOB)")
        db.execute("INSERT INTO retained_reports VALUES ('old',?)", (retained,))
    original_payload = json.dumps(audit)

    def migrate_then_fail(directory: Path, image: str) -> None:
        assert not any(args[0] == "up" and args[-1] == "proxy" for _, _, args in host.events)
        SQLiteRepository(path).initialize()
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        Path(str(path) + "-wal").write_bytes(b"stale migrated WAL must not be replayed")
        Path(str(path) + "-shm").write_bytes(b"stale migrated shared memory")
        raise RuntimeError("private migration readiness failed")

    monkeypatch.setattr(host, "wait_private_healthy", migrate_then_fail)
    with pytest.raises(RuntimeError, match="private migration readiness"):
        host.update()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            db.execute("SELECT payload FROM audits WHERE id='a'").fetchone()[0] == original_payload
        )
        assert db.execute("SELECT payload FROM versions WHERE id='v'").fetchone()[0] == json.dumps(
            version
        )
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='remediations'").fetchall()
    assert (path.stat().st_uid, path.stat().st_gid, path.stat().st_mode & 0o777) == (
        info.st_uid,
        info.st_gid,
        0o640,
    )
    assert not Path(str(path) + "-wal").exists()
    assert not Path(str(path) + "-shm").exists()
    with sqlite3.connect(data / "tester/tester.sqlite3") as db:
        assert db.execute("SELECT report FROM retained_reports").fetchone()[0] == retained
    assert deploy_host.read_env(host.config)["DEPLOYMENT_SHA"] == OLD


@pytest.mark.parametrize("bad_configuration", [None, "faults_disabled", "legacy_manifest"])
def test_private_probes_use_actual_apps_readonly_before_proxy_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_configuration: str | None,
) -> None:
    from io import BytesIO
    from urllib.error import HTTPError

    from fastapi.testclient import TestClient

    from app.config import SystemSettings
    from app.main import create_app as system_app
    from tester.config import TesterSettings
    from tester.main import create_app as tester_app

    root = Path(__file__).parents[2]
    monkeypatch.setenv("RULESET_VERSION", "2.0.0")
    monkeypatch.setenv("ENABLE_TEST_INTERFACE", "true")
    monkeypatch.setenv("ENABLE_TEST_AUDIT_FAULTS", "true")
    monkeypatch.setenv("METADATA_DB_PATH", str(tmp_path / "system.sqlite3"))
    monkeypatch.setenv("BLOB_ROOT", str(tmp_path / "blobs"))
    monkeypatch.setenv("TESTER_DB_PATH", str(tmp_path / "tester.sqlite3"))
    monkeypatch.setenv("FIXTURE_MANIFEST_PATH", str(root / "tests/fixtures/manifest-phase4.json"))
    settings = SystemSettings(_env_file=None)
    requested: list[str] = []
    with (
        TestClient(system_app(settings)) as system,
        TestClient(tester_app(TesterSettings(_env_file=None))) as tester,
    ):
        if bad_configuration == "faults_disabled":
            monkeypatch.setenv("ENABLE_TEST_AUDIT_FAULTS", "false")
        if bad_configuration == "legacy_manifest":
            monkeypatch.setenv("FIXTURE_MANIFEST_PATH", str(root / "tests/fixtures/manifest.json"))

        def local_urlopen(url: str, timeout: int = 5) -> BytesIO:
            requested.append(url)
            port = ":8000" if ":8000/" in url else ":8001"
            path = url.split(port, 1)[1]
            response = (system if port == ":8000" else tester).get(path)
            if response.status_code >= 400:
                raise HTTPError(url, response.status_code, "local test response", {}, None)
            return BytesIO(response.content)

        monkeypatch.setattr("urllib.request.urlopen", local_urlopen)
        deploy = deploy_host.Deployment(tmp_path, REPOSITORY, SHA)
        monkeypatch.setattr(deploy, "verify_images", lambda *args: None)

        class ProbeRejected(Exception):
            pass

        def container_exec(directory: Path, env: Path, *args: str, capture: bool = False) -> str:
            assert args[:2] == ("exec", "-T")
            assert args[2] in {"system", "tester"}
            assert args[3:5] == ("python", "-c")
            try:
                exec(compile(args[5], "private-container-probe", "exec"), {})
            except RuntimeError as exc:
                # Observe the probe's actual rejection without running its two-minute
                # container retry loop or altering clocks used by TestClient threads.
                raise ProbeRejected(str(exc)) from exc
            return ""

        monkeypatch.setattr(deploy, "compose", container_exec)
        if bad_configuration is None:
            deploy.wait_private_healthy(tmp_path, "team9-phase3:" + SHA)
            assert requested == [
                "http://127.0.0.1:8000/api/v1/health",
                "http://127.0.0.1:8001/openapi.json",
                "http://127.0.0.1:8001/test/report",
            ]
        else:
            with pytest.raises(ProbeRejected, match="incomplete|incompatible"):
                deploy.wait_private_healthy(tmp_path, "team9-phase3:" + SHA)
        assert all("/workbooks" not in url and "/test/setup" not in url for url in requested)
        with sqlite3.connect(settings.metadata_db_path) as db:
            assert db.execute("SELECT count(*) FROM workbooks").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_published_release_recovers_when_main_has_advanced_without_rebuilding(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    host.fail_health = True
    with pytest.raises(RuntimeError):
        host.update()
    published_directory = host.release
    host.fail_health = False
    host.sha = "c" * 40
    host.release = host.root / "releases" / host.sha
    host.events.clear()
    monkeypatch.setattr(
        host, "is_current", lambda: pytest.fail("Recovery must precede new-main gates")
    )
    monkeypatch.setattr(
        host, "prepare_release", lambda *args: pytest.fail("Do not build during recovery")
    )
    monkeypatch.setattr(
        host, "backup", lambda *args: pytest.fail("Do not snapshot or restore accepted data")
    )
    host.update()
    assert not host.publication.exists()
    assert json.loads(host.state.read_text())["sha"] == SHA
    assert all(directory == published_directory for directory, _, _ in host.events)
    assert all(image == "team9-phase3:" + SHA for _, image, _ in host.events)
    starts = [args for _, _, args in host.events if args[0] == "up"]
    assert "--no-recreate" in starts[0]
    assert all("--force-recreate" not in args for args in starts)


def test_poller_recovers_only_pinned_existing_release_before_new_main_ci(
    poller: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = poller / "releases" / OLD
    source = directory / "scripts/deploy_host.py"
    source.parent.mkdir(parents=True)
    source.write_text("# vetted previously published deployment artifact\n")
    publication = poller / "config/publication-state.json"
    publication.write_text(json.dumps({"sha": OLD, "directory": str(directory)}))
    (poller / "config/deploy-failed.json").write_text(json.dumps({"sha": OLD}))
    lookup_main = deploy_poll.subprocess.run
    monkeypatch.setattr(
        deploy_poll.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Existing recovery needs no main lookup"),
    )
    monkeypatch.setattr(
        deploy_poll,
        "eligible",
        lambda *args: pytest.fail("No new candidate is admitted during existing-image recovery"),
    )
    recovered: list[str] = []

    def recover(candidate: Path, sha: str, repository: str) -> int:
        assert candidate == source and sha == OLD
        assert candidate.read_text() == "# vetted previously published deployment artifact\n"
        recovered.append(sha)
        publication.unlink()
        (poller / "config/deployment-state.json").write_text(json.dumps({"sha": OLD}))
        return 0

    monkeypatch.setattr(deploy_poll, "execute", recover)
    deploy_poll.poll(poller, REPOSITORY, retry=True)
    assert recovered == [OLD]
    assert not (poller / "config/deploy-failed.json").exists()
    # The next main commit still cannot execute before its own exact-SHA CI passes.
    monkeypatch.setattr(deploy_poll.subprocess, "run", lookup_main)
    monkeypatch.setattr(deploy_poll, "eligible", lambda *args: False)
    monkeypatch.setattr(deploy_poll, "execute", lambda *args: pytest.fail("New main must pass CI"))
    deploy_poll.poll(poller, REPOSITORY)


def test_lost_proxy_start_ack_does_not_restore_prepublication_database(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = Path(deploy_host.read_env(host.config)["EBS_DATA_DIR"])
    path = data / "system/system.sqlite3"
    compose = host.compose

    def publish_then_lose_ack(directory: Path, env: Path, *args: str, capture: bool = False) -> str:
        result = compose(directory, env, *args, capture=capture)
        if args[0] == "up" and args[-1] == "proxy":
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA user_version=2")
                db.execute("CREATE TABLE published_acceptances(id TEXT)")
                db.execute("INSERT INTO published_acceptances VALUES ('must survive')")
            raise RuntimeError("Lost proxy-start acknowledgement after a public acceptance")
        return result

    monkeypatch.setattr(host, "compose", publish_then_lose_ack)
    with pytest.raises(RuntimeError, match="Lost proxy-start"):
        host.update()
    assert host.publication.exists()
    assert deploy_host.read_env(host.config)["DEPLOYMENT_SHA"] == SHA
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT id FROM published_acceptances").fetchone()[0] == "must survive"
    assert not any(
        image == "team9-phase3:" + OLD and args[0] == "up" for _, image, args in host.events
    )


@pytest.mark.parametrize(
    "module,value",
    [(deploy_host, "durable publication marker"), (deploy_poll, b"durable failure marker")],
)
def test_atomic_marker_writes_fsync_both_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    value: str | bytes,
) -> None:
    import os
    import stat

    sync, replace = os.fsync, os.replace
    events: list[str] = []

    def fsync(fd: int) -> None:
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        sync(fd)

    def renamed(source: Path, destination: Path) -> None:
        replace(source, destination)
        events.append("replace")

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", renamed)
    path = tmp_path / "marker"
    module.private_write(path, value)
    assert events == ["file", "replace", "directory"]
    assert path.read_bytes() == (value.encode() if isinstance(value, str) else value)
    assert path.stat().st_mode & 0o777 == 0o600


def test_proxy_grace_period_is_not_cut_short_by_the_compose_command_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = deploy_host.Deployment(tmp_path, REPOSITORY, SHA)
    bounds: list[int] = []

    def run(*command: str, capture: bool = False, timeout: int = 900) -> str:
        bounds.append(timeout)
        return ""

    monkeypatch.setattr(deployment, "run", run)
    deployment.compose(tmp_path, tmp_path / "config.env", "stop", "--timeout", "1900", "proxy")
    assert bounds[0] >= 1900 + 30


def put_remote_session(
    host: Host,
    *,
    state: str = "READY",
    created_at: str = "2000-01-01T00:00:00Z",
    protection: str | None = "2999-01-01T00:00:00Z",
    include_protection: bool = True,
) -> dict[str, Any]:
    data = Path(deploy_host.read_env(host.config)["EBS_DATA_DIR"])
    payload = {
        "session_id": "remote",
        "owner_subject": "external-admin",
        "allowed_subjects": ["owner"],
        "state": state,
        "created_at": created_at,
        "revision": OLD,
        "cleanup_error": None,
        "audit_failures_remaining": 0,
    }
    if include_protection:
        payload["deployment_protected_until"] = protection
    with sqlite3.connect(data / "system/system.sqlite3") as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,state TEXT,payload TEXT)"
        )
        db.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?,?,?)", ("remote", state, json.dumps(payload))
        )
    return payload


@pytest.mark.parametrize("state", ["PREPARING", "READY"])
def test_external_test_session_between_jobs_defers_before_any_build(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    put_remote_session(host, state=state)
    data = Path(deploy_host.read_env(host.config)["EBS_DATA_DIR"])
    assert host.active_work(data) == 1
    with sqlite3.connect(data / "system/system.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    with sqlite3.connect(data / "tester/tester.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM tester_sessions").fetchone()[0] == 0
    monkeypatch.setattr(
        host, "prepare_release", lambda *args: pytest.fail("Build competes with remote grading")
    )
    clock = iter([0.0, 1901.0])
    monkeypatch.setattr(deploy_host.time, "monotonic", lambda: next(clock))
    original = host.config.read_bytes()
    with pytest.raises(TimeoutError, match="Active audit/tester work"):
        host.update()
    assert not host.events
    assert not host.state.exists()
    assert host.config.read_bytes() == original


@pytest.mark.parametrize(
    "state,fresh_creation,protection,include_protection,expected",
    [
        ("READY", True, None, False, 1),
        ("PREPARING", True, None, False, 1),
        ("READY", True, None, True, 1),
        ("READY", False, None, False, 0),
        ("READY", False, "2999-01-01T00:00:00+00:00", True, 1),
        ("READY", True, "2000-01-01T00:00:00Z", True, 0),
        ("FAILED", True, "2999-01-01T00:00:00Z", True, 0),
        ("CLEANED", True, "2999-01-01T00:00:00Z", True, 0),
        ("READY", False, "invalid-timestamp", True, 1),
    ],
)
def test_remote_session_lease_legacy_fallback_expiry_and_cleanup_are_readonly(
    host: Host,
    state: str,
    fresh_creation: bool,
    protection: str | None,
    include_protection: bool,
    expected: int,
) -> None:
    from datetime import UTC, datetime

    created = datetime.now(UTC).isoformat() if fresh_creation else "2000-01-01T00:00:00Z"
    payload = put_remote_session(
        host,
        state=state,
        created_at=created,
        protection=protection,
        include_protection=include_protection,
    )
    data = Path(deploy_host.read_env(host.config)["EBS_DATA_DIR"])
    assert host.active_work(data) == expected
    with sqlite3.connect(data / "system/system.sqlite3") as db:
        assert (
            json.loads(db.execute("SELECT payload FROM sessions WHERE id='remote'").fetchone()[0])
            == payload
        )


def test_new_remote_session_after_build_is_still_protected_before_activation(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare = host.prepare_release

    def build_then_remote_setup(settings: dict[str, str]) -> None:
        prepare(settings)
        put_remote_session(host)

    monkeypatch.setattr(host, "prepare_release", build_then_remote_setup)
    clock = iter([0.0, 0.0, 1901.0])
    monkeypatch.setattr(deploy_host.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="Active audit/tester work"):
        host.update()
    assert host.release.exists()
    assert all(args[0] not in {"up", "stop"} for _, _, args in host.events)


def test_remote_setup_racing_shutdown_defers_without_migration_or_losing_session(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose = host.compose

    def finish_setup_during_shutdown(
        directory: Path, env: Path, *args: str, capture: bool = False
    ) -> str:
        result = compose(directory, env, *args, capture=capture)
        if args == ("stop", "--timeout", "90", "system", "worker", "tester"):
            put_remote_session(host)
        return result

    monkeypatch.setattr(host, "compose", finish_setup_during_shutdown)
    with pytest.raises(TimeoutError, match="Work arrived during quiescence"):
        host.update()
    assert "backup" not in host.timeline
    assert not host.private_healthy
    assert not host.publication.exists()
    assert deploy_host.read_env(host.config)["DEPLOYMENT_SHA"] == OLD
    data = Path(deploy_host.read_env(host.config)["EBS_DATA_DIR"])
    assert host.active_work(data) == 1


def test_missing_existing_database_fails_before_build_and_does_not_create_one(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = Path(deploy_host.read_env(host.config)["EBS_DATA_DIR"])
    database = data / "system/system.sqlite3"
    database.unlink()
    monkeypatch.setattr(
        host,
        "prepare_release",
        lambda *args: pytest.fail("Do not build with missing retained state"),
    )
    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        host.update()
    assert not database.exists()
    assert not host.events


def test_main_advancing_during_prebuild_drain_never_builds_or_stops_old_release(
    host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host.current = [True, False]
    monkeypatch.setattr(
        host, "prepare_release", lambda *args: pytest.fail("Main advanced before build")
    )
    original = host.config.read_bytes()
    host.update()
    assert not host.events
    assert host.config.read_bytes() == original
