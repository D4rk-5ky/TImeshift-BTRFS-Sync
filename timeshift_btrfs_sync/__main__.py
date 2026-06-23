"""Module runner for `python -m timeshift_btrfs_sync`.

Python executes this file when the package is run with `python -m`. We forward
to the normal argparse CLI and convert the returned integer into the process
exit code.
"""

from .cli import main


# Do not run the CLI merely because the module was imported by another module.
if __name__ == "__main__":
    raise SystemExit(main())
