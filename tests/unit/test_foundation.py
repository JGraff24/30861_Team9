"""Settings, injection and real ASGI startup without network/storage effects."""

import json
import os
import socket
import subprocess
import sys
import typing
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import SystemSettings
from app.contracts import ports
from app.contracts.ports import Repository, SystemServices
from app.main import create_app as create_system
from tester.config import TesterSettings as SettingsForTester
from tester.main import TesterServices as ServicesForTester
from tester.main import create_app as create_tester


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for model in (SystemSettings, SettingsForTester):
        for name, field in model.model_fields.items():
            monkeypatch.delenv(str(field.validation_alias or name.upper()), raising=False)
    monkeypatch.chdir(tmp_path)


def test_factories_do_not_contact_network_or_write_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("factories must not contact network")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    for factory in (create_system, create_tester):
        application = factory()
        assert application.openapi()
    assert not Path("state").exists()
    with TestClient(create_system()) as client:
        assert client.get("/docs").status_code == 200
        assert len(client.get("/openapi.json").json()["paths"]) == 4
        assert client.get("/api/v1/health").status_code == 503
    with TestClient(create_tester()) as client:
        assert client.get("/openapi.json").json()["paths"] == {}
        assert client.get("/test/report").status_code == 404


def test_injected_ports_are_retained_and_initialized_at_lifespan() -> None:
    settings = SystemSettings()
    repository = Mock(spec=Repository)
    services = SystemServices(settings=settings, repository=repository)
    application = create_system(services=services)
    assert repository.mock_calls == []
    with TestClient(application):
        assert application.state.services is services
    repository.initialize.assert_called_once_with()
    repository.recover_stale_jobs.assert_called_once()
    assert "repository" not in services.missing_integrations
    assert "runner" in services.missing_integrations
    assert create_system(settings, repository=repository).state.services.repository is repository
    with pytest.raises(ValueError):
        create_system(settings, services=services)
    tester_services = ServicesForTester(settings=SettingsForTester())
    assert create_tester(services=tester_services).state.services is tester_services
    with pytest.raises(ValueError):
        create_tester(SettingsForTester(), services=tester_services)


def test_all_port_types_resolve_without_cyclic_imports() -> None:
    for protocol in (
        ports.WorkbookEngine,
        ports.Repository,
        ports.BlobStore,
        ports.EngineRunner,
        ports.SessionService,
        ports.CodeMeasurementStore,
        ports.AuthService,
    ):
        for name, method in vars(protocol).items():
            if not name.startswith("_") and callable(method):
                assert "return" in typing.get_type_hints(method)
    assert typing.get_type_hints(SystemServices)["settings"] is SystemSettings


def test_proxy_root_paths_are_in_docs() -> None:
    for factory, settings, root in (
        (create_system, SystemSettings(api_root_path="/system"), "/system"),
        (create_tester, SettingsForTester(tester_root_path="/tester"), "/tester"),
    ):
        with TestClient(factory(settings), root_path=root) as client:
            assert f"{root}/openapi.json" in client.get("/docs").text
            assert client.get("/openapi.json").json()["servers"] == [{"url": root}]


def test_settings_defaults_and_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SystemSettings()
    assert settings.validation_timeout_seconds == 30
    assert settings.audit_timeout_seconds == 60 and settings.queue_timeout_seconds == 15
    assert settings.engine_memory_limit_mib == 512
    assert not settings.enable_test_interface and not settings.enable_code_measurement
    assert settings.metadata_db_path != SettingsForTester().tester_db_path
    monkeypatch.setenv(
        "SYSTEM_TOKEN_HASHES_JSON", json.dumps({"a" * 64: {"subject": "owner", "role": "user"}})
    )
    monkeypatch.setenv("REVIEWER_GRANTS_JSON", '{"reviewer":["owner"]}')
    monkeypatch.setenv("TEST_SESSION_USERS_JSON", '{"admin":["owner"]}')
    monkeypatch.setenv("ENABLE_TEST_INTERFACE", "true")
    settings = SystemSettings()
    assert settings.system_token_hashes["a" * 64].subject == "owner"
    assert settings.reviewer_grants == {"reviewer": ["owner"]}
    assert settings.test_session_users == {"admin": ["owner"]}
    assert settings.enable_test_interface


