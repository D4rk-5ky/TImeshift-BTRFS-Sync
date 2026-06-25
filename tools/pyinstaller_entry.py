"""Small PyInstaller entry point for building the ts-btrfs executable.

PyInstaller works best when it receives a normal Python script. This tiny file
only imports the real CLI and exits with its return code. Keeping it separate
avoids adding PyInstaller-specific code to the application package itself.
"""

from __future__ import annotations

from timeshift_btrfs_sync.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
