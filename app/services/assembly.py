"""Explicit server-configured integration factories, with no fallback implementations."""

from importlib import import_module
from typing import Any

from app.auth.service import TokenAuthService
from app.config import BlobBackend, SystemSettings
from app.contracts.ports import SystemServices
from app.storage.blobs import FilesystemBlobStore, S3BlobStore
from app.storage.sqlite import SQLiteRepository


def load_factory(path: str) -> Any:
    module, separator, name = path.partition(":")
    if not separator or not name:
        raise ValueError("Factory must use module:attribute syntax")
    return getattr(import_module(module), name)


def assemble(settings: SystemSettings, **injected: Any) -> SystemServices:
    repository = injected.get("repository") or SQLiteRepository(settings.metadata_db_path)
    blobs = injected.get("blob_store")
    if blobs is None:
        blobs = (
            FilesystemBlobStore(settings.blob_root)
            if (settings.blob_backend == BlobBackend.FILESYSTEM)
            else S3BlobStore(str(settings.s3_bucket), str(settings.aws_region), settings.s3_prefix)
        )
    engine = injected.get("engine")
    if engine is None and settings.engine_factory:
        engine = load_factory(settings.engine_factory)()
    runner = injected.get("runner")
    if runner is None and settings.runner_factory:
        runner = load_factory(settings.runner_factory)(
            engine=engine, enable_code_measurement=settings.enable_code_measurement
        )
    session_service = injected.get("session_service")
    if session_service is None and settings.session_service_factory:
        session_service = load_factory(settings.session_service_factory)(
            repository=repository,
            blob_store=blobs,
            settings=settings,
        )
    return SystemServices(
        settings=settings,
        repository=repository,
        blob_store=blobs,
        engine=engine,
        runner=runner,
        session_service=session_service,
        auth_service=injected.get("auth_service") or TokenAuthService(settings),
    )
