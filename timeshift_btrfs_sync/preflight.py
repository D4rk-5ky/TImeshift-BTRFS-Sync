"""Sync path preflight checks.

The sync command must not create a fresh Timeshift on-demand snapshot, create
source cache snapshots, or start a send/receive pipeline until the configured
source and destination roots are reachable. These checks intentionally use the
configured Btrfs commands instead of generic sudo mkdir/test/rm permissions, so
they preserve the app's narrow sudo model.
"""

from __future__ import annotations

from dataclasses import dataclass
import shlex
import subprocess

from .commands import sudo_prefix
from .config import AppConfig
from .source import SourceRunner


class PathPreflightError(RuntimeError):
    """Raised before any destructive/creating sync work when required paths fail."""


@dataclass(slots=True)
class PathCheck:
    """One configured path availability result."""

    label: str
    path: str
    location: str
    ok: bool
    detail: str = ""


def _btrfs_path_check_script(checks: list[tuple[str, str]], *, sudo: str, btrfs_command: str) -> str:
    """Build a POSIX shell script that checks several paths in one process.

    The script verifies that ``btrfs subvolume list -o <path>`` can access each
    path. The command succeeds for an existing path inside a Btrfs filesystem,
    whether that path is itself a subvolume or an ordinary directory. That makes
    it suitable for Timeshift's snapshot_root, which may be a normal directory.
    """

    sudo_words = " ".join(shlex.quote(part) for part in sudo_prefix(sudo))
    lines = [
        f"sudo_words={shlex.quote(sudo_words)}",
        f"btrfs_cmd={shlex.quote(btrfs_command)}",
        r"""
run_btrfs() {
    if [ -n "$sudo_words" ]; then
        # shellcheck disable=SC2086
        $sudo_words "$btrfs_cmd" "$@"
    else
        "$btrfs_cmd" "$@"
    fi
}

check_path() {
    label=$1
    path=$2
    err_file=$(mktemp) || exit 2
    if run_btrfs subvolume list -o "$path" >/dev/null 2>"$err_file"; then
        printf 'TSBTRFS_PATH_OK\t%s\t%s\n' "$label" "$path"
    else
        status=$?
        detail=$(tr '\n' ' ' < "$err_file" | sed 's/[[:space:]][[:space:]]*/ /g')
        printf 'TSBTRFS_PATH_FAIL\t%s\t%s\t%s\t%s\n' "$label" "$path" "$status" "$detail"
    fi
    rm -f "$err_file"
}
""".strip(),
    ]
    for label, path in checks:
        lines.append(f"check_path {shlex.quote(label)} {shlex.quote(path)}")
    return "\n".join(lines)


def _parse_path_check_output(output: str, *, location: str) -> list[PathCheck]:
    """Parse path check sentinel lines."""

    results: list[PathCheck] = []
    for line in output.splitlines():
        if line.startswith("TSBTRFS_PATH_OK\t"):
            _marker, label, path = line.split("\t", 2)
            results.append(PathCheck(label=label, path=path, location=location, ok=True))
            continue
        if line.startswith("TSBTRFS_PATH_FAIL\t"):
            parts = line.split("\t", 4)
            if len(parts) >= 5:
                _marker, label, path, status, detail = parts
                results.append(
                    PathCheck(
                        label=label,
                        path=path,
                        location=location,
                        ok=False,
                        detail=f"btrfs access failed with exit {status}: {detail.strip()}",
                    )
                )
    return results


def _source_path_checks(config: AppConfig, source: SourceRunner) -> list[PathCheck]:
    """Check source.snapshot_root and source.cache_root in one source command."""

    checks: list[tuple[str, str]] = [("source.snapshot_root", config.source.snapshot_root)]
    if config.source.cache_root:
        checks.append(("source.cache_root", config.source.cache_root))

    script = _btrfs_path_check_script(checks, sudo=config.source.sudo, btrfs_command=config.source.btrfs_command)
    result = source.run("sh -c " + shlex.quote(script), check=False, log_stderr=False, mirror_stderr=False)
    parsed = _parse_path_check_output(result.stdout, location=source.location)
    seen = {item.label for item in parsed}

    # If SSH or the script failed before printing structured lines, fail every
    # missing result explicitly. This avoids silently continuing before a manual
    # snapshot just because the preflight command itself broke.
    if result.returncode != 0 or len(seen) != len(checks):
        detail = (result.stderr.strip() or result.stdout.strip() or f"return code {result.returncode}").strip()
        for label, path in checks:
            if label not in seen:
                parsed.append(PathCheck(label=label, path=path, location=source.location, ok=False, detail=detail))
    return parsed


def _local_target_path_check(config: AppConfig, *, dry_run: bool) -> list[PathCheck]:
    """Check destination.target_root locally without creating anything."""

    path = config.destination.target_root

    if dry_run and config.destination.create_target_root and not path.exists():
        return [
            PathCheck(
                label="destination.target_root",
                path=str(path),
                location="local",
                ok=True,
                detail="missing now; real sync would create it before this preflight",
            )
        ]

    if not path.exists():
        return [PathCheck(label="destination.target_root", path=str(path), location="local", ok=False, detail="path does not exist")]
    if not path.is_dir():
        return [PathCheck(label="destination.target_root", path=str(path), location="local", ok=False, detail="path exists but is not a directory")]

    script = _btrfs_path_check_script(
        [("destination.target_root", str(path))],
        sudo=config.destination.sudo,
        btrfs_command=config.destination.btrfs_command,
    )
    result = subprocess.run(
        ["sh", "-c", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    parsed = _parse_path_check_output(result.stdout, location="local")
    if result.returncode != 0 or not parsed:
        detail = (result.stderr.strip() or result.stdout.strip() or f"return code {result.returncode}").strip()
        return [PathCheck(label="destination.target_root", path=str(path), location="local", ok=False, detail=detail)]
    return parsed


def check_required_sync_paths(config: AppConfig, source: SourceRunner, *, dry_run: bool) -> list[PathCheck]:
    """Verify required configured roots before manual snapshot creation or send.

    The check runs before automatic/manual on-demand creation and before
    send/receive work. It requires:

    * source.snapshot_root on the source endpoint
    * source.cache_root on the source endpoint when configured
    * destination.target_root locally

    Failing early avoids creating a new on-demand Timeshift snapshot when the app
    could not have used the configured cache or destination paths anyway.
    Source checks run through SSH in ssh mode and as local commands in local mode.
    """

    results = _source_path_checks(config, source)
    results.extend(_local_target_path_check(config, dry_run=dry_run))

    print("SYNC PATH PREFLIGHT")
    for item in results:
        status = "OK" if item.ok else "FAIL"
        print(f"  {item.label:<24} {status}")
        print(f"    location: {item.location}")
        print(f"    path:     {item.path}")
        if item.detail:
            print(f"    detail:   {item.detail}")
    print("  purpose: verify snapshot_root, cache_root, and target_root before on-demand creation or send")
    print("----")

    failures = [item for item in results if not item.ok]
    if failures:
        details = "\n".join(f"- {item.label}: {item.path}: {item.detail or 'not available'}" for item in failures)
        raise PathPreflightError(
            "Required sync path preflight failed before creating an on-demand snapshot or starting send/receive.\n"
            + details
        )
    return results
