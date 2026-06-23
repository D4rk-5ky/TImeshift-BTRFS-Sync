"""Simple file lock to prevent overlapping sync/prune runs."""

from __future__ import annotations

from pathlib import Path
import fcntl


class FileLock:
    """Context manager using flock() on a configured lock file.

    Overlapping btrfs send/receive or delete operations can be dangerous. This
    lock makes a second ts-btrfs process fail fast instead of running alongside
    the first one.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self):
        """Create/open the lock file and acquire an exclusive non-blocking lock."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._fh.write("locked\n")
        self._fh.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        """Release the lock when the CLI command exits."""

        if self._fh:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
