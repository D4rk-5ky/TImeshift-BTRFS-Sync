"""Timeshift command wrappers and parser for `timeshift --list`."""

from __future__ import annotations

from pathlib import Path
import re
from . import btrfs
from .commands import quote_join, remote_double_quote, sudo_prefix
from .models import SnapshotMeta, SubvolumeMeta
from .ssh import SSHRunner
from .source import SourceRunner

SNAPSHOT_RE = re.compile(r"(?P<name>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")
TAG_CHARS = set("HDWMBO")


def timeshift_cmd(sudo: str, timeshift_command: str, args: list[str]) -> str:
    """Build a source-side shell command that invokes sudo+timeshift."""

    return quote_join(sudo_prefix(sudo) + [timeshift_command] + args)


def normalize_tags(text: str | None) -> list[str]:
    """Return unique Timeshift tag letters found in text."""

    tags: list[str] = []
    for ch in (text or "").upper():
        if ch in TAG_CHARS and ch not in tags:
            tags.append(ch)
    return tags


def parse_timeshift_list(output: str, snapshot_root: str) -> list[SnapshotMeta]:
    """Parse Timeshift snapshot names and tag/comment text."""

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
            # Timeshift versions/themes may render tags either compact (DWM) or
            # separated by spaces (D W M). Collect all leading tag-only tokens
            # before treating the rest of the line as the comment.
            tokens = after.split()
            tag_tokens: list[str] = []
            while tokens and all(ch.upper() in TAG_CHARS for ch in tokens[0]):
                tag_tokens.append(tokens.pop(0))
            if tag_tokens:
                tags = normalize_tags("".join(tag_tokens))
                comment = " ".join(tokens) if tokens else None
            else:
                comment = after
        snapshots.append(SnapshotMeta(name=name, path=str(Path(snapshot_root) / name), tags=tags, comment=comment, created=name))
    return sorted(snapshots, key=lambda s: s.name)


def list_source_snapshots(
    source: SourceRunner,
    *,
    snapshot_root: str,
    subvolumes: list[str],
    sudo: str,
    timeshift_command: str,
    btrfs_command: str,
    include_btrfs_info: bool = True,
    btrfs_index=None,
) -> list[SnapshotMeta]:
    """Discover source snapshots through SSH or local source commands.

    When a bulk Btrfs index for ``snapshot_root`` is supplied, discovery fills
    each configured subvolume from that in-memory metadata. That avoids running
    one ``btrfs subvolume show`` per Timeshift snapshot over SSH. If discovery
    verification is disabled, missing entries are represented by path-only
    metadata and checked later at send time.
    """

    result = source.run(timeshift_cmd(sudo, timeshift_command, ["--list"]))
    snapshots = parse_timeshift_list(result.stdout, snapshot_root)
    for snap in snapshots:
        for subvol in subvolumes:
            path = str(Path(snap.path) / subvol)
            indexed = btrfs_index.meta(path) if btrfs_index is not None else None
            if indexed:
                snap.subvolumes[subvol] = indexed
                continue
            if not include_btrfs_info:
                snap.subvolumes[subvol] = SubvolumeMeta(name=subvol, path=path)
                continue
            meta = btrfs.source_get_subvolume_meta(source, path=path, name=subvol, sudo=sudo, btrfs_command=btrfs_command, required=False)
            if meta:
                snap.subvolumes[subvol] = meta
    return snapshots


def list_remote_snapshots(
    ssh: SSHRunner,
    *,
    snapshot_root: str,
    subvolumes: list[str],
    sudo: str,
    timeshift_command: str,
    btrfs_command: str,
    include_btrfs_info: bool = True,
    btrfs_index=None,
) -> list[SnapshotMeta]:
    """Discover source snapshots using only sudo timeshift and sudo btrfs."""

    return list_source_snapshots(
        SourceRunner(mode="ssh", ssh=ssh),
        snapshot_root=snapshot_root,
        subvolumes=subvolumes,
        sudo=sudo,
        timeshift_command=timeshift_command,
        btrfs_command=btrfs_command,
        include_btrfs_info=include_btrfs_info,
        btrfs_index=btrfs_index,
    )


def create_remote_manual_snapshot_cmd(sudo: str, timeshift_command: str, comment: str) -> str:
    """Build the Timeshift manual/on-demand snapshot create command.

    Do not pass ``--tags O`` here. Timeshift documents O as the default
    on-demand tag, but several Timeshift versions reject an explicit O tag due
    to a CLI validation bug. Omitting ``--tags`` is both cleaner and safer: a
    plain ``timeshift --create`` snapshot becomes an on-demand snapshot by
    default.

    The comment is intentionally quoted with remote-safe double quotes instead
    of the default single-quote style. That avoids very noisy logged SSH
    commands such as ``'"'"'comment'"'"'`` while still making comments with
    spaces safe for the remote shell.
    """

    base = sudo_prefix(sudo) + [timeshift_command, "--create", "--scripted", "--comments"]
    return quote_join(base) + " " + remote_double_quote(comment)


def create_source_manual_snapshot(source: SourceRunner, *, sudo: str, timeshift_command: str, comment: str) -> None:
    """Create a source Timeshift on-demand snapshot through SSH or locally."""

    source.run(create_remote_manual_snapshot_cmd(sudo, timeshift_command, comment), mirror_stdout_on_failure=True)


def create_remote_manual_snapshot(ssh: SSHRunner, *, sudo: str, timeshift_command: str, comment: str) -> None:
    """Create a Timeshift on-demand snapshot.

    Timeshift assigns the on-demand/O tag automatically when no other tag is
    supplied. This avoids the known CLI bug where explicit ``--tags O`` can
    fail even though the man page lists O as valid.
    """

    # Timeshift sometimes reports the useful reason for create failures on
    # stdout rather than stderr. Mirror stdout on failure so users can see the
    # real Timeshift error instead of only "Command failed (1)".
    create_source_manual_snapshot(SourceRunner(mode="ssh", ssh=ssh), sudo=sudo, timeshift_command=timeshift_command, comment=comment)
