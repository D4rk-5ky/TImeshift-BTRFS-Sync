"""Btrfs wrappers.

Source-side commands in this module are limited to passwordless `btrfs` only.
There is intentionally no source-side sudo mkdir, sudo cat, helper script, or
remote Python execution.
"""

from __future__ import annotations

from pathlib import Path
from .commands import quote_join, run_local, sudo_prefix
from .models import SubvolumeMeta
from .ssh import SSHRunner

UUID_KEYS = {
    "UUID": "uuid",
    "Parent UUID": "parent_uuid",
    "Received UUID": "received_uuid",
}


def _clean_uuid(value: str) -> str | None:
    value = value.strip()
    return None if not value or value == "-" else value


def parse_subvolume_show(output: str, name: str, path: str) -> SubvolumeMeta:
    """Parse the UUID fields from `btrfs subvolume show`."""

    meta = SubvolumeMeta(name=name, path=path)
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        attr = UUID_KEYS.get(key.strip())
        if attr:
            setattr(meta, attr, _clean_uuid(value))
    return meta


def parse_property_ro(output: str) -> bool | None:
    """Parse `ro=true` / `ro=false`."""

    for line in output.splitlines():
        line = line.strip().lower()
        if line == "ro=true":
            return True
        if line == "ro=false":
            return False
    return None


def remote_btrfs_cmd(sudo: str, btrfs_command: str, args: list[str]) -> str:
    """Build a remote command that only invokes sudo+btrfs."""

    return quote_join(sudo_prefix(sudo) + [btrfs_command] + args)


def remote_subvolume_show(ssh: SSHRunner, sudo: str, btrfs_command: str, path: str, name: str) -> SubvolumeMeta:
    cmd = remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "show", path])
    result = ssh.run(cmd)
    return parse_subvolume_show(result.stdout, name=name, path=path)


def remote_try_subvolume_show(ssh: SSHRunner, sudo: str, btrfs_command: str, path: str, name: str) -> SubvolumeMeta | None:
    """Like remote_subvolume_show, but return None if the path is not a subvolume."""

    cmd = remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "show", path])
    result = ssh.run(cmd, check=False)
    if result.returncode != 0:
        return None
    return parse_subvolume_show(result.stdout, name=name, path=path)


def local_subvolume_show(path: Path, sudo: str, name: str, btrfs_command: str = "btrfs") -> SubvolumeMeta:
    cmd = sudo_prefix(sudo) + [btrfs_command, "subvolume", "show", str(path)]
    result = run_local(cmd)
    return parse_subvolume_show(result.stdout, name=name, path=str(path))


def remote_readonly(ssh: SSHRunner, sudo: str, btrfs_command: str, path: str) -> bool | None:
    cmd = remote_btrfs_cmd(sudo, btrfs_command, ["property", "get", "-ts", path, "ro"])
    result = ssh.run(cmd, check=False)
    if result.returncode != 0:
        return None
    return parse_property_ro(result.stdout)


def local_readonly(path: Path, sudo: str, btrfs_command: str = "btrfs") -> bool | None:
    cmd = sudo_prefix(sudo) + [btrfs_command, "property", "get", "-ts", str(path), "ro"]
    result = run_local(cmd, check=False)
    if result.returncode != 0:
        return None
    return parse_property_ro(result.stdout)


def _validate_cache_snapshot_name(snapshot_name: str) -> str:
    """Validate a snapshot directory name used in the source send cache.

    Snapshot names come from `timeshift --list` and normally look like
    `2026-06-22_18-00-01`. We reject path separators and dot entries so the
    destination cannot trick btrfs into creating cache snapshots elsewhere.
    """

    if not snapshot_name or "/" in snapshot_name or snapshot_name in {".", ".."}:
        raise RuntimeError(f"Unsafe snapshot name for cache path: {snapshot_name!r}")
    return snapshot_name


def _validate_cache_subvolume_name(subvolume_name: str) -> str:
    """Validate a subvolume name used in the source send cache.

    v0.3.1 intentionally supports the normal Timeshift layout (`@`, `@home`)
    and rejects nested/absolute names for the source cache. This keeps the cache
    layout recognizable and avoids needing mkdir for deeper intermediate paths.
    """

    if not subvolume_name or "/" in subvolume_name or subvolume_name in {".", ".."}:
        raise RuntimeError(f"Unsafe subvolume name for cache path: {subvolume_name!r}")
    return subvolume_name


