"""Focused remote-adapter housekeeping budget; no AWS requests are made."""

from unittest.mock import Mock

from app.config import SystemSettings
from app.contracts.ports import Repository, SystemServices
from app.jobs.worker import AuditWorker
from app.storage.blobs import S3BlobStore


def test_remote_cleanup_preserves_intent_without_hammering_missing_objects(monkeypatch) -> None:
    repository = Mock(spec=Repository)
    repository.pending_blob_cleanup.return_value = [("first", "a" * 64), ("second", "b" * 64)]
    blobs = S3BlobStore("unused-test-bucket", "unused-region", client=Mock())
    blobs.delete_uncommitted_source = Mock()
    worker = AuditWorker(
        SystemServices(settings=SystemSettings(), repository=repository, blob_store=blobs)
    )
    now = [100.0]
    monkeypatch.setattr("app.jobs.worker.time.monotonic", lambda: now[0])
    worker._cleanup_candidates()
    assert blobs.delete_uncommitted_source.call_count == 1
    worker._cleanup_candidates()
    assert blobs.delete_uncommitted_source.call_count == 2
    worker._cleanup_candidates()
    assert blobs.delete_uncommitted_source.call_count == 2
    now[0] += 61
    worker._cleanup_candidates()
    assert blobs.delete_uncommitted_source.call_count == 3
