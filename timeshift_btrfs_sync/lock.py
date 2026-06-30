"""Simple file lock for sync/prune commands."""

from __future__ import annotations

from pathlib import Path
import fcntl


class FileLock:
    """flock() based non-blocking exclusive lock."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self):
        if not self.path.parent.is_dir():
            raise FileNotFoundError(
                f"Lock directory does not exist or is not a directory: {self.path.parent}. "
                "Real sync/prune should create it during lock path preflight before FileLock opens the lock file."
            )
        self._fh = self.path.open("w")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._fh.write("locked\n")
        self._fh.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fh:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
