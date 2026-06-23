"""Allow the app to be started with `python -m timeshift_btrfs_sync`.

Python executes this file when the package is run as a module. We simply call
the normal CLI entry point and convert its return code into the process exit
code.
"""

from .cli import main


# This guard prevents the CLI from running if the module is only imported.
if __name__ == "__main__":
    raise SystemExit(main())
