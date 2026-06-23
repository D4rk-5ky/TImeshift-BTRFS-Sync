"""Btrfs command builders, parsers, and safety helpers.

Source-side functions in this module deliberately build commands that invoke
only `sudo btrfs ...`. There is no source-side sudo mkdir/cat/find/python/helper.
"""

from __future__ import annotations

from pathlib import Path
from .commands import quote_join, run_local, sudo_prefix
from .models import SubvolumeMeta
from .ssh import SSHRunner


# Human-readable keys from `btrfs subvolume show` mapped to our dataclass names.
UUID_KEYS = {
    "UUID": "uuid",
    "Parent UUID": "parent_uuid",
    "Received UUID": "received_uuid",
}


def _clean_uuid(value: str) -> str | None:
    """Normalize Btrfs UUID values.

    Btrfs prints `-` for missing UUID fields. The app stores those as None in
    Python/state.json.
    """

    value = value.strip()
    return None if not value or value == "-" else value


def parse_subvolume_show(output: str, name: str, path: str) -> SubvolumeMeta:
    """Parse selected fields from `btrfs subvolume show` output."""

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
    """Parse read-only status from `btrfs property get -ts <path> ro`."""

    for line in output.splitlines():
        line = line.strip().lower()
        if line == "ro=true":
            return True
        if line == "ro=false":
            return False
    return None


def remote_btrfs_cmd(sudo: str, btrfs_command: str, args: list[str]) -> str:
    """Build a quoted remote command that invokes only sudo+btrfs.

    Example result:
      sudo -n btrfs subvolume show /timeshift-btrfs/snapshots/...
    """

    return quote_join(sudo_prefix(sudo) + [btrfs_command] + args)


def remote_subvolume_show(ssh: SSHRunner, sudo: str, btrfs_command: str, path: str, name: str) -> SubvolumeMeta:
    """Read Btrfs UUID metadata for a source-side subvolume."""

    cmd = remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "show", path])
    result = ssh.run(cmd)
    return parse_subvolume_show(result.stdout, name=name, path=path)


def remote_try_subvolume_show(ssh: SSHRunner, sudo: str, btrfs_command: str, path: str, name: str) -> SubvolumeMeta | None:
    """Read source subvolume metadata, returning None if the path is not valid."""

    cmd = remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "show", path])
    result = ssh.run(cmd, check=False)
    if result.returncode != 0:
        return None
    return parse_subvolume_show(result.stdout, name=name, path=path)


def local_subvolume_show(path: Path, sudo: str, name: str, btrfs_command: str = "btrfs") -> SubvolumeMeta:
    """Read Btrfs UUID metadata for a local destination subvolume."""

    cmd = sudo_prefix(sudo) + [btrfs_command, "subvolume", "show", str(path)]
    result = run_local(cmd)
    return parse_subvolume_show(result.stdout, name=name, path=str(path))


def remote_readonly(ssh: SSHRunner, sudo: str, btrfs_command: str, path: str) -> bool | None:
    """Check whether a source-side subvolume is read-only."""

    cmd = remote_btrfs_cmd(sudo, btrfs_command, ["property", "get", "-ts", path, "ro"])
    result = ssh.run(cmd, check=False)
    if result.returncode != 0:
        return None
    return parse_property_ro(result.stdout)


def local_readonly(path: Path, sudo: str, btrfs_command: str = "btrfs") -> bool | None:
    """Check whether a local destination subvolume is read-only."""

    cmd = sudo_prefix(sudo) + [btrfs_command, "property", "get", "-ts", str(path), "ro"]
    result = run_local(cmd, check=False)
    if result.returncode != 0:
        return None
    return parse_property_ro(result.stdout)


def _validate_cache_snapshot_name(snapshot_name: str) -> str:
    """Validate a snapshot name before using it in a source cache path."""

    if not snapshot_name or "/" in snapshot_name or snapshot_name in {".", ".."}:
        raise RuntimeError(f"Unsafe snapshot name for cache path: {snapshot_name!r}")
    return snapshot_name


def _validate_cache_subvolume_name(subvolume_name: str) -> str:
    """Validate a subvolume name before using it in a source cache path."""

    if not subvolume_name or "/" in subvolume_name or subvolume_name in {".", ".."}:
        raise RuntimeError(f"Unsafe subvolume name for cache path: {subvolume_name!r}")
    return subvolume_name


def readonly_cache_parent_path(cache_root: str, snapshot_name: str) -> str:
    """Return the per-snapshot cache parent.

    Example:
      /timeshift-btrfs/.ts-btrfs-sync/send-cache/2026-06-22_18-00-01
    """

    return str(Path(cache_root) / _validate_cache_snapshot_name(snapshot_name))


def readonly_cache_path(cache_root: str, snapshot_name: str, subvolume_name: str) -> str:
    """Return the Timeshift-like read-only cache subvolume path.

    Example:
      /timeshift-btrfs/.ts-btrfs-sync/send-cache/2026-06-22_18-00-01/@
      /timeshift-btrfs/.ts-btrfs-sync/send-cache/2026-06-22_18-00-01/@home
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
    """Return a source path safe for `btrfs send`.

    If the original Timeshift subvolume is already read-only, it is used. If it
    is writable, the app creates a read-only cache snapshot using only Btrfs
    commands. The per-snapshot parent is created with `btrfs subvolume create`,
    not mkdir.
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

    # Reuse an existing read-only cache snapshot if it was created by an earlier run.
    existing = remote_try_subvolume_show(ssh, sudo, btrfs_command, cache_path, subvolume_name)
    if existing:
        return cache_path

    # Create <cache_root>/<snapshot-name> using btrfs, not mkdir. The top-level
    # cache_root itself must already exist; that is an admin one-time setup step.
    create_parent_cmd = remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "create", cache_parent])
    ssh.run(create_parent_cmd, check=False)

    # Create the actual read-only send source.
    cmd = remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "snapshot", "-r", original_path, cache_path])
    result = ssh.run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            "Failed to create read-only source cache snapshot. "
            "Make sure source.cache_root exists on the source and the cache path is not blocked.\n"
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
    """Build the SSH command that streams `btrfs send` from the source."""

    args = ["send"]
    if parent_path:
        args += ["-p", parent_path]
    args.append(current_path)
    return ssh.command(remote_btrfs_cmd(sudo, btrfs_command, args))


def local_receive_cmd(destination_dir: Path, sudo: str, btrfs_command: str = "btrfs") -> list[str]:
    """Build the local command that receives a Btrfs stream."""

    return sudo_prefix(sudo) + [btrfs_command, "receive", str(destination_dir)]


def delete_local_subvolume(path: Path, sudo: str, btrfs_command: str = "btrfs") -> None:
    """Delete one local destination Btrfs subvolume during pruning."""

    run_local(sudo_prefix(sudo) + [btrfs_command, "subvolume", "delete", str(path)])
