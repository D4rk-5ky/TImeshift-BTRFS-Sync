"""Sync path preflight checks.

The sync command must not create a fresh Timeshift on-demand snapshot, create
source cache snapshots, or start a send/receive pipeline until the configured
source and destination roots are reachable.

Real-run preflight is also the path-creation gate. The lock file parent is
prepared first, before source and destination checks, so only one real job can
run against a destination. If a configured path is missing, preflight attempts
to create exactly that configured path using the safest command that matches
the path type, then verifies Btrfs accessibility before sync continues:

* source.snapshot_root is Timeshift-owned. It must already exist as a
  directory on a Btrfs filesystem; it may be a normal directory and is never
  created by this app.
* source.cache_root is created as a Btrfs subvolume below an existing
  Btrfs-accessible parent.
* destination.target_root is created as a local Btrfs subvolume when
  destination.create_target_root is true. Existing target roots must already be
  Btrfs subvolumes and are verified with `btrfs subvolume show`.

Destination helper folders, including the lock folder, are created as Btrfs
subvolumes first and fall back to mkdir when Btrfs creation is not possible. If
any creation attempt fails, preflight raises a hard error that names the exact
configured path that could not be created.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
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


def _shell_words(parts: list[str]) -> str:
    """Return a shell-safe string for configured command-prefix words."""

    return " ".join(shlex.quote(part) for part in parts)


def _btrfs_path_check_script(checks: list[tuple[str, str]], *, sudo: str, btrfs_command: str) -> str:
    """Build a POSIX shell script that checks several paths in one process.

    The script verifies that ``btrfs subvolume list -o <path>`` can access each
    path. The command succeeds for an existing path inside a Btrfs filesystem,
    whether that path is itself a subvolume or an ordinary directory. That makes
    it suitable for Timeshift's snapshot_root, which may be a normal directory.
    """

    sudo_words = _shell_words(sudo_prefix(sudo))
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
            parts = line.split("\t", 3)
            if len(parts) >= 3:
                _marker, label, path = parts[:3]
                detail = parts[3].strip() if len(parts) > 3 else ""
                results.append(PathCheck(label=label, path=path, location=location, ok=True, detail=detail))
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


def _source_snapshot_root_script(
    snapshot_root: str,
    *,
    sudo: str,
    btrfs_command: str,
    dry_run: bool,
) -> str:
    """Build a source script that validates Timeshift-owned source.snapshot_root.

    Timeshift creates its Btrfs snapshot subvolumes below snapshot_root. The
    root path itself may be an ordinary directory on a Btrfs filesystem. The app
    must not create this path, because creating it can hide a missing Timeshift
    mount or a wrong OS/root selection. Missing snapshot_root is therefore a
    hard preflight error in both dry-run and real-run mode.
    """

    sudo_words = _shell_words(sudo_prefix(sudo))
    _ = dry_run  # snapshot_root is never created; dry-run and real-run validate identically.
    return f"""
sudo_words={shlex.quote(sudo_words)}
btrfs_cmd={shlex.quote(btrfs_command)}
snapshot_root={shlex.quote(snapshot_root)}

run_sudo_prefix() {{
    if [ -n "$sudo_words" ]; then
        # shellcheck disable=SC2086
        $sudo_words "$@"
    else
        "$@"
    fi
}}

run_btrfs() {{
    run_sudo_prefix "$btrfs_cmd" "$@"
}}

compact_error() {{
    tr '\n' ' ' < "$1" | sed 's/[[:space:]][[:space:]]*/ /g'
}}

err_file=$(mktemp) || exit 2
if [ ! -e "$snapshot_root" ]; then
    printf 'TSBTRFS_PATH_FAIL\t%s\t%s\t%s\t%s\n' 'source.snapshot_root' "$snapshot_root" 1 'missing; this is Timeshift-owned and must already exist. Mount/fix the Timeshift Btrfs snapshot root instead of letting the app create it.'
elif [ ! -d "$snapshot_root" ]; then
    printf 'TSBTRFS_PATH_FAIL\t%s\t%s\t%s\t%s\n' 'source.snapshot_root' "$snapshot_root" 1 'path exists but is not a directory'
