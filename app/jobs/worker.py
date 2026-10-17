"""Separate durable worker: python -m app.jobs.worker [--once]."""

import argparse
import logging
import math
import time
from datetime import UTC, datetime
from uuid import uuid4

from app.config import SystemSettings
from app.contracts.errors import EngineExecutionError, IntegrityError, LeaseConflictError
from app.contracts.models import ExecutionErrorKind, ProcessingLimits
from app.contracts.ports import SystemServices
from app.jobs.execution import execute_bounded
from app.services.assembly import assemble
from app.services.projects import record_measurement, verify_validation
from app.storage.blobs import sha256

logger = logging.getLogger(__name__)


class AuditWorker:
    def __init__(self, services: SystemServices, worker_id: str | None = None) -> None:
        self.services = services
        self.worker_id = worker_id or uuid4().hex

    def run_once(self) -> bool:
        services = self.services
        repository, blobs, runner = services.repository, services.blob_store, services.runner
        if repository is None or blobs is None or runner is None:
            raise RuntimeError(
                "Worker requires repository, blob store and Ryan's configured runner"
            )
        timeout = min(60, services.settings.audit_timeout_seconds)
        # Lease includes pre/post integrity I/O, bounded runner startup and DB finalization.
        job = repository.claim_next_job(self.worker_id, datetime.now(UTC), math.ceil(timeout) + 60)
        if job is None:
            return False
        started = time.monotonic()
        try:
            version = repository.get_version(job.workbook_id, job.version_id)
            audit = repository.get_audit(job.workbook_id, job.version_id, job.audit_id)
            if version is None or audit is None:
                raise IntegrityError("Job source metadata missing")
            data = blobs.read_source(version.blob_key)
            if sha256(data) != version.source_sha256 or len(data) != version.size_bytes:
                raise IntegrityError("Source integrity differs before processing")
            limits = ProcessingLimits()
            validated = version.validated
            if validated is None:
                # Compatibility with records accepted before inventory persistence was added.
                validation = execute_bounded(
                    runner,
                    "validate",
                    data,
                    version.file_name,
                    limits,
                    audit.ruleset_version,
                    None,
                    job.session_id,
                    services.settings.deployment_sha,
                    min(30, services.settings.validation_timeout_seconds, timeout),
                    services.settings.engine_memory_limit_mib,
                )
                record_measurement(services, job.session_id, validation.measurement)
                validated = validation.validated
            if validated is None:
                raise IntegrityError("Validation metadata missing")
            verify_validation(data, validated, limits)
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise EngineExecutionError(
                    500, "AUDIT_TIMEOUT", "Audit deadline expired.", kind=ExecutionErrorKind.TIMEOUT
                )
            result = execute_bounded(
                runner,
                "audit",
                data,
                version.file_name,
                limits,
                audit.ruleset_version,
                validated,
                job.session_id,
                services.settings.deployment_sha,
                remaining,
                services.settings.engine_memory_limit_mib,
            )
            record_measurement(services, job.session_id, result.measurement)
            retained = blobs.read_source(version.blob_key)
            if sha256(retained) != version.source_sha256 or len(retained) != version.size_bytes:
                raise IntegrityError("Source integrity differs after processing")
            if time.monotonic() - started > timeout:
                raise EngineExecutionError(
                    500, "AUDIT_TIMEOUT", "Audit deadline expired.", kind=ExecutionErrorKind.TIMEOUT
                )
            if result.evaluation is None:
                raise IntegrityError("Audit evaluation missing")
            repository.complete_audit(
                job.job_id, self.worker_id, result.evaluation, datetime.now(UTC)
            )
            code = "AUDIT_SUCCEEDED"
        except LeaseConflictError:
            # Another recovery transaction already decided this job; never overwrite history.
            code = "LEASE_CONFLICT"
        except Exception as exc:
            code = (
                "SOURCE_INTEGRITY_FAILED"
                if isinstance(exc, IntegrityError)
                else (exc.code if isinstance(exc, EngineExecutionError) else "PROCESSING_FAILED")
            )
            if isinstance(exc, EngineExecutionError):
                try:
                    record_measurement(services, job.session_id, exc.measurement)
                except Exception:
                    logger.error("measurement_persistence_failed", extra={"jobId": job.job_id})
            from app.contracts.models import ProjectError

            try:
                repository.fail_audit(
                    job.job_id,
                    self.worker_id,
                    ProjectError(
                        code=code,
                        message="Workbook processing failed.",
                        requestId=job.job_id,
                        details={},
                    ),
                    datetime.now(UTC),
                )
            except Exception:
                # An unavailable DB cannot record a result; lease recovery will fail it later.
                logger.error("job_failure_persistence_failed", extra={"jobId": job.job_id})
        logger.info(
            "job_finished",
            extra={
                "jobId": job.job_id,
                "auditId": job.audit_id,
                "sessionId": job.session_id,
                "code": code,
                "duration": round(time.monotonic() - started, 3),
            },
        )
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    from app.services.logging import configure_logging

    configure_logging()
    services = assemble(SystemSettings())
    if services.runner is None or services.engine is None:
        raise SystemExit(
            "Worker unavailable: configure Dylan ENGINE_FACTORY and Ryan RUNNER_FACTORY"
        )
    assert services.repository is not None
    services.repository.initialize()
    worker = AuditWorker(services)
    while True:
        worker.run_once()
        if args.once:
            break
        time.sleep(0.25)


if __name__ == "__main__":
    main()
