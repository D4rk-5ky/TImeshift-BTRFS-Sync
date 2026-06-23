"""Allow running the package with: python -m timeshift_btrfs_sync."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