elif run_btrfs subvolume list -o "$snapshot_root" >/dev/null 2>"$err_file"; then
    printf 'TSBTRFS_PATH_OK\t%s\t%s\t%s\n' 'source.snapshot_root' "$snapshot_root" 'exists as Timeshift-owned directory and is Btrfs-accessible; ordinary directory is allowed'
else
    status=$?
    detail=$(compact_error "$err_file")
    printf 'TSBTRFS_PATH_FAIL\t%s\t%s\t%s\t%s\n' 'source.snapshot_root' "$snapshot_root" "$status" "path exists but is not Btrfs-accessible: $detail"
fi
rm -f "$err_file"
""".strip()

def _cache_root_check_script(
    cache_root: str,
    *,
    sudo: str,
    btrfs_command: str,
    create_readonly_cache: bool,
    dry_run: bool,
) -> str:
    """Build a source script that validates or creates source.cache_root."""

    sudo_words = _shell_words(sudo_prefix(sudo))
    can_create = "1" if create_readonly_cache else "0"
    may_create = "0" if dry_run else "1"
    return f"""
sudo_words={shlex.quote(sudo_words)}
btrfs_cmd={shlex.quote(btrfs_command)}
cache_root={shlex.quote(cache_root)}
can_create={can_create}
may_create={may_create}

run_btrfs() {{
    if [ -n "$sudo_words" ]; then
        # shellcheck disable=SC2086
        $sudo_words "$btrfs_cmd" "$@"
    else
        "$btrfs_cmd" "$@"
    fi
}}

compact_error() {{
    tr '\n' ' ' < "$1" | sed 's/[[:space:]][[:space:]]*/ /g'
}}

