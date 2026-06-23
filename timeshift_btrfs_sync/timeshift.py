"""Timeshift command wrappers and `timeshift --list` parser.

This module discovers source snapshots without installing a helper script and
without source-side sudo cat/find/python. It only calls `sudo timeshift --list`
and then verifies candidate subvolumes with `sudo btrfs ...`.
"""

from __future__ import annotations

from pathlib import Path
import re

from . import btrfs
from .commands import quote_join, sudo_prefix
from .models import SnapshotMeta, SubvolumeMeta
from .ssh import SSHRunner


# Timeshift's default Btrfs snapshot directory names use this timestamp format.
SNAPSHOT_RE = re.compile(r"(?P<name>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")

# Supported tags. H/D/W/M/B/O are Timeshift style; Y is this app's optional
# yearly extension.
TAG_CHARS = set("HDWMBOY")


def timeshift_cmd(sudo: str, timeshift_command: str, args: list[str]) -> str:
    """Build a quoted remote command that invokes only sudo+timeshift."""

    return quote_join(sudo_prefix(sudo) + [timeshift_command] + args)


def normalize_tags(text: str | None) -> list[str]:
    """Return unique Timeshift tag letters found in text."""

    tags: list[str] = []
    for ch in (text or "").upper():
        if ch in TAG_CHARS and ch not in tags:
            tags.append(ch)
    return tags


def parse_timeshift_list(output: str, snapshot_root: str) -> list[SnapshotMeta]:
    """Parse snapshot names, tags, and comments from `timeshift --list`.

    The parser looks for timestamp-like snapshot names anywhere on each line.
    If the token after the timestamp is made only of tag letters, it becomes the
    tag list; the rest is treated as a comment.
    """

    snapshots: list[SnapshotMeta] = []
    seen: set[str] = set()
    for line in output.splitlines():
        match = SNAPSHOT_RE.search(line)
        if not match:
            continue
        name = match.group("name")
        if name in seen:
            continue
        seen.add(name)

        after = line[match.end():].strip()
        tags: list[str] = []
        comment: str | None = None
        if after:
            parts = after.split(maxsplit=1)
            first = parts[0].strip()
            if first and all(ch.upper() in TAG_CHARS for ch in first):
                tags = normalize_tags(first)
                comment = parts[1] if len(parts) > 1 else None
            else:
                comment = after

        snapshots.append(
            SnapshotMeta(
                name=name,
                path=str(Path(snapshot_root) / name),
                tags=tags,
                comment=comment,
                created=name,
            )
        )
    return sorted(snapshots, key=lambda s: s.name)


def list_remote_snapshots(
    ssh: SSHRunner,
    *,
    snapshot_root: str,
    subvolumes: list[str],
    sudo: str,
    timeshift_command: str,
    btrfs_command: str,
    include_btrfs_info: bool = True,
) -> list[SnapshotMeta]:
    """Discover source snapshots using only sudo timeshift and sudo btrfs."""

    # First discover snapshot names/tags. This replaces reading info.json.
    result = ssh.run(timeshift_cmd(sudo, timeshift_command, ["--list"]))
    snapshots = parse_timeshift_list(result.stdout, snapshot_root)

    if not include_btrfs_info:
        # Fast mode: fill in expected paths but do not verify Btrfs metadata.
        for snap in snapshots:
            for subvol in subvolumes:
                snap.subvolumes[subvol] = SubvolumeMeta(name=subvol, path=str(Path(snap.path) / subvol))
        return snapshots

    # Normal mode: verify each configured subvolume actually exists and read its
    # UUID/read-only metadata with btrfs.
    for snap in snapshots:
        for subvol in subvolumes:
            path = str(Path(snap.path) / subvol)
            meta = btrfs.remote_try_subvolume_show(ssh, sudo, btrfs_command, path, subvol)
            if not meta:
                continue
            meta.readonly = btrfs.remote_readonly(ssh, sudo, btrfs_command, path)
            snap.subvolumes[subvol] = meta
    return snapshots


def create_remote_manual_snapshot(
    ssh: SSHRunner,
    *,
    sudo: str,
    timeshift_command: str,
    comment: str,
) -> None:
    """Create a Timeshift on-demand/manual snapshot with tag O."""

    ssh.run(timeshift_cmd(
        sudo,
        timeshift_command,
        ["--create", "--scripted", "--tags", "O", "--comments", comment],
    ))
