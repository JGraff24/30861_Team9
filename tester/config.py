"""Independent tester settings; access configuration is never a grader input."""

import ipaddress
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator

from app.config import AppEnvironment, EnvironmentSettings, PositiveSeconds, RootPath
from app.contracts.models import SHA256, ContractModel, NonemptyText


class TargetAuth(ContractModel):
    """Private per-target credentials. `reviewer` must hold a read grant from userA only.

    `test-adminB` is an optional second test-admin used only to prove that a foreign
    test-admin cannot advance this tester's namespace clock (Phase 5 AC-15). Without it the
    dependent cases are reported as skips, never as passes.
    """

    user_a: SecretStr = Field(alias="userA", repr=False)
    user_b: SecretStr = Field(alias="userB", repr=False)
    reviewer: SecretStr = Field(repr=False)
    test_admin: SecretStr = Field(alias="test-admin", repr=False)
    test_admin_b: SecretStr | None = Field(default=None, alias="test-adminB", repr=False)

    @model_validator(mode="after")
    def nonempty_tokens(self) -> Self:
        values = [self.user_a, self.user_b, self.reviewer, self.test_admin]
        if self.test_admin_b is not None:
            values.append(self.test_admin_b)
        if any(not value.get_secret_value().strip() for value in values):
            raise ValueError("configured target tokens must be nonempty")
        return self


def validate_base_url(value: str, environment: AppEnvironment) -> None:
    url = urlsplit(value)
    if (
        not url.hostname
        or url.scheme not in {"http", "https"}
        or url.username is not None
        or url.password is not None
        or "?" in value
        or "#" in value
        or value != value.strip()
        or url.path.rstrip("/").endswith("/test")
    ):
        raise ValueError(
            "target must be an HTTP(S) base URL without credentials/query/fragment/test"
        )
    try:
        _ = url.port
    except ValueError as exc:
        raise ValueError("target has invalid port") from exc
    try:
        address = ipaddress.ip_address(url.hostname)
    except ValueError:
        address = None
    if address is not None and (address.is_link_local or address.is_unspecified):
        raise ValueError("metadata/link-local/unspecified targets are forbidden")
    if environment == AppEnvironment.PRODUCTION and url.scheme != "https":
        raise ValueError("production targets require HTTPS")
    if url.scheme == "http" and not (
        url.hostname in {"localhost", "system", "host.docker.internal"}
        or (address is not None and address.is_loopback)
    ):
        raise ValueError("development HTTP requires an explicit loopback/Docker target")


class TesterSettings(EnvironmentSettings):
    tester_root_path: RootPath = ""
    tester_db_path: Path = Path("state/tester.sqlite3")
    tester_operator_token_hashes: dict[SHA256, NonemptyText] = Field(
        default_factory=dict,
        validation_alias="TESTER_OPERATOR_TOKEN_HASHES_JSON",
        repr=False,
        exclude=True,
    )
    allowed_target_urls: list[NonemptyText] = Field(default_factory=list)
    target_auth_map: dict[NonemptyText, TargetAuth] = Field(
        default_factory=dict,
        validation_alias="TARGET_AUTH_MAP_JSON",
        repr=False,
        exclude=True,
    )
    fixture_manifest_path: Path = Field(
        default_factory=lambda: (
            Path(__file__).resolve().parent / "fixtures" / "manifest-phase4.json"
            if (Path(__file__).resolve().parent / "fixtures" / "manifest-phase4.json").exists()
            else Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "manifest-phase4.json"
        )
    )
    # Phase 5 report profile (specification 3.0.0 section 14). Trusted operator configuration
    # only: the tester's own team, the exact approved-target-URL to team map, and the team's
    # versioned design-requirements map. Never derived from a grader request or from hosts.
    tester_team_id: NonemptyText | None = None
    target_team_map: dict[NonemptyText, NonemptyText] = Field(
        default_factory=dict, validation_alias="TARGET_TEAM_MAP_JSON"
    )
    tester_design_map_path: Path | None = Field(
        default_factory=lambda: next(
            (
                path
                for path in (
                    Path(__file__).resolve().parent / "design" / "team9-design-map.json",
                    Path(__file__).resolve().parents[1]
                    / "docs"
                    / "design"
                    / "team9-design-map.json",
                )
                if path.exists()
            ),
            None,
        )
    )
    tester_setup_timeout_seconds: PositiveSeconds = 45
    tester_run_timeout_seconds: PositiveSeconds = 600
    # Failed setups are always compensated on the next setup. This flag additionally
    # enables cleanup of successfully retrieved reports' target namespaces.
    tester_cleanup_prior_sessions: bool = False

    @model_validator(mode="after")
    def approved_url_shapes(self) -> Self:
        for url in self.allowed_target_urls:
            validate_base_url(url, self.app_env)
        if len(self.allowed_target_urls) != len(set(self.allowed_target_urls)):
            raise ValueError("allowed target URLs must be unique")
        if set(self.target_auth_map) - set(self.allowed_target_urls):
            raise ValueError("target access map keys must be exact approved base URLs")
        if set(self.target_team_map) - set(self.allowed_target_urls):
            raise ValueError("target team map keys must be exact approved base URLs")
        return self