parent_of() {{
    value=$1
    parent=${{value%/*}}
    [ -n "$parent" ] || parent=/
    [ "$parent" = "$value" ] && parent=/
    printf '%s\n' "$parent"
}}

err_file=$(mktemp) || exit 2
if run_btrfs subvolume show "$cache_root" >/dev/null 2>"$err_file"; then
    printf 'TSBTRFS_PATH_OK\t%s\t%s\t%s\n' 'source.cache_root' "$cache_root" 'exists as Btrfs subvolume'
elif [ -e "$cache_root" ]; then
    detail=$(compact_error "$err_file")
    printf 'TSBTRFS_PATH_FAIL\t%s\t%s\t%s\t%s\n' 'source.cache_root' "$cache_root" 1 "path exists but is not a Btrfs subvolume: $detail"
elif [ "$can_create" != "1" ]; then
    detail=$(compact_error "$err_file")
    printf 'TSBTRFS_PATH_FAIL\t%s\t%s\t%s\t%s\n' 'source.cache_root' "$cache_root" 1 "missing and source.create_readonly_cache is false: $detail"
else
    parent=$(parent_of "$cache_root")
    if [ "$may_create" != "1" ]; then
        printf 'TSBTRFS_PATH_OK\t%s\t%s\t%s\n' 'source.cache_root' "$cache_root" "missing now; real preflight would create this Btrfs subvolume after verifying Btrfs parent $parent"
    elif ! run_btrfs subvolume list -o "$parent" >/dev/null 2>"$err_file"; then
        status=$?
        detail=$(compact_error "$err_file")
        printf 'TSBTRFS_PATH_FAIL\t%s\t%s\t%s\t%s\n' 'source.cache_root' "$cache_root" "$status" "could not create source.cache_root because parent is not Btrfs-accessible: $parent: $detail"
    elif run_btrfs subvolume create "$cache_root" >/dev/null 2>"$err_file"; then
        if run_btrfs subvolume show "$cache_root" >/dev/null 2>"$err_file"; then
            printf 'TSBTRFS_PATH_OK\t%s\t%s\t%s\n' 'source.cache_root' "$cache_root" 'created Btrfs subvolume and verified it'
        else
            status=$?
            detail=$(compact_error "$err_file")
            printf 'TSBTRFS_PATH_FAIL\t%s\t%s\t%s\t%s\n' 'source.cache_root' "$cache_root" "$status" "created Btrfs subvolume but verification failed: $detail"
        fi
    else
        status=$?
        detail=$(compact_error "$err_file")
        printf 'TSBTRFS_PATH_FAIL\t%s\t%s\t%s\t%s\n' 'source.cache_root' "$cache_root" "$status" "could not create Btrfs subvolume: $detail"
    fi
fi
rm -f "$err_file"
""".strip()


def _source_path_checks(config: AppConfig, source: SourceRunner, *, dry_run: bool) -> list[PathCheck]:
    """Check/create source.snapshot_root and source.cache_root policy."""

    parsed: list[PathCheck] = []
    snapshot_script = _source_snapshot_root_script(
        config.source.snapshot_root,
        sudo=config.source.sudo,
        btrfs_command=config.source.btrfs_command,
        dry_run=dry_run,
    )
    snapshot_result = source.run("sh -c " + shlex.quote(snapshot_script), check=False, log_stderr=False, mirror_stderr=False)
    snapshot_parsed = _parse_path_check_output(snapshot_result.stdout, location=source.location)
    if snapshot_result.returncode != 0 or not any(item.label == "source.snapshot_root" for item in snapshot_parsed):
        detail = (snapshot_result.stderr.strip() or snapshot_result.stdout.strip() or f"return code {snapshot_result.returncode}").strip()
        snapshot_parsed.append(
            PathCheck(
                label="source.snapshot_root",
                path=config.source.snapshot_root,
                location=source.location,
                ok=False,
                detail=detail,
            )
        )
    parsed.extend(snapshot_parsed)

    if config.source.cache_root:
        cache_script = _cache_root_check_script(
            config.source.cache_root,
            sudo=config.source.sudo,
            btrfs_command=config.source.btrfs_command,
            create_readonly_cache=config.source.create_readonly_cache,
            dry_run=dry_run,
        )
        cache_result = source.run("sh -c " + shlex.quote(cache_script), check=False, log_stderr=False, mirror_stderr=False)
        cache_parsed = _parse_path_check_output(cache_result.stdout, location=source.location)
        if cache_result.returncode != 0 or not any(item.label == "source.cache_root" for item in cache_parsed):
            detail = (cache_result.stderr.strip() or cache_result.stdout.strip() or f"return code {cache_result.returncode}").strip()
            cache_parsed.append(
                PathCheck(
                    label="source.cache_root",
                    path=config.source.cache_root,
                    location=source.location,
                    ok=False,
                    detail=detail,
                )
            )
        parsed.extend(cache_parsed)
    return parsed


def _parent_of_path(path: Path) -> Path:
    """Return the immediate parent path used for exact-path creation checks."""

    parent = path.parent
    return parent if str(parent) else Path("/")


def _local_btrfs_result(config: AppConfig, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one local destination sudo+btrfs command for preflight checks."""

    return subprocess.run(
        sudo_prefix(config.destination.sudo) + [config.destination.btrfs_command] + args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _compact_process_error(result: subprocess.CompletedProcess[str]) -> str:
    """Return compact stderr/stdout text from a failed subprocess."""

    detail = result.stderr.strip() or result.stdout.strip() or f"return code {result.returncode}"
    return " ".join(detail.split())


def _compact_os_error(exc: OSError) -> str:
    """Return compact text for local filesystem creation errors."""

    return " ".join(str(exc).split())


def _print_check_block(title: str, results: list[PathCheck], *, purpose: str) -> None:
    """Print one human-readable preflight result block."""

    print(title)
    for item in results:
        status = "OK" if item.ok else "FAIL"
        print(f"  {item.label:<24} {status}")
        print(f"    location: {item.location}")
        print(f"    path:     {item.path}")
        if item.detail:
            print(f"    detail:   {item.detail}")
    print(f"  purpose: {purpose}")
    print("----")


def _raise_for_failed_checks(results: list[PathCheck], *, heading: str, fix_text: str) -> None:
    """Raise a hard preflight error when any check failed."""

    failures = [item for item in results if not item.ok]
    if not failures:
        return
    details = "\n".join(f"- {item.label}: {item.path}: {item.detail or 'not available'}" for item in failures)
    raise PathPreflightError(f"{heading}\n{fix_text}\n" + details)


def ensure_local_helper_dir(config: AppConfig, label: str, path: str | Path, *, dry_run: bool) -> PathCheck:
    """Ensure one local helper directory exists.

    Existing normal directories and existing Btrfs subvolumes are both accepted.
    When the path is missing in a real run, the app tries to create the exact
    path as a Btrfs subvolume first because the project primarily manages Btrfs
    backup storage. If Btrfs creation fails, it falls back to ordinary mkdir so
    helper paths can still live on non-Btrfs locations when configured that way.
    Parent directories are not invented; the immediate parent must already
    exist. This prevents a missing target root from being silently created as
    ordinary directories.
    """

    helper_path = Path(path).expanduser()
    path_text = str(helper_path)

    if helper_path.exists():
        if not helper_path.is_dir():
            return PathCheck(label=label, path=path_text, location="local", ok=False, detail="path exists but is not a directory")
        show_result = _local_btrfs_result(config, ["subvolume", "show", path_text])
        detail = "exists as Btrfs subvolume" if show_result.returncode == 0 else "exists as directory"
        if not os.access(path_text, os.W_OK | os.X_OK):
            return PathCheck(
                label=label,
                path=path_text,
                location="local",
                ok=False,
                detail=(
                    detail + "; app user cannot write inside this helper path. "
                    "Use a writable path, fix ownership/permissions, or create the helper subvolume with ownership suitable for the app user."
                ),
            )
        return PathCheck(label=label, path=path_text, location="local", ok=True, detail=detail)

    parent = _parent_of_path(helper_path)
    parent_text = str(parent)
    if dry_run:
        return PathCheck(
            label=label,
            path=path_text,
            location="local",
            ok=True,
            detail=f"missing now; real run would try Btrfs subvolume create first, then mkdir fallback, after verifying parent {parent_text}",
        )

    if not parent.exists():
        return PathCheck(label=label, path=path_text, location="local", ok=False, detail=f"could not create helper path because parent does not exist: {parent_text}")
    if not parent.is_dir():
        return PathCheck(label=label, path=path_text, location="local", ok=False, detail=f"could not create helper path because parent is not a directory: {parent_text}")

    create_result = _local_btrfs_result(config, ["subvolume", "create", path_text])
    if create_result.returncode == 0:
        verify_result = _local_btrfs_result(config, ["subvolume", "show", path_text])
        if verify_result.returncode != 0:
            return PathCheck(
                label=label,
                path=path_text,
                location="local",
                ok=False,
                detail=f"created helper path as Btrfs subvolume but verification failed: {_compact_process_error(verify_result)}",
            )
        if not os.access(path_text, os.W_OK | os.X_OK):
            return PathCheck(
                label=label,
                path=path_text,
                location="local",
                ok=False,
                detail=(
                    "created helper path as Btrfs subvolume, but the app user cannot write inside it. "
                    "This commonly happens when sudo btrfs creates a root-owned subvolume; fix ownership/permissions or configure the path elsewhere."
                ),
            )
        return PathCheck(label=label, path=path_text, location="local", ok=True, detail="created Btrfs subvolume")

    try:
        helper_path.mkdir(mode=0o755, exist_ok=True)
    except OSError as mkdir_exc:
        return PathCheck(
            label=label,
            path=path_text,
            location="local",
            ok=False,
            detail=(
                "could not create helper path with Btrfs subvolume create or mkdir: "
                f"btrfs error: {_compact_process_error(create_result)}; mkdir error: {_compact_os_error(mkdir_exc)}"
            ),
        )

    if helper_path.is_dir():
        if not os.access(path_text, os.W_OK | os.X_OK):
            return PathCheck(
                label=label,
                path=path_text,
                location="local",
                ok=False,
                detail=(
                    "created directory after Btrfs subvolume create failed, but the app user cannot write inside it; "
                    "fix ownership/permissions or configure the path elsewhere. "
                    f"Btrfs error was: {_compact_process_error(create_result)}"
                ),
            )
        return PathCheck(
            label=label,
            path=path_text,
            location="local",
            ok=True,
            detail="created directory after Btrfs subvolume create failed: " + _compact_process_error(create_result),
        )
    return PathCheck(
        label=label,
        path=path_text,
        location="local",
        ok=False,
        detail="mkdir returned successfully but path is not a directory after Btrfs subvolume create failed: " + _compact_process_error(create_result),
    )

def _path_is_within(child: Path, parent: Path) -> bool:
    """Return True when child is parent or below parent after path normalization."""

    try:
        child.expanduser().resolve(strict=False).relative_to(parent.expanduser().resolve(strict=False))
        return True
    except ValueError:
        return False


def prepare_lock_path(config: AppConfig, *, dry_run: bool = False) -> list[PathCheck]:
    """Create/verify the lock directory before other sync/prune directories.

    The lock is the first concurrency gate for real sync/prune runs. The app
    prepares the lock file parent before checking snapshots, state, log, source,
    or other destination helper folders, then the CLI acquires the lock. If the
    lock path lives below destination.target_root and that target root is
    missing, the target root is created first as the minimum prerequisite because
    a child lock folder cannot exist before its parent Btrfs subvolume exists.
    """

    results: list[PathCheck] = []
    lock_parent = config.lock_file.parent
    target_root = config.destination.target_root

    if _path_is_within(lock_parent, target_root) and not target_root.exists():
        results.extend(_local_target_path_check(config, dry_run=dry_run))

    results.append(ensure_local_helper_dir(config, "lock_file.parent", lock_parent, dry_run=dry_run))

    _print_check_block(
        "LOCK PATH PREFLIGHT",
        results,
        purpose="verify/create the lock file parent before checking other sync/prune paths; create destination.target_root first only when the lock path lives below it",
    )
    _raise_for_failed_checks(
        results,
        heading="Lock path preflight failed before acquiring the lock.",
        fix_text="The path below could not be verified or created. Fix the exact configured path before retrying.",
    )
    return results

def prepare_destination_helper_paths(config: AppConfig, *, dry_run: bool = False) -> list[PathCheck]:
    """Create/verify local destination helper folders used by sync/prune.

    Helper folders may be ordinary directories or Btrfs subvolumes. The app
    accepts either when they already exist. When missing, it attempts Btrfs
    subvolume creation first and falls back to mkdir when Btrfs creation is not
    possible at that location.
    """

    raw_paths: list[tuple[str, Path]] = [
        ("destination.snapshots", config.destination.target_root / "snapshots"),
        ("state_file.parent", config.state_file.parent),
        ("lock_file.parent", config.lock_file.parent),
    ]
    if config.log_dir is not None:
        raw_paths.append(("log_dir", config.log_dir))

    results: list[PathCheck] = []
    seen: set[str] = set()
    for label, path in raw_paths:
        key = str(path.expanduser())
        if key in seen:
            continue
        seen.add(key)
        results.append(ensure_local_helper_dir(config, label, path, dry_run=dry_run))

    _print_check_block(
        "DESTINATION HELPER PATH PREFLIGHT",
        results,
        purpose="verify/create snapshots, state, lock, and optional log helper folders before writing state or receiving data",
    )
    _raise_for_failed_checks(
        results,
        heading="Destination helper path preflight failed.",
        fix_text="The path below could not be verified or created. Fix the exact configured path before retrying.",
    )
    return results


def _local_target_path_check(config: AppConfig, *, dry_run: bool) -> list[PathCheck]:
    """Check/create destination.target_root locally.

    If the configured target root is missing and create_target_root is enabled,
    it is created as a Btrfs subvolume with the configured destination sudo+btrfs
    command. Only the exact configured target root is created; parent directories
    must already exist and must be Btrfs-accessible.

    Existing target roots are not converted. They must already be Btrfs
    subvolumes. A plain directory inside a Btrfs filesystem is refused because
    the destination root is the app-owned backup container and later receive,
    state, prune, and destroy operations depend on that exact root being a
    verified subvolume.
    """

    path = config.destination.target_root
    path_text = str(path)

    if path.exists():
        if not path.is_dir():
            return [
                PathCheck(
                    label="destination.target_root",
                    path=path_text,
                    location="local",
                    ok=False,
                    detail="path exists but is not a directory",
                )
            ]
        show_result = _local_btrfs_result(config, ["subvolume", "show", path_text])
        if show_result.returncode != 0:
            return [
                PathCheck(
                    label="destination.target_root",
                    path=path_text,
                    location="local",
                    ok=False,
                    detail="path exists but is not a Btrfs subvolume: " + _compact_process_error(show_result),
                )
            ]
        return [
            PathCheck(
                label="destination.target_root",
                path=path_text,
                location="local",
                ok=True,
                detail="exists as Btrfs subvolume",
            )
        ]

    if not config.destination.create_target_root:
        return [
            PathCheck(
                label="destination.target_root",
                path=path_text,
                location="local",
                ok=False,
                detail="path does not exist and destination.create_target_root is false",
            )
        ]

    parent = _parent_of_path(path)
    parent_text = str(parent)
    if dry_run:
        return [
            PathCheck(
                label="destination.target_root",
                path=path_text,
                location="local",
                ok=True,
                detail=f"missing now; real preflight would verify Btrfs parent {parent_text} and create this path as a Btrfs subvolume",
            )
        ]

    if not parent.exists():
        return [
            PathCheck(
                label="destination.target_root",
                path=path_text,
                location="local",
                ok=False,
                detail=f"could not create destination.target_root because parent does not exist: {parent_text}",
            )
        ]
    if not parent.is_dir():
        return [
            PathCheck(
                label="destination.target_root",
                path=path_text,
                location="local",
                ok=False,
                detail=f"could not create destination.target_root because parent is not a directory: {parent_text}",
            )
        ]

    parent_check = _local_btrfs_result(config, ["subvolume", "list", "-o", parent_text])
    if parent_check.returncode != 0:
        return [
            PathCheck(
                label="destination.target_root",
                path=path_text,
                location="local",
                ok=False,
                detail=f"could not create destination.target_root because parent is not Btrfs-accessible: {parent_text}: {_compact_process_error(parent_check)}",
            )
        ]

    create_result = _local_btrfs_result(config, ["subvolume", "create", path_text])
    if create_result.returncode != 0:
        return [
            PathCheck(
                label="destination.target_root",
                path=path_text,
                location="local",
                ok=False,
                detail=f"could not create destination.target_root as Btrfs subvolume: {_compact_process_error(create_result)}",
            )
        ]

    verify_result = _local_btrfs_result(config, ["subvolume", "show", path_text])
    if verify_result.returncode != 0:
        return [
            PathCheck(
                label="destination.target_root",
                path=path_text,
                location="local",
                ok=False,
                detail=f"created destination.target_root as Btrfs subvolume but verification failed: {_compact_process_error(verify_result)}",
            )
        ]

    return [
        PathCheck(
            label="destination.target_root",
            path=path_text,
            location="local",
            ok=True,
            detail="created Btrfs subvolume and verified it",
        )
    ]


def check_required_sync_paths(config: AppConfig, source: SourceRunner, *, dry_run: bool) -> list[PathCheck]:
    """Verify/create required configured roots before manual snapshot creation or send.

    The check runs before automatic/manual on-demand creation and before
    send/receive work. It requires:

    * source.snapshot_root on the source endpoint
    * source.cache_root on the source endpoint when configured
    * destination.target_root locally

    In real-run mode, missing configured roots are created before preflight
    succeeds. In dry-run mode, creation is only described. Source checks run
    through SSH in ssh mode and as local commands in local mode.
    """

    results = _source_path_checks(config, source, dry_run=dry_run)
    results.extend(_local_target_path_check(config, dry_run=dry_run))

    _print_check_block(
        "SYNC PATH PREFLIGHT",
        results,
        purpose="verify/create snapshot_root, cache_root, and target_root before on-demand creation or send",
    )
    _raise_for_failed_checks(
        results,
        heading="Required sync path preflight failed before creating an on-demand snapshot or starting send/receive.",
        fix_text="The path below could not be verified or created. Fix the exact configured path before retrying.",
    )
    return results
