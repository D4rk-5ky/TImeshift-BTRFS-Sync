"""Timeshift command wrappers and `timeshift --list` parser.

To avoid source-side helper scripts and source-side sudo cat/find/mkdir, snapshot
names and tags are discovered from `sudo -n timeshift --list`.
"""

from __future__ import annotations

from pathlib import Path
import re

from . import btrfs
from .commands import quote_join, sudo_prefix
from .models import SnapshotMeta, SubvolumeMeta
from .ssh import SSHRunner

SNAPSHOT_RE = re.compile(r"(?P<name>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")
TAG_CHARS = set("HDWMBOY")


def timeshift_cmd(sudo: str, timeshift_command: str, args: list[str]) -> str:
    """Build a remote command that only invokes sudo+timeshift."""

    return quote_join(sudo_prefix(sudo) + [timeshift_command] + args)


def normalize_tags(text: str | None) -> list[str]:
    """Normalize Timeshift tag letters."""

    tags: list[str] = []
    for ch in (text or "").upper():
        if ch in TAG_CHARS and ch not in tags:
            tags.append(ch)
    return tags


def parse_timeshift_list(output: str, snapshot_root: str) -> list[SnapshotMeta]:
    """Parse snapshot names/tags/comments from `timeshift --list` output.

    The parser looks for Timeshift timestamp names anywhere in each line. Text
    after the name is interpreted as a tag column if the next token is made only
    of known tag letters, with the rest treated as a comment.
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
    """List source Timeshift snapshots using only sudo timeshift and sudo btrfs."""

    result = ssh.run(timeshift_cmd(sudo, timeshift_command, ["--list"]))
    snapshots = parse_timeshift_list(result.stdout, snapshot_root)

    if not include_btrfs_info:
        for snap in snapshots:
            for subvol in subvolumes:
                snap.subvolumes[subvol] = SubvolumeMeta(name=subvol, path=str(Path(snap.path) / subvol))
        return snapshots

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
    """Create a Timeshift on-demand snapshot with tag O."""

    ssh.run(timeshift_cmd(
        sudo,
        timeshift_command,
        ["--create", "--scripted", "--tags", "O", "--comments", comment],
    ))
