"""Durable worker: python -m app.jobs.worker [--once | --concurrency {1,2}]."""

import argparse
import logging
import math
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import PurePosixPath
from threading import Event
from types import FrameType
from uuid import uuid4

from app.config import SystemSettings
from app.contracts.errors import (
    EngineExecutionError,
    IntegrityError,
    LeaseConflictError,
    ProjectException,
    StorageError,
)
from app.contracts.models import (
    AuditRecord,
    AuditStatus,
    ExecutionErrorKind,
    ProcessingLimits,
    ProjectError,
    VersionRecord,
    VersionStatus,
)
from app.contracts.phase4 import RemediationJob
from app.contracts.ports import SystemServices
from app.jobs.execution import execute_bounded
from app.jobs.remediation import execute_correction_bounded, require_correction_engine
from app.services.assembly import assemble
from app.services.projects import record_measurement, verify_validation
from app.services.reports import validate_evaluation, validate_finding_ids
from app.storage.blobs import S3BlobStore, sha256

# `python -m app.jobs.worker` sets __name__ to __main__; keep the configured app logger.
logger = logging.getLogger("app.jobs.worker")


class AuditWorker:
    def __init__(self, services: SystemServices, worker_id: str | None = None) -> None:
        self.services = services
        self.worker_id = worker_id or uuid4().hex
        self._prefer_remediation = False
        self._cleanup_retry_at: dict[str, float] = {}
        self._cleanup_cursor = 0

    def run_once(
        self, *, cleanup_when_idle: bool = True, stop_requested: Event | None = None
    ) -> bool:
        services = self.services
        repository, blobs, runner = services.repository, services.blob_store, services.runner
        if repository is None or blobs is None or runner is None:
            raise RuntimeError(
                "Worker requires repository, blob store and Ryan's configured runner"
            )
        if stop_requested is not None and stop_requested.is_set():
            return False
        if self._prefer_remediation:
            remediation = repository.claim_next_remediation(
                self.worker_id,
                datetime.now(UTC),
                math.ceil(services.settings.remediation_timeout_seconds) + 30,
            )
            if remediation is not None:
                self._prefer_remediation = False
                return self._run_remediation(remediation)
        timeout = min(60, services.settings.audit_timeout_seconds)
        if stop_requested is not None and stop_requested.is_set():
            return False
        # Lease includes pre/post integrity I/O, bounded runner startup and DB finalization.
        job = repository.claim_next_job(self.worker_id, datetime.now(UTC), math.ceil(timeout) + 60)
        if job is None:
            if stop_requested is not None and stop_requested.is_set():
                return False
            remediation = repository.claim_next_remediation(
                self.worker_id,
                datetime.now(UTC),
                math.ceil(services.settings.remediation_timeout_seconds) + 30,
            )
            if remediation is not None:
                return self._run_remediation(remediation)
            if cleanup_when_idle:
                self._cleanup_candidates()
            return False
        self._prefer_remediation = True
        started = time.monotonic()
        stage = "source"
        audit: AuditRecord | None = None
        try:
            version = repository.get_version(job.workbook_id, job.version_id)
            audit = repository.get_audit(job.workbook_id, job.version_id, job.audit_id)
            if version is None or audit is None:
                raise IntegrityError("Job source metadata missing")
            data = blobs.read_source(version.blob_key)
            if sha256(data) != version.source_sha256 or len(data) != version.size_bytes:
                raise IntegrityError("Source integrity differs before processing")
            if job.test_audit_failure:
                if job.session_id is None:
                    raise IntegrityError("Test fault cannot apply to ordinary data")
                repository.fail_audit(
                    job.job_id,
                    self.worker_id,
                    ProjectError(
                        code="TEST_AUDIT_FAILURE",
                        message="Audit failure requested by the isolated test session.",
                        requestId=job.job_id,
                        details={"stage": "audit", "testOnly": True},
                    ),
                    datetime.now(UTC),
                )
                logger.info(
                    "job_finished",
                    extra={
                        "jobId": job.job_id,
                        "auditId": job.audit_id,
                        "sessionId": job.session_id,
                        "code": "TEST_AUDIT_FAILURE",
                        "stage": "audit",
                        "duration": round(time.monotonic() - started, 3),
                    },
                )
                return True
            limits = ProcessingLimits()
            validated = version.validated
            if validated is None:
                stage = "validation"
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
            stage = "audit"
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
            validate_evaluation(result.evaluation, audit.ruleset_version)
            validate_finding_ids(result.evaluation, version.source_sha256, audit.ruleset_version)
            stage = "publication"
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
                else (
                    "STORAGE_FAILED"
                    if isinstance(exc, StorageError)
                    else (exc.code if isinstance(exc, ProjectException) else "PROCESSING_FAILED")
                )
            )
            if (
                audit is not None
                and audit.ruleset_version == "2.0.0"
                and isinstance(exc, EngineExecutionError)
                and exc.kind == ExecutionErrorKind.TIMEOUT
            ):
                code = "PROCESSING_TIMEOUT"
            if isinstance(exc, EngineExecutionError):
                try:
                    record_measurement(services, job.session_id, exc.measurement)
                except Exception:
                    logger.error("measurement_persistence_failed", extra={"jobId": job.job_id})
            try:
                repository.fail_audit(
                    job.job_id,
                    self.worker_id,
                    ProjectError(
                        code=code,
                        message="Workbook processing failed.",
                        requestId=job.job_id,
                        details={"stage": stage},
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
                "stage": stage,
                "duration": round(time.monotonic() - started, 3),
            },
        )
        return True

    def _cleanup_candidates(self, *, max_items: int | None = None) -> None:
        repository, blobs = self.services.repository, self.services.blob_store
        assert repository is not None and blobs is not None
        try:
            pending = repository.pending_blob_cleanup()
        except StorageError:
            logger.error("candidate_cleanup_scan_failed")
            return
        if not pending:
            return
        processed = 0
        start = self._cleanup_cursor % len(pending)
        for offset in range(len(pending)):
            index = (start + offset) % len(pending)
            key, digest = pending[index]
            remote = isinstance(blobs, S3BlobStore)
            if remote and self._cleanup_retry_at.get(key, 0) > time.monotonic():
                continue
            if remote:
                # Keep deletion intent indefinitely, but do not reread every
                # historical missing S3 object on each 250 ms queue poll.
                self._cleanup_retry_at[key] = time.monotonic() + 60
            try:
                # Failed-job tombstones remain eligible on later sweeps so a late
                # fenced worker write is eventually removed after crash recovery.
                blobs.delete_uncommitted_source(key, digest)
            except StorageError:
                logger.error("candidate_rollback_failed")
            processed += 1
            self._cleanup_cursor = (index + 1) % len(pending)
            if remote or (max_items is not None and processed >= max_items):
                # Production housekeeping handles at most one object per interval,
                # advancing fairly through durable intents even under queue load.
                break

    def _run_remediation(self, job: RemediationJob) -> bool:
        services = self.services
        repository, blobs, runner = services.repository, services.blob_store, services.runner
        assert repository is not None and blobs is not None and runner is not None
        started = time.monotonic()
        deadline = started + min(180, services.settings.remediation_timeout_seconds)
        stage = "source"
        stored = False
        published = False
        cleanup_safe = True
        candidate_hash: str | None = None
        code = "PROCESSING_FAILED"

        def remaining() -> float:
            duration = deadline - time.monotonic()
            if duration <= 0:
                raise EngineExecutionError(
                    500,
                    "PROCESSING_TIMEOUT",
                    "Remediation exceeded its execution deadline.",
                    kind=ExecutionErrorKind.TIMEOUT,
                )
            return duration

        try:
            record = repository.get_remediation(job.remediation_id)
            version = repository.get_version(job.workbook_id, job.version_id)
            if record is None or version is None:
                raise IntegrityError("Remediation source metadata missing")
            audit = repository.get_audit(job.workbook_id, job.version_id, record.source_audit_id)
            if audit is None or (
                record.owner_subject != version.owner_subject
                or record.session_id != version.session_id
                or job.session_id != version.session_id
            ):
                raise IntegrityError("Remediation source relationship differs")
            data = blobs.read_source(version.blob_key)
            if sha256(data) != version.source_sha256 or len(data) != version.size_bytes:
                raise IntegrityError("Source integrity differs before remediation")
            has_edits = any(decision.action == "APPLY" for decision in record.decisions)
            engine = require_correction_engine(services.engine) if has_edits else None
            limits = ProcessingLimits()
            validated = version.validated
            if validated is None:
                stage = "source_validation"
                result = execute_bounded(
                    runner,
                    "validate",
                    data,
                    version.file_name,
                    limits,
                    record.ruleset_version,
                    None,
                    job.session_id,
                    services.settings.deployment_sha,
                    min(30, services.settings.validation_timeout_seconds, remaining()),
                    services.settings.engine_memory_limit_mib,
                )
                record_measurement(services, job.session_id, result.measurement)
                validated = result.validated
            if validated is None:
                raise ProjectException(
                    500, "ENGINE_RESULT_INVALID", "Source validation is missing."
                )
            verify_validation(data, validated, limits)
            stage = "editing"
            if has_edits:
                assert engine is not None
                edited = execute_correction_bounded(
                    engine,
                    "remediate",
                    data,
                    validated,
                    audit,
                    record.decisions,
                    session_id=job.session_id,
                    revision=services.settings.deployment_sha,
                    measure=services.settings.enable_code_measurement,
                    timeout_seconds=remaining(),
                    memory_limit_mib=services.settings.engine_memory_limit_mib,
                )
                record_measurement(services, job.session_id, edited.measurement)
                candidate = edited.candidate
            else:
                candidate = data
            if candidate is None:
                raise ProjectException(500, "ENGINE_RESULT_INVALID", "Candidate bytes are missing.")
            basename = PurePosixPath(version.file_name).stem
            suffix = "-remediated.xlsx"
            filename = basename[: 255 - len(suffix)] + suffix
            stage = "output_validation"
            validation = execute_bounded(
                runner,
                "validate",
                candidate,
                filename,
                limits,
                record.ruleset_version,
                None,
                job.session_id,
                services.settings.deployment_sha,
                min(30, services.settings.validation_timeout_seconds, remaining()),
                services.settings.engine_memory_limit_mib,
            )
            record_measurement(services, job.session_id, validation.measurement)
            if validation.validated is None:
                raise ProjectException(
                    500, "ENGINE_RESULT_INVALID", "Output validation is missing."
                )
            output_validated = validation.validated
            verify_validation(candidate, output_validated, limits)
            stage = "preservation"
            if has_edits:
                assert engine is not None
                preserved = execute_correction_bounded(
                    engine,
                    "verify_preservation",
                    data,
                    validated,
                    audit,
                    record.decisions,
                    candidate=candidate,
                    session_id=job.session_id,
                    revision=services.settings.deployment_sha,
                    measure=services.settings.enable_code_measurement,
                    timeout_seconds=remaining(),
                    memory_limit_mib=services.settings.engine_memory_limit_mib,
                )
                record_measurement(services, job.session_id, preserved.measurement)
            elif candidate != data:
                raise ProjectException(
                    500, "PRESERVATION_FAILED", "An unchanged candidate differs from its source."
                )
            stage = "output_audit"
            result = execute_bounded(
                runner,
                "audit",
                candidate,
                filename,
                limits,
                record.ruleset_version,
                output_validated,
                job.session_id,
                services.settings.deployment_sha,
                min(60, services.settings.audit_timeout_seconds, remaining()),
                services.settings.engine_memory_limit_mib,
            )
            record_measurement(services, job.session_id, result.measurement)
            if result.evaluation is None:
                raise ProjectException(
                    500, "ENGINE_RESULT_INVALID", "Output evaluation is missing."
                )
            validate_evaluation(result.evaluation, record.ruleset_version)
            validate_finding_ids(result.evaluation, sha256(candidate), record.ruleset_version)
            stage = "correction_verification"
            if has_edits:
                assert engine is not None
                verified = execute_correction_bounded(
                    engine,
                    "verify_corrections",
                    data,
                    validated,
                    audit,
                    record.decisions,
                    candidate=candidate,
                    evaluation=result.evaluation,
                    session_id=job.session_id,
                    revision=services.settings.deployment_sha,
                    measure=services.settings.enable_code_measurement,
                    timeout_seconds=remaining(),
                    memory_limit_mib=services.settings.engine_memory_limit_mib,
                )
                record_measurement(services, job.session_id, verified.measurement)
            stage = "source_integrity"
            retained = blobs.read_source(version.blob_key)
            if sha256(retained) != version.source_sha256 or len(retained) != version.size_bytes:
                raise IntegrityError("Source integrity differs after remediation")
            remaining()
            stage = "storage"
            candidate_hash = sha256(candidate)
            repository.register_candidate(job.job_id, self.worker_id, candidate_hash)
            digest = blobs.put_source(job.blob_key, candidate)
            stored = True
            if digest != candidate_hash or blobs.read_source(job.blob_key) != candidate:
                raise StorageError("Output storage verification failed")
            remaining()
            completed_at = datetime.now(UTC)
            output_version = VersionRecord(
                version_id=job.output_version_id,
                workbook_id=job.workbook_id,
                owner_subject=version.owner_subject,
                session_id=version.session_id,
                file_name=filename,
                source_sha256=candidate_hash,
                blob_key=job.blob_key,
                size_bytes=len(candidate),
                worksheet_count=output_validated.worksheet_count,
                nonempty_cell_count=output_validated.nonempty_cell_count,
                created_at=completed_at,
                status=VersionStatus.REMEDIATED,
                latest_audit_id=job.output_audit_id,
                source_version_id=job.version_id,
                validated=output_validated,
            )
            output_audit = AuditRecord(
                audit_id=job.output_audit_id,
                workbook_id=job.workbook_id,
                version_id=job.output_version_id,
                owner_subject=version.owner_subject,
                session_id=version.session_id,
                ruleset_version=record.ruleset_version,
                status=AuditStatus.PROCESSING,
                started_at=record.created_at,
                completed_at=None,
                findings=[],
                limitations=[],
                error=None,
            )
            stage = "publication"
            cleanup_safe = False
            try:
                repository.complete_remediation(
                    job.job_id,
                    self.worker_id,
                    output_version,
                    output_audit,
                    result.evaluation,
                    completed_at,
                )
            except Exception:
                # A lost commit acknowledgement must not delete bytes belonging to a
                # committed output. Confirm immutable identity before compensating.
                committed = repository.get_remediation(job.remediation_id)
                if committed is None or (
                    committed.status != "REMEDIATED"
                    or committed.output_version_id != job.output_version_id
                    or committed.output_id != job.output_id
                    or committed.audit_id != job.output_audit_id
                ):
                    raise
            published = True
            code = "REMEDIATION_SUCCEEDED"
        except LeaseConflictError:
            code = "LEASE_CONFLICT"
        except Exception as exc:
            if isinstance(exc, EngineExecutionError):
                try:
                    record_measurement(services, job.session_id, exc.measurement)
                except Exception:
                    logger.error("measurement_persistence_failed", extra={"jobId": job.job_id})
            if isinstance(exc, IntegrityError):
                code = (
                    "SOURCE_INTEGRITY_FAILED"
                    if stage in {"source", "source_integrity"}
                    else "STORAGE_FAILED"
                )
            elif isinstance(exc, StorageError):
                code = "STORAGE_FAILED"
            elif isinstance(exc, EngineExecutionError) and exc.kind == ExecutionErrorKind.TIMEOUT:
                code = "PROCESSING_TIMEOUT"
            elif stage == "preservation":
                code = "PRESERVATION_FAILED"
            elif stage in {"output_validation", "correction_verification"}:
                code = "OUTPUT_VALIDATION_FAILED"
            elif stage == "output_audit":
                code = "OUTPUT_AUDIT_FAILED"
            elif isinstance(exc, ProjectException) and exc.code == "ENGINE_RESULT_INVALID":
                code = exc.code
            else:
                code = "PROCESSING_FAILED"
            try:
                repository.fail_remediation(
                    job.job_id,
                    self.worker_id,
                    ProjectError(
                        code=code,
                        message=f"Remediation failed during {stage.replace('_', ' ')}.",
                        requestId=job.job_id,
                        details={"stage": stage},
                    ),
                    datetime.now(UTC),
                )
                cleanup_safe = True
            except Exception:
                logger.error("job_failure_persistence_failed", extra={"jobId": job.job_id})
        finally:
            if stored and not published and cleanup_safe and candidate_hash is not None:
                try:
                    blobs.delete_uncommitted_source(job.blob_key, candidate_hash)
                except Exception:
                    logger.error("candidate_rollback_failed", extra={"jobId": job.job_id})
        logger.info(
            "job_finished",
            extra={
                "jobId": job.job_id,
                "remediationId": job.remediation_id,
                "workbookId": job.workbook_id,
                "versionId": job.version_id,
                "sessionId": job.session_id,
                "stage": stage,
                "code": code,
                "duration": round(time.monotonic() - started, 3),
            },
        )
        return True