def readonly_cache_parent_path(cache_root: str, snapshot_name: str) -> str:
    """Return the per-snapshot source cache folder path.

    This mirrors Timeshift's normal layout:
      <cache_root>/<snapshot-name>/
    """

    return str(Path(cache_root) / _validate_cache_snapshot_name(snapshot_name))


def readonly_cache_path(cache_root: str, snapshot_name: str, subvolume_name: str) -> str:
    """Return the source read-only cache path for one subvolume.

    Example:
      /timeshift-btrfs/.ts-btrfs-sync/send-cache/2026-06-22_18-00-01/@
      /timeshift-btrfs/.ts-btrfs-sync/send-cache/2026-06-22_18-00-01/@home

    The parent folder is created with `btrfs subvolume create`, not mkdir.
    """

    return str(Path(readonly_cache_parent_path(cache_root, snapshot_name)) / _validate_cache_subvolume_name(subvolume_name))


def remote_ensure_readonly_send_path(
    ssh: SSHRunner,
    *,
    sudo: str,
    btrfs_command: str,
    original_path: str,
    cache_root: str | None,
    snapshot_name: str,
    subvolume_name: str,
    create_readonly_cache: bool,
) -> str:
    """Return a source path that can be used with `btrfs send`.

    If the original Timeshift subvolume is read-only, it is used directly.
    If it is writable, we create/use this Timeshift-like cache layout:

      <cache_root>/<snapshot-name>/<subvolume>

    The per-snapshot parent is created with `btrfs subvolume create`, not mkdir,
    so source-side sudo is still limited to btrfs/timeshift only. The top-level
    cache_root must be created manually once by the admin.
    """

    ro = remote_readonly(ssh, sudo, btrfs_command, original_path)
    if ro is True:
        return original_path

    if not create_readonly_cache:
        raise RuntimeError(f"Source subvolume is not read-only and cache creation is disabled: {original_path}")
    if not cache_root:
        raise RuntimeError("Source subvolume is writable and source.cache_root is not configured")

    cache_parent = readonly_cache_parent_path(cache_root, snapshot_name)
    cache_path = readonly_cache_path(cache_root, snapshot_name, subvolume_name)

    existing = remote_try_subvolume_show(ssh, sudo, btrfs_command, cache_path, subvolume_name)
    if existing:
        return cache_path

    # Keep the layout recognizable by creating <cache_root>/<snapshot-name>/ as
    # a Btrfs subvolume. This uses only btrfs, not mkdir. If that path already
    # exists as a normal directory created manually, this command fails, but the
    # child snapshot below may still succeed, so this failure is not fatal.
    create_parent_cmd = remote_btrfs_cmd(
        sudo,
        btrfs_command,
        ["subvolume", "create", cache_parent],
    )
    ssh.run(create_parent_cmd, check=False)

    # This is the only source-side write in the app, and it is done by btrfs
    # itself. The top-level cache_root must be created manually once by admin.
    cmd = remote_btrfs_cmd(
        sudo,
        btrfs_command,
        ["subvolume", "snapshot", "-r", original_path, cache_path],
    )
    result = ssh.run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to create read-only source cache snapshot. "
            "Make sure source.cache_root exists on the source and that the "
            "per-snapshot cache path is not blocked by a file.\n"
            + result.stderr.strip()
        )
    return cache_path


def remote_send_cmd(
    ssh: SSHRunner,
    *,
    sudo: str,
    btrfs_command: str,
    current_path: str,
    parent_path: str | None = None,
) -> list[str]:
    args = ["send"]
    if parent_path:
        args += ["-p", parent_path]
    args.append(current_path)
    return ssh.command(remote_btrfs_cmd(sudo, btrfs_command, args))


def local_receive_cmd(destination_dir: Path, sudo: str, btrfs_command: str = "btrfs") -> list[str]:
    return sudo_prefix(sudo) + [btrfs_command, "receive", str(destination_dir)]


def delete_local_subvolume(path: Path, sudo: str, btrfs_command: str = "btrfs") -> None:
    run_local(sudo_prefix(sudo) + [btrfs_command, "subvolume", "delete", str(path)])
