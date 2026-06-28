#!/usr/bin/env python3
"""Build ts-btrfs executables with PyInstaller.

This helper is intentionally small. It wraps the recommended PyInstaller command
so the project has one stable place for the executable build options.

Examples:
    python3 scripts/build_pyinstaller.py --mode onedir
    python3 scripts/build_pyinstaller.py --mode onefile
    python3 scripts/build_pyinstaller.py --mode both --with-mqtt
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENTRY_SCRIPT = PROJECT_ROOT / "tools" / "pyinstaller_entry.py"


def build_args(*, mode: str, name: str, clean: bool, with_mqtt: bool, extra: list[str]) -> list[str]:
    """Return the PyInstaller argument list for one build mode."""

    args: list[str] = [
        "--name",
        name,
        "--console",
        "--paths",
        str(PROJECT_ROOT),
        "--add-data",
        f"{PROJECT_ROOT / 'timeshift_btrfs_sync' / 'data' / 'config.example.toml'}:timeshift_btrfs_sync/data",
    ]
    if clean:
        args.append("--clean")
    if mode == "onefile":
        args.append("--onefile")

    # MQTT is optional and imported lazily. If the user wants an executable that
    # can publish MQTT notifications, include paho-mqtt during analysis.
    if with_mqtt:
        args.extend(["--hidden-import", "paho.mqtt.client"])

    args.extend(extra)
    args.append(str(ENTRY_SCRIPT))
    return args


def run_pyinstaller(args: list[str]) -> None:
    """Run PyInstaller with a useful error if it is not installed."""

    try:
        from PyInstaller.__main__ import run
    except ImportError as exc:
        raise SystemExit(
            "PyInstaller is not installed. Install it in this venv with:\n"
            "  python3 -m pip install -e '.[pyinstaller]'\n"
            "or, with MQTT support included:\n"
            "  python3 -m pip install -e '.[mqtt,pyinstaller]'"
        ) from exc

    run(args)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the requested build."""

    parser = argparse.ArgumentParser(description="Build ts-btrfs with PyInstaller.")
    parser.add_argument(
        "--mode",
        choices=("onedir", "onefile", "both"),
        default="onedir",
        help="Build folder-style executable, one-file executable, or both. Default: onedir.",
    )
    parser.add_argument(
        "--name",
        default="ts-btrfs",
        help="Executable name. Default: ts-btrfs.",
    )
    parser.add_argument(
        "--with-mqtt",
        action="store_true",
        help="Include optional paho-mqtt support in the executable.",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Do not pass --clean to PyInstaller.",
    )
    parser.add_argument(
        "--extra-pyinstaller-arg",
        action="append",
        default=[],
        help="Extra raw argument passed to PyInstaller. Can be used more than once.",
    )
    ns = parser.parse_args(argv)

    modes = ["onedir", "onefile"] if ns.mode == "both" else [ns.mode]
    for mode in modes:
        print(f"\n=== Building {ns.name} ({mode}) with PyInstaller ===\n")
        args = build_args(
            mode=mode,
            name=ns.name,
            clean=not ns.no_clean,
            with_mqtt=ns.with_mqtt,
            extra=list(ns.extra_pyinstaller_arg),
        )
        run_pyinstaller(args)

    print("\nBuild output is in ./dist/")
    if "onedir" in modes:
        print(f"  ./dist/{ns.name}/{ns.name}")
    if "onefile" in modes:
        print(f"  ./dist/{ns.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
