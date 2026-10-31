"""Typed local/deployment inputs. Loading settings performs no external I/O."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.contracts.models import SHA256, NonemptyText, Principal, SemVer


class AppEnvironment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class BlobBackend(StrEnum):
    FILESYSTEM = "filesystem"
    S3 = "s3"


PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
RootPath = Annotated[str, Field(pattern=r"^(/[^/?#]+)*$")]


class EnvironmentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
    )
    app_env: AppEnvironment = AppEnvironment.DEVELOPMENT


class SystemSettings(EnvironmentSettings):
    api_root_path: RootPath = ""
    ruleset_version: SemVer = "2.0.0"
    deployment_sha: NonemptyText = "unknown"
    metadata_db_path: Path = Path("state/system.sqlite3")
    blob_backend: BlobBackend = BlobBackend.FILESYSTEM
    blob_root: Path = Path("state/sources")
    s3_bucket: NonemptyText | None = None
    s3_prefix: str = "sources/"
    aws_region: NonemptyText | None = None
    system_token_hashes: dict[SHA256, Principal] = Field(
        default_factory=dict,
        validation_alias="SYSTEM_TOKEN_HASHES_JSON",
        repr=False,
        exclude=True,
    )
    reviewer_grants: dict[NonemptyText, list[NonemptyText]] = Field(
        default_factory=dict,
        validation_alias="REVIEWER_GRANTS_JSON",
        repr=False,
        exclude=True,
    )
    engine_memory_limit_mib: Annotated[int, Field(gt=0)] = 512
    enable_test_interface: bool = False
    enable_test_audit_faults: bool = False
    test_session_users: dict[NonemptyText, list[NonemptyText]] = Field(
        default_factory=dict,
        validation_alias="TEST_SESSION_USERS_JSON",
        repr=False,
        exclude=True,
    )
    enable_code_measurement: bool = False
    engine_factory: NonemptyText | None = "app.audit.engine:OoxmlWorkbookEngine"
    runner_factory: NonemptyText | None = "app.measurement.runner:MeasuredEngineRunner"
    session_service_factory: NonemptyText | None = (
        "app.test_interface.service:PersistentSessionService"
    )
    test_router_factory: NonemptyText | None = "app.test_interface.routes:create_router"
    ui_router_factory: NonemptyText | None = "app.ui.routes:create_router"
    validation_timeout_seconds: PositiveSeconds = 30
    audit_timeout_seconds: PositiveSeconds = 60
    remediation_timeout_seconds: PositiveSeconds = 180
    queue_timeout_seconds: PositiveSeconds = 15

    @model_validator(mode="after")
    def supplied_deployment_inputs(self) -> Self:
        if self.blob_backend == BlobBackend.S3 and (not self.s3_bucket or not self.aws_region):
            raise ValueError("S3 selection requires S3_BUCKET and AWS_REGION")
        if self.app_env == AppEnvironment.PRODUCTION and self.deployment_sha == "unknown":
            raise ValueError("production requires a supplied DEPLOYMENT_SHA")
        if self.audit_timeout_seconds > 60 or self.validation_timeout_seconds > 30:
            raise ValueError("audit/validation deadlines cannot exceed 60/30 seconds")
        if self.remediation_timeout_seconds > 180:
            raise ValueError("remediation deadline cannot exceed 180 seconds")
        if self.queue_timeout_seconds > 15:
            raise ValueError("queue deadline cannot exceed 15 seconds")
        if self.app_env == AppEnvironment.PRODUCTION and not self.system_token_hashes:
            raise ValueError("production requires configured token hashes")
        if self.deployment_sha != "unknown":
            import re

            if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.deployment_sha) is None:
                raise ValueError("DEPLOYMENT_SHA must be a full Git SHA, or unknown locally")
        return self
