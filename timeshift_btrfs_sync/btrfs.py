"""Btrfs command builders and parsers."""

from __future__ import annotations

from pathlib import Path
from .commands import quote_join, run_local, sudo_prefix
from .models import SubvolumeMeta
from .ssh import SSHRunner

UUID_KEYS = {"UUID": "uuid", "Parent UUID": "parent_uuid", "Received UUID": "received_uuid"}


def _clean_uuid(value: str) -> str | None:
    """Normalize Btrfs UUID fields."""

    value = value.strip()
    return None if not value or value == "-" else value


def parse_subvolume_show(output: str, name: str, path: str) -> SubvolumeMeta:
    """Parse selected fields from `btrfs subvolume show`."""

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
    """Parse `ro=true` or `ro=false`."""

    for line in output.splitlines():
        line = line.strip().lower()
        if line == "ro=true":
            return True
        if line == "ro=false":
            return False
    return None


def remote_btrfs_cmd(sudo: str, btrfs_command: str, args: list[str]) -> str:
    """Build a quoted remote command that invokes sudo+btrfs only."""

    return quote_join(sudo_prefix(sudo) + [btrfs_command] + args)


def local_btrfs_cmd(sudo: str, btrfs_command: str, args: list[str]) -> list[str]:
    """Build a local btrfs argv list."""

    return sudo_prefix(sudo) + [btrfs_command] + args


def remote_try_subvolume_show(ssh: SSHRunner, sudo: str, btrfs_command: str, path: str, name: str) -> SubvolumeMeta | None:
    """Return remote subvolume metadata or None if invalid."""

    result = ssh.run(remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "show", path]), check=False)
    if result.returncode != 0:
        return None
    return parse_subvolume_show(result.stdout, name=name, path=path)


def local_subvolume_show(path: Path, sudo: str, name: str, btrfs_command: str = "btrfs") -> SubvolumeMeta:
    """Return local subvolume metadata."""

    result = run_local(local_btrfs_cmd(sudo, btrfs_command, ["subvolume", "show", str(path)]))
    return parse_subvolume_show(result.stdout, name=name, path=str(path))


def remote_readonly(ssh: SSHRunner, sudo: str, btrfs_command: str, path: str) -> bool | None:
    """Check remote subvolume read-only property."""

    result = ssh.run(remote_btrfs_cmd(sudo, btrfs_command, ["property", "get", "-ts", path, "ro"]), check=False)
    if result.returncode != 0:
        return None
    return parse_property_ro(result.stdout)


def local_readonly(path: Path, sudo: str, btrfs_command: str = "btrfs") -> bool | None:
    """Check local subvolume read-only property."""

    result = run_local(local_btrfs_cmd(sudo, btrfs_command, ["property", "get", "-ts", str(path), "ro"]), check=False)
    if result.returncode != 0:
        return None
    return parse_property_ro(result.stdout)


def set_local_compression(path: Path, sudo: str, btrfs_command: str, compression: str | None) -> None:
    """Set Btrfs compression property on a local file/directory/subvolume.

    This affects future writes through that inode/property path. It does not
    rewrite already-written extents.
    """

    if not compression:
        return
    run_local(local_btrfs_cmd(sudo, btrfs_command, ["property", "set", str(path), "compression", compression]), check=False)


def _validate_cache_snapshot_name(snapshot_name: str) -> str:
    """Reject unsafe cache snapshot names."""

    if not snapshot_name or "/" in snapshot_name or snapshot_name in {".", ".."}:
        raise RuntimeError(f"Unsafe snapshot name for cache path: {snapshot_name!r}")
    return snapshot_name


def _validate_cache_subvolume_name(subvolume_name: str) -> str:
    """Reject unsafe cache subvolume names."""

    if not subvolume_name or "/" in subvolume_name or subvolume_name in {".", ".."}:
        raise RuntimeError(f"Unsafe subvolume name for cache path: {subvolume_name!r}")
    return subvolume_name


def readonly_cache_parent_path(cache_root: str, snapshot_name: str) -> str:
    """Return <cache_root>/<snapshot-name>."""

    return str(Path(cache_root) / _validate_cache_snapshot_name(snapshot_name))


def readonly_cache_path(cache_root: str, snapshot_name: str, subvolume_name: str) -> str:
    """Return Timeshift-like source read-only cache path."""

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
    """Return a source path safe for `btrfs send`."""

    ro = remote_readonly(ssh, sudo, btrfs_command, original_path)
    if ro is True:
        return original_path
    if not create_readonly_cache:
        raise RuntimeError(f"Source subvolume is not read-only and cache creation is disabled: {original_path}")
    if not cache_root:
        raise RuntimeError("Source subvolume is writable and source.cache_root is not configured")

    cache_parent = readonly_cache_parent_path(cache_root, snapshot_name)
    cache_path = readonly_cache_path(cache_root, snapshot_name, subvolume_name)
    if remote_try_subvolume_show(ssh, sudo, btrfs_command, cache_path, subvolume_name):
        return cache_path

    # Create per-snapshot parent with btrfs, not mkdir.
    ssh.run(remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "create", cache_parent]), check=False)

    result = ssh.run(
        remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "snapshot", "-r", original_path, cache_path]),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Failed to create read-only source cache snapshot.\n" + result.stderr.strip())
    return cache_path


def remote_send_cmd(
    ssh: SSHRunner,
    *,
    sudo: str,
    btrfs_command: str,
    current_path: str,
    parent_path: str | None = None,
    compressed_data: bool = False,
    proto: int | None = None,
) -> list[str]:
    """Build SSH command that runs remote `btrfs send`."""

    args = ["send"]
    if proto is not None:
        args += ["--proto", str(proto)]
    if compressed_data:
        args += ["--compressed-data"]
    if parent_path:
        args += ["-p", parent_path]
    args.append(current_path)
    return ssh.command(remote_btrfs_cmd(sudo, btrfs_command, args))


def local_receive_cmd(destination_dir: Path, sudo: str, btrfs_command: str = "btrfs") -> list[str]:
    """Build local `btrfs receive` command."""

    return local_btrfs_cmd(sudo, btrfs_command, ["receive", str(destination_dir)])


def delete_local_subvolume(path: Path, sudo: str, btrfs_command: str = "btrfs") -> None:
    """Delete one local Btrfs subvolume."""

    run_local(local_btrfs_cmd(sudo, btrfs_command, ["subvolume", "delete", str(path)]))
