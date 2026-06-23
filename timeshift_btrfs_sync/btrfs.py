"""Btrfs command wrappers and parsers.

This module does not implement Btrfs itself. It only builds safe command lines,
runs local or remote btrfs commands, and parses the small pieces of metadata the
sync logic needs.
"""

from __future__ import annotations

from pathlib import Path
import shlex

from .commands import quote_join, run_local, sudo_prefix
from .models import SubvolumeMeta
from .ssh import SSHRunner


# `btrfs subvolume show` prints human-readable keys. This mapping tells the
# parser which output lines should populate which SubvolumeMeta attributes.
UUID_KEYS = {
    "UUID": "uuid",
    "Parent UUID": "parent_uuid",
    "Received UUID": "received_uuid",
}


def _clean_uuid(value: str) -> str | None:
    """Normalize UUID fields from btrfs output.

    Btrfs prints `-` when a UUID field is not set. The app stores that as None
    so state.json is easier to inspect and compare.
    """

    value = value.strip()
    if not value or value == "-":
        return None
    return value


def parse_subvolume_show(output: str, name: str, path: str) -> SubvolumeMeta:
    """Parse `btrfs subvolume show` output into a SubvolumeMeta object."""

    meta = SubvolumeMeta(name=name, path=path)
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        attr = UUID_KEYS.get(key)
        if attr:
            setattr(meta, attr, _clean_uuid(value))
    return meta


def parse_property_ro(output: str) -> bool | None:
    """Parse `btrfs property get -ts <path> ro` output.

    Expected output is either `ro=true` or `ro=false`. None means the output did
    not match what we expected.
    """

    for line in output.splitlines():
        line = line.strip().lower()
        if line == "ro=true":
            return True
        if line == "ro=false":
            return False
    return None


def remote_path_exists(ssh: SSHRunner, path: str) -> bool:
    """Return whether a path exists on the source machine."""

    cmd = f"test -e {shlex.quote(path)}"
    result = ssh.run(cmd, check=False)
    return result.returncode == 0


def remote_dir_exists(ssh: SSHRunner, path: str) -> bool:
    """Return whether a directory exists on the source machine."""

    cmd = f"test -d {shlex.quote(path)}"
    result = ssh.run(cmd, check=False)
    return result.returncode == 0


def local_path_exists(path: Path) -> bool:
    """Return whether a local path exists."""

    return path.exists()


def remote_subvolume_show(ssh: SSHRunner, sudo: str, path: str, name: str) -> SubvolumeMeta:
    """Read Btrfs UUID metadata for a remote/source subvolume."""

    cmd = quote_join(sudo_prefix(sudo) + ["btrfs", "subvolume", "show", path])
    result = ssh.run(cmd)
    return parse_subvolume_show(result.stdout, name=name, path=path)


def local_subvolume_show(path: Path, sudo: str, name: str) -> SubvolumeMeta:
    """Read Btrfs UUID metadata for a local/destination subvolume."""

    cmd = sudo_prefix(sudo) + ["btrfs", "subvolume", "show", str(path)]
    result = run_local(cmd)
    return parse_subvolume_show(result.stdout, name=name, path=str(path))


def remote_readonly(ssh: SSHRunner, sudo: str, path: str) -> bool | None:
    """Check whether a remote/source subvolume is read-only."""

    cmd = quote_join(sudo_prefix(sudo) + ["btrfs", "property", "get", "-ts", path, "ro"])
    result = ssh.run(cmd, check=False)
    if result.returncode != 0:
        return None
    return parse_property_ro(result.stdout)


def local_readonly(path: Path, sudo: str) -> bool | None:
    """Check whether a local/destination subvolume is read-only."""

    cmd = sudo_prefix(sudo) + ["btrfs", "property", "get", "-ts", str(path), "ro"]
    result = run_local(cmd, check=False)
    if result.returncode != 0:
        return None
    return parse_property_ro(result.stdout)


def remote_ensure_dir(ssh: SSHRunner, path: str) -> None:
    """Create a directory on the source machine if it does not exist."""

    ssh.run(quote_join(["mkdir", "-p", path]))


def local_ensure_dir(path: Path) -> None:
    """Create a local directory if it does not exist."""

    path.mkdir(parents=True, exist_ok=True)


def remote_ensure_readonly_send_path(
    ssh: SSHRunner,
    sudo: str,
    original_path: str,
    cache_path: str,
) -> str:
    """Return a safe remote path that can be used by `btrfs send`.

    `btrfs send` requires a read-only subvolume. If the original Timeshift
    subvolume is already read-only, it is used directly. If not, this creates an
    app-managed read-only snapshot under the configured send cache and returns
    that cache path.
    """

    ro = remote_readonly(ssh, sudo, original_path)
    if ro is True:
        return original_path

    # The cache path includes snapshot/subvolume names. Its parent must exist
    # before `btrfs subvolume snapshot -r` can create the actual subvolume.
    parent = str(Path(cache_path).parent)
    ssh.run(quote_join(sudo_prefix(sudo) + ["mkdir", "-p", parent]))

    # Reuse an existing cache snapshot so repeated dry-run/test cycles do not
    # create duplicates.
    if remote_dir_exists(ssh, cache_path):
        return cache_path

    cmd = quote_join(sudo_prefix(sudo) + ["btrfs", "subvolume", "snapshot", "-r", original_path, cache_path])
    ssh.run(cmd)
    return cache_path


def local_receive_cmd(destination_dir: Path, sudo: str) -> list[str]:
    """Build the local `btrfs receive` command.

    btrfs receive writes the incoming subvolume inside `destination_dir`.
    """

    return sudo_prefix(sudo) + ["btrfs", "receive", str(destination_dir)]


def remote_send_cmd(
    ssh: SSHRunner,
    sudo: str,
    current_path: str,
    parent_path: str | None = None,
) -> list[str]:
    """Build the remote `btrfs send` command run through SSH.

    When `parent_path` is provided, the command becomes incremental:
    `btrfs send -p <parent> <current>`.
    """

    args = sudo_prefix(sudo) + ["btrfs", "send"]
    if parent_path:
        args += ["-p", parent_path]
    args.append(current_path)
    return ssh.command(quote_join(args))


def delete_local_subvolume(path: Path, sudo: str) -> None:
    """Delete one local Btrfs subvolume from the destination."""

    cmd = sudo_prefix(sudo) + ["btrfs", "subvolume", "delete", str(path)]
    run_local(cmd)


def local_subvolume_sync(path: Path, sudo: str) -> None:
    """Ask Btrfs to wait for queued subvolume deletions under a path.

    This helper is available for future cleanup workflows. It is non-fatal here
    because not all btrfs versions/setups need it.
    """

    cmd = sudo_prefix(sudo) + ["btrfs", "subvolume", "sync", str(path)]
    run_local(cmd, check=False)
