"""Open a native app's SQLite store without changing a byte of it.

A WAL-mode database may be rewritten even by a read-only connection, and its newest rows may live
only in the ``-wal`` file. So the base file and the ``-wal`` file are copied into a fresh private
(0700) directory under the system temporary directory, the copy is opened, and the directory is
removed afterwards. The ``-shm`` file is never opened or copied. The copy counts only when the
source's size, modification time and inode are unchanged across it (retried for a store the app is
writing); otherwise the call fails rather than returning a torn read.
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

ATTEMPTS = 4
MAX_BYTES = 2 << 30


class SnapshotError(RuntimeError):
    pass


def _stamp(path: Path) -> tuple[int, int, int]:
    status = path.stat()
    return status.st_size, status.st_mtime_ns, status.st_ino


@contextlib.contextmanager
def snapshot(path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a read-only connection to a private copy of ``path`` (and its ``-wal``)."""
    path = path.expanduser()
    if not path.is_file():
        raise SnapshotError(f"database does not exist: {path}")
    log = Path(str(path) + "-wal")
    scratch = Path(tempfile.mkdtemp(prefix="cce-app-cache-"))
    scratch.chmod(0o700)
    try:
        copy = scratch / "snapshot.sqlite"
        for _ in range(ATTEMPTS):
            try:
                has_log = log.exists() and log.stat().st_size > 0
                before = (_stamp(path), _stamp(log) if has_log else None)
                total = before[0][0] + (before[1][0] if before[1] else 0)
                if total > MAX_BYTES or total > shutil.disk_usage(scratch).free // 2:
                    raise SnapshotError("database too large to snapshot")
                shutil.copyfile(path, copy)
                copied_log = Path(str(copy) + "-wal")
                copied_log.unlink(missing_ok=True)
                if has_log:
                    shutil.copyfile(log, copied_log)
                after = (_stamp(path), _stamp(log) if has_log else None)
            except FileNotFoundError:
                continue  # the app closed its last connection and checkpointed the log away
            if after == before:
                break
        else:
            raise SnapshotError("database changed during snapshot")
        connection = sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True)
        try:
            yield connection
        finally:
            connection.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
