"""File lock used to prevent overlapping sync/prune runs."""

from __future__ import annotations

from pathlib import Path
import fcntl


class FileLock:
    """Context manager around flock().

    Two simultaneous Btrfs receive/delete jobs against the same destination could
    corrupt the tool's state or collide on paths. The lock makes the second run
    fail immediately instead of running concurrently.
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
        """Release the lock when the command exits."""

        if self._fh:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
