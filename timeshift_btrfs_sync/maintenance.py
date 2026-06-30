"""Guarded maintenance commands for state and lock files.

These helpers intentionally operate only on the exact files configured in
``state_file`` and ``lock_file``.  They do not delete snapshots, source cache
subvolumes, destination subvolumes, or Timeshift-owned paths.  Real actions use
similar guardrails to destroy-leftovers: dry-run by default, explicit run mode,
a long danger flag, and typed confirmations.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import fcntl
import json
import os

from .config import AppConfig
from .destroy import _safe_cleanup_path


@dataclass(slots=True)
class MaintenanceResult:
    """Structured result for one maintenance-file operation."""

    label: str
    path: Path
    exists: bool
    action: str
    changed: bool = False


def _confirm_or_raise(prompt: str, expected: str) -> None:
    """Require an exact typed confirmation before destructive maintenance."""

    answer = input(prompt).strip()
    if answer != expected:
        raise RuntimeError("Confirmation did not match; maintenance command aborted")


def _safe_configured_file(path: Path, label: str) -> Path:
    """Return a normalized configured file path or raise for unsafe targets.

    The command never accepts an arbitrary CLI path.  It only operates on the
    path loaded from config.  This extra validation mirrors destroy-leftovers'
    broad-path checks and also refuses directories because these commands only
    remove individual metadata/lock files.
    """

    normalized = Path(_safe_cleanup_path(path, label))
    if normalized.exists() and normalized.is_dir():
        raise RuntimeError(f"Refusing to remove directory for {label}; expected a single file: {normalized}")
    return normalized


def _looks_like_state_file(path: Path) -> bool:
    """Return True when an existing file appears to be ts-btrfs state.

    A corrupt ``state.json`` can be removed when the filename is the standard
    state filename.  For custom filenames, require JSON with the usual state
    document shape so a bad config cannot quietly delete an unrelated file.
    """

    if not path.exists():
        return True
    if path.name == "state.json":
        return True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and "snapshots" in data and "version" in data


def _looks_like_lock_file(path: Path) -> bool:
    """Return True when an existing file looks like this app's simple lock file."""

    if not path.exists():
        return True
    try:
        if path.stat().st_size > 4096:
            return False
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False
    return text in {"", "locked"}


def _print_header(title: str, config: AppConfig, *, dry_run: bool, danger_flag: str) -> None:
    """Print the common maintenance command warning block."""

    print(title)
    print("=" * len(title))
    print("This command only targets the exact configured metadata/lock file.")
    print("It does not delete source snapshots, source cache snapshots, destination snapshots, or Timeshift-owned paths.")
    print(f"Run mode: {'dry-run' if dry_run else 'REAL FILE REMOVAL'}")
    print(f"Configured job: {config.name}")
    print(f"Real mode requires --run, {danger_flag}, and typed confirmations.")
    print()


def _require_real_confirmation(
    *,
    dry_run: bool,
    danger_confirmed: bool,
    interactive: bool,
    danger_flag_name: str,
    mode_text: str,
    job_name: str,
) -> None:
    """Require real-mode flags and typed confirmations."""

    if dry_run:
        return
    if not danger_confirmed:
        raise RuntimeError(f"Real maintenance requires {danger_flag_name}")
    if interactive:
        _confirm_or_raise(f"Type {mode_text} to continue: ", mode_text)
        _confirm_or_raise(f"Type the configured job name ({job_name}) to continue: ", job_name)


def clear_state_file(
    config: AppConfig,
    *,
    dry_run: bool,
    danger_confirmed: bool,
    interactive: bool = True,
) -> MaintenanceResult:
    """Remove the configured state.json file after explicit confirmation.

    The caller should acquire the app lock before real execution so state cannot
    be removed while sync/prune is using it.  Removing state is useful after a
    failed transfer or when you want the next sync to rebuild state from exact
    source/destination Btrfs UUID matches.
    """

    path = _safe_configured_file(config.state_file, "state_file")
    _print_header("CLEAR STATE FILE", config, dry_run=dry_run, danger_flag="--i-understand-this-clears-state")
    print("Target state_file:")
    print(f"  {path}")
    print()

    exists = path.exists()
    if exists and not _looks_like_state_file(path):
        raise RuntimeError(
            f"Refusing to clear {path}; it does not look like a ts-btrfs state file. "
            "Fix state_file in the config or remove the file manually after reviewing it."
        )

    _require_real_confirmation(
        dry_run=dry_run,
        danger_confirmed=danger_confirmed,
        interactive=interactive,
        danger_flag_name="--i-understand-this-clears-state",
        mode_text="CLEAR STATE",
        job_name=config.name,
    )

    if not exists:
        print("Result: state_file is already missing.")
        return MaintenanceResult("state_file", path, exists=False, action="already missing")

    if dry_run:
        print("Result: would remove configured state_file.")
        return MaintenanceResult("state_file", path, exists=True, action="would remove")

    path.unlink()
    print("Result: removed configured state_file.")
    return MaintenanceResult("state_file", path, exists=True, action="removed", changed=True)


def delete_lock_file(
    config: AppConfig,
    *,
    dry_run: bool,
    danger_confirmed: bool,
    interactive: bool = True,
) -> MaintenanceResult:
    """Delete the configured lock file when no running process holds it.

    The command refuses to remove a currently held lock.  It is for stale lock
    files after crashes or manual process cleanup, not for stopping a running
    sync/prune job.
    """

    path = _safe_configured_file(config.lock_file, "lock_file")
    _print_header("DELETE LOCK FILE", config, dry_run=dry_run, danger_flag="--i-understand-this-deletes-lock")
    print("Target lock_file:")
    print(f"  {path}")
    print()

    exists = path.exists()
    if exists and not _looks_like_lock_file(path):
        raise RuntimeError(
            f"Refusing to delete {path}; it does not look like a ts-btrfs lock file. "
            "Fix lock_file in the config or remove the file manually after reviewing it."
        )

    _require_real_confirmation(
        dry_run=dry_run,
        danger_confirmed=danger_confirmed,
        interactive=interactive,
        danger_flag_name="--i-understand-this-deletes-lock",
        mode_text="DELETE LOCK",
        job_name=config.name,
    )

    if not exists:
        print("Result: lock_file is already missing.")
        return MaintenanceResult("lock_file", path, exists=False, action="already missing")

    if dry_run:
        print("Result: would remove configured lock_file if it is not currently held.")
        return MaintenanceResult("lock_file", path, exists=True, action="would remove")

    with path.open("r+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                f"Refusing to delete active lock file: {path}. "
                "A ts-btrfs process still holds the lock. Stop the running process instead of deleting the lock."
            )
        os.unlink(path)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    print("Result: removed configured lock_file.")
    return MaintenanceResult("lock_file", path, exists=True, action="removed", changed=True)