def test_configuration_redacts_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    private = "private-target-token-never-log"
    monkeypatch.setenv(
        "TARGET_AUTH_MAP_JSON",
        json.dumps(
            {
                "http://127.0.0.1:8000": {
                    "userA": private,
                    "userB": private,
                    "reviewer": private,
                    "test-admin": private,
                }
            }
        ),
    )
    monkeypatch.setenv("ALLOWED_TARGET_URLS", '["http://127.0.0.1:8000"]')
    settings = SettingsForTester()
    assert private not in repr(settings)
    assert private not in settings.model_dump_json()
    access = settings.target_auth_map["http://127.0.0.1:8000"]
    assert private not in repr(access) and private not in access.model_dump_json()
    assert access.user_a.get_secret_value() == private
    system = SystemSettings(
        SYSTEM_TOKEN_HASHES_JSON={"b" * 64: {"subject": "private-owner", "role": "user"}}
    )
    assert "private-owner" not in repr(system) and "private-owner" not in system.model_dump_json()


@pytest.mark.parametrize(
    "values",
    [
        {"engine_memory_limit_mib": 0},
        {"validation_timeout_seconds": 0},
        {"audit_timeout_seconds": float("inf")},
        {"queue_timeout_seconds": -1},
        {"api_root_path": "/system/"},
        {"app_env": "invalid"},
        {"blob_backend": "s3"},
        {"deployment_sha": "invented"},
        {"app_env": "production"},
        {"SYSTEM_TOKEN_HASHES_JSON": {"not-a-hash": {"subject": "owner", "role": "user"}}},
    ],
)
def test_invalid_system_settings(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SystemSettings(**values)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "https://user:token@example.com",
        "https://example.com/system?x=y",
        "https://example.com/system#fragment",
        "https://example.com/test",
        "http://arbitrary.example/system",
        "https://example.com:invalid/system",
        "https://example.com/test/",
    ],
)
def test_invalid_target_settings(url: str) -> None:
    with pytest.raises(ValidationError):
        SettingsForTester(allowed_target_urls=[url])


def test_target_configuration_is_exact_and_production_https() -> None:
    assert SettingsForTester(allowed_target_urls=["http://system:8000"]).allowed_target_urls
    assert SettingsForTester(
        app_env="production", allowed_target_urls=["https://example.com/system"]
    )
    with pytest.raises(ValidationError):
        SettingsForTester(app_env="production", allowed_target_urls=["http://127.0.0.1:8000"])
    with pytest.raises(ValidationError):
        SettingsForTester(
            allowed_target_urls=["https://example.com/system"],
            TARGET_AUTH_MAP_JSON={
                "https://example.com/system/": {
                    "userA": "a",
                    "userB": "b",
                    "reviewer": "r",
                    "test-admin": "t",
                },
            },
        )


def test_opt_in_local_credentials_are_private_and_never_overwritten(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "create_dev_tokens.py"
    output = tmp_path / ".env.local"
    command = [sys.executable, str(script), "--local-only", "--output", str(output)]
    completed = subprocess.run(command, check=True, text=True, capture_output=True)
    settings = SettingsForTester(_env_file=output)
    private = settings.target_auth_map["http://127.0.0.1:8000"].user_a.get_secret_value()
    assert private not in completed.stdout and private not in completed.stderr
    if os.name == "posix":
        assert output.stat().st_mode & 0o777 == 0o600
    assert subprocess.run(command, capture_output=True).returncode != 0
    assert (
        subprocess.run(
            command, env=os.environ | {"APP_ENV": "production"}, capture_output=True
        ).returncode
        != 0
    )
