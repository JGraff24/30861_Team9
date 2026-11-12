"""Single-host, process-wide fencing around external writes and deletion."""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.contracts.errors import ProjectException


@contextmanager
def workbook_storage_lock(database: Path, workbook_id: str) -> Iterator[None]:
    # Workbook IDs are generated/validated internally. Lock files contain no content.
    directory = database.parent / "storage-locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / workbook_id).open("a+b") as handle:
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.write(b"\0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ProjectException(409, "ACTIVE_OPERATION", "Workbook storage is active.") from None
        try:
            yield
        finally:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