def run_workers(services: SystemServices, concurrency: int, stop_requested: Event) -> None:
    """Each bounded slot claims only work it can immediately execute.

    Housekeeping has its own single thread and per-iteration object budget. It
    cannot delay claims or disappear indefinitely while every slot is occupied.
    A failing slot stops the pool; restart/lease recovery handles unfinished work.
    """
    if concurrency not in (1, 2):
        raise ValueError("Worker concurrency must be one or two")

    def slot() -> None:
        worker = AuditWorker(services)
        try:
            while not stop_requested.is_set():
                if not worker.run_once(cleanup_when_idle=False, stop_requested=stop_requested):
                    stop_requested.wait(0.25)
        except BaseException as exc:
            stop_requested.set()
            logger.error("worker_slot_failed", extra={"exceptionType": type(exc).__name__})
            raise RuntimeError("Worker slot stopped unexpectedly") from None

    def housekeeping() -> None:
        worker = AuditWorker(services)
        try:
            while not stop_requested.is_set():
                worker._cleanup_candidates(max_items=1)
                stop_requested.wait(1)
        except BaseException as exc:
            stop_requested.set()
            logger.error("worker_housekeeper_failed", extra={"exceptionType": type(exc).__name__})
            raise RuntimeError("Worker housekeeping stopped unexpectedly") from None

    with ThreadPoolExecutor(
        max_workers=concurrency + 1, thread_name_prefix="workbook-worker"
    ) as pool:
        futures = [pool.submit(slot) for _ in range(concurrency)] + [pool.submit(housekeeping)]
        for future in as_completed(futures):
            future.result()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    if args.once and args.concurrency != 1:
        parser.error("--once processes one job and requires --concurrency 1")
    from app.services.logging import configure_logging

    configure_logging()
    services = assemble(SystemSettings())
    if services.runner is None or services.engine is None:
        raise SystemExit(
            "Worker unavailable: configure Dylan ENGINE_FACTORY and Ryan RUNNER_FACTORY"
        )
    assert services.repository is not None
    services.repository.initialize()
    if args.once:
        AuditWorker(services).run_once()
        return
    stop_requested = Event()

    def stop(signum: int, _frame: FrameType | None) -> None:
        stop_requested.set()
        logger.info("worker_stopping", extra={"code": f"SIGNAL_{signum}"})

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    logger.info("worker_started", extra={"code": f"CONCURRENCY_{args.concurrency}"})
    try:
        run_workers(services, args.concurrency, stop_requested)
    except Exception:
        raise SystemExit(1) from None
    finally:
        stop_requested.set()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    logger.info("worker_stopped", extra={"code": "DRAINED"})


if __name__ == "__main__":
    main()
