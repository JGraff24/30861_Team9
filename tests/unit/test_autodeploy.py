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
        self.current = [True, True, True]

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
        return ""

    def wait_healthy(self, directory: Path, image: str, hostname: str) -> None:
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
    assert len(starts) == 1 and "--force-recreate" in starts[0][2]
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


def test_failed_health_restores_previous_config_and_image(host: Host) -> None:
    original = host.config.read_bytes()
    host.fail_health = True
    with pytest.raises(RuntimeError, match="unhealthy"):
        host.update()
    assert host.config.read_bytes() == original
    assert host.healthy == ["team9-phase3:" + SHA, "team9-phase3:" + OLD]
    assert not host.state.exists()
    assert host.events[-1][1] == "team9-phase3:" + OLD
    assert not list(host.config.parent.glob("candidate-*.env"))


def test_failed_next_release_preserves_existing_release_record(host: Host) -> None:
    previous = host.root / "releases" / OLD
    (previous / "deploy").mkdir(parents=True)
    (previous / "deploy/compose.production.yaml").touch()
    original = json.dumps({"sha": OLD, "directory": str(previous)})
    host.state.write_text(original)
    host.fail_health = True
    with pytest.raises(RuntimeError):
        host.update()
    assert host.state.read_text() == original
    assert host.events[-1][0] == previous


def test_main_advancing_during_build_leaves_production_running(host: Host) -> None:
    host.current = [True, False]
    original = host.config.read_bytes()
    host.update()
    assert host.config.read_bytes() == original
    assert all(event[2][0] not in {"up", "stop"} for event in host.events)


def test_main_advancing_while_waiting_leaves_production_running(host: Host) -> None:
    host.current = [True, True, False]
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
