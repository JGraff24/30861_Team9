"""Deployment gates and rollback boundaries; no AWS, GitHub or Docker access."""

from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import deploy_aws, deploy_host

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


def github_responses(
    monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]], head: str = SHA
) -> None:
    def response(path: str) -> dict[str, Any]:
        if path.endswith("branches/main"):
            return {"commit": {"sha": head}}
        assert "actions/workflows/ci.yml/runs?" in path
        assert "event=push" in path and "head_sha=" + SHA in path
        return {"workflow_runs": runs}

    monkeypatch.setattr(deploy_aws, "github", response)


def test_exact_successful_main_ci_is_eligible(monkeypatch: pytest.MonkeyPatch) -> None:
    github_responses(monkeypatch, [ci_run()])
    assert deploy_aws.eligible(REPOSITORY, SHA)


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
    with pytest.raises(RuntimeError):
        deploy_aws.eligible(REPOSITORY, SHA)


def test_failed_rerun_overrides_earlier_success(monkeypatch: pytest.MonkeyPatch) -> None:
    github_responses(monkeypatch, [ci_run(), ci_run(run_attempt=2, conclusion="failure")])
    with pytest.raises(RuntimeError, match="has not passed"):
        deploy_aws.eligible(REPOSITORY, SHA)


def test_superseded_main_does_not_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    github_responses(monkeypatch, [ci_run()], head=OLD)
    assert not deploy_aws.eligible(REPOSITORY, SHA)


@pytest.mark.parametrize(
    "status,code", [("Success", 0), ("Failed", 1), ("Success", 1), ("TimedOut", -1)]
)
def test_ssm_result_is_checked(monkeypatch: pytest.MonkeyPatch, status: str, code: int) -> None:
    calls: list[tuple[str, ...]] = []

    def aws(region: str, *args: str) -> dict[str, Any]:
        assert region == "us-east-2"
        calls.append(args)
        if args[1] == "send-command":
            assert json.loads(args[args.index("--parameters") + 1]) == {"ReleaseSha": [SHA]}
            assert "AWS-RunShellScript" not in args
            return {"Command": {"CommandId": "command-1"}}
        return {"Status": status, "ResponseCode": code}

    monkeypatch.setattr(deploy_aws, "aws", aws)
    if status == "Success" and code == 0:
        deploy_aws.deploy("us-east-2", "i-0922ebb45b0bed415", "deployment-document", SHA)
    else:
        with pytest.raises(RuntimeError):
            deploy_aws.deploy("us-east-2", "i-0922ebb45b0bed415", "deployment-document", SHA)
    assert len(calls) == 2


def test_ssm_retries_eventual_consistency_then_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes: list[Any] = [
        subprocess.CalledProcessError(254, "aws", stderr="InvocationDoesNotExist"),
        {"Status": "InProgress"},
        {"Status": "Success", "ResponseCode": 0},
    ]

    def aws(region: str, *args: str) -> dict[str, Any]:
        if args[1] == "send-command":
            return {"Command": {"CommandId": "command-1"}}
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, dict)
        return result

    monkeypatch.setattr(deploy_aws, "aws", aws)
    monkeypatch.setattr(deploy_aws.time, "sleep", lambda seconds: None)
    deploy_aws.deploy("us-east-2", "i-0922ebb45b0bed415", "deployment-document", SHA)
    assert not outcomes


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
    clock = iter([0.0, 661.0])
    monkeypatch.setattr(deploy_host.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="Active audit/tester work"):
        host.update()
    assert all(event[2][0] != "stop" for event in host.events)


def test_host_script_parses_on_amazon_linux_python39() -> None:
    ast.parse(Path(deploy_host.__file__).read_text(), feature_version=(3, 9))
