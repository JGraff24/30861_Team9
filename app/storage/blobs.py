"""Immutable source adapters. Constructors perform no filesystem/network I/O."""

import base64
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from app.contracts.errors import IntegrityError, StorageError

MAX_SOURCE_BYTES = 25_000_000
KEY_PATTERN = re.compile(
    r"(?:ordinary|tests/[A-Za-z0-9_-]{1,64})/"
    r"[A-Za-z0-9_-]{1,64}/[A-Za-z0-9_-]{1,64}/source"
)


def check_key(key: str) -> None:
    if KEY_PATTERN.fullmatch(key) is None:
        raise IntegrityError("Invalid server source key")


def check_test_key(key: str, session_id: str) -> None:
    check_key(key)
    if not key.startswith(f"tests/{session_id}/"):
        raise IntegrityError("Source is outside the requested test namespace")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FilesystemBlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, key: str) -> Path:
        check_key(key)
        root = self.root.resolve()
        path = root / key
        if not path.resolve().is_relative_to(root):
            raise IntegrityError("Source path escapes storage root")
        return path

    def health(self) -> bool:
        return self.root.is_dir() and os.access(self.root, os.R_OK | os.W_OK | os.X_OK)

    def put_source(self, key: str, data: bytes) -> str:
        if len(data) > MAX_SOURCE_BYTES:
            raise StorageError("Source exceeds storage limit")
        path = self._path(key)
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # A hard link publishes complete, fsynced bytes without replacing an existing key.
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return sha256(data)
        except OSError as exc:
            raise StorageError("Source could not be stored immutably") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def read_source(self, key: str) -> bytes:
        try:
            with self._path(key).open("rb") as handle:
                data = handle.read(MAX_SOURCE_BYTES + 1)
            if len(data) > MAX_SOURCE_BYTES:
                raise IntegrityError("Stored source exceeds size bound")
            return data
        except OSError as exc:
            raise StorageError("Source could not be read") from exc

    def delete_uncommitted_source(self, key: str, expected_sha256: str) -> None:
        path = self._path(key)
        if not path.exists():
            return
        if sha256(self.read_source(key)) != expected_sha256:
            raise IntegrityError("Rollback source hash differs")
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError("Source rollback failed") from exc

    def delete_test_source(self, key: str, session_id: str) -> None:
        check_test_key(key, session_id)
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError("Test source cleanup failed") from exc


class S3BlobStore:
    def __init__(
        self, bucket: str, region: str, prefix: str = "sources/", *, client: Any = None
    ) -> None:
        if not prefix or prefix.startswith("/") or ".." in prefix.split("/"):
            raise ValueError("Invalid S3 prefix")
        self.bucket = bucket
        self.region = region
        self.prefix = prefix.rstrip("/") + "/"
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            # SDK role credential chain; never accept static keys in application settings.
            import boto3  # type: ignore[import-untyped]
            from botocore.config import Config  # type: ignore[import-untyped]

            session = boto3.Session(region_name=self.region)
            credentials = session.get_credentials()
            if credentials is None or credentials.method not in {"iam-role", "container-role"}:
                raise StorageError("S3 requires workload IAM role credentials")
            self._client = session.client(
                "s3",
                config=Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 0}),
            )
        return self._client

    def _key(self, key: str) -> str:
        check_key(key)
        return self.prefix + key

    def health(self) -> bool:
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return True
        except Exception:
            return False

    def put_source(self, key: str, data: bytes) -> str:
        full_key = self._key(key)
        if len(data) > MAX_SOURCE_BYTES:
            raise StorageError("Source exceeds storage limit")
        digest = sha256(data)
        checksum = base64.b64encode(bytes.fromhex(digest)).decode("ascii")
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=full_key,
                Body=data,
                IfNoneMatch="*",
                ServerSideEncryption="AES256",
                ChecksumSHA256=checksum,
                Metadata={"sha256": digest},
                ContentType="application/octet-stream",
            )
            return digest
        except Exception as exc:
            raise StorageError("S3 immutable source write failed") from exc

    def read_source(self, key: str) -> bytes:
        full_key = self._key(key)
        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=full_key, ChecksumMode="ENABLED"
            )
            body = response["Body"]
            try:
                data: bytes = body.read(MAX_SOURCE_BYTES + 1)
            finally:
                body.close()
            if len(data) > MAX_SOURCE_BYTES:
                raise IntegrityError("Stored source exceeds size bound")
            digest = sha256(data)
            if response.get("Metadata", {}).get("sha256") != digest:
                raise IntegrityError("S3 source metadata checksum differs")
            checksum = response.get("ChecksumSHA256")
            if checksum and checksum != base64.b64encode(bytes.fromhex(digest)).decode("ascii"):
                raise IntegrityError("S3 source checksum differs")
            return data
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("S3 source read failed") from exc

    def _delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            raise StorageError("S3 source cleanup failed") from exc

    def delete_uncommitted_source(self, key: str, expected_sha256: str) -> None:
        self._key(key)
        try:
            data = self.read_source(key)
        except StorageError as exc:
            cause = exc.__cause__
            if getattr(cause, "response", {}).get("Error", {}).get("Code") == "NoSuchKey":
                return
            raise
        if sha256(data) != expected_sha256:
            raise IntegrityError("Rollback source hash differs")
        self._delete(key)

    def delete_test_source(self, key: str, session_id: str) -> None:
        check_test_key(key, session_id)
        self._delete(key)
