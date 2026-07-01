"""Timeshift command wrappers and parser for `timeshift --list`."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import os
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


def _snapshot_time(name: str) -> datetime | None:
    """Return a datetime parsed from a Timeshift snapshot folder name."""

    try:
        return datetime.strptime(name, "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def _indexed_snapshot_subvolumes(snapshot_root: str, subvolumes: list[str], btrfs_index) -> dict[str, set[str]]:
    """Return snapshot date folders and configured subvolumes present in an index.

    Some Timeshift versions can print a creation timestamp in ``timeshift --list``
    that differs by a few seconds from the actual snapshot directory name.  The
    Btrfs index contains the real paths, so discovery uses it to resolve the
    actual folder name before building paths for metadata staging and sending.
    """

    if btrfs_index is None:
        return {}
    root = os.path.normpath(str(snapshot_root)).rstrip("/")
    found: dict[str, set[str]] = {}
    wanted = set(subvolumes)
    for raw_path in getattr(btrfs_index, "by_path", {}):
        path = os.path.normpath(str(raw_path)).rstrip("/")
        if path == root or not path.startswith(root + "/"):
            continue
        rel = path[len(root) + 1 :]
        parts = rel.split("/")
        if len(parts) < 2:
            continue
        snapshot_name, subvol_name = parts[0], parts[1]
        if not SNAPSHOT_RE.fullmatch(snapshot_name):
            continue
        if subvol_name in wanted:
            found.setdefault(snapshot_name, set()).add(subvol_name)
    return found


def _resolve_indexed_snapshot_name(parsed_name: str, indexed: dict[str, set[str]], subvolumes: list[str]) -> str:
    """Resolve a Timeshift-list timestamp to the real indexed folder name.

    Exact folder names are preferred. If the exact folder is absent, choose a
    single indexed folder within a small time window that contains at least one
    configured subvolume. This fixes cases where ``timeshift --list`` reports
    e.g. ``06-20-20`` while the actual folder is ``06-20-23``. Ambiguous matches
    are ignored so the app never guesses between multiple possible folders.
    """

    if not indexed or parsed_name in indexed:
        return parsed_name
    parsed_time = _snapshot_time(parsed_name)
    if parsed_time is None:
        return parsed_name
    wanted = set(subvolumes)
    candidates: list[tuple[float, int, str]] = []
    for candidate, present in indexed.items():
        candidate_time = _snapshot_time(candidate)
        if candidate_time is None:
            continue
        delta = abs((candidate_time - parsed_time).total_seconds())
        if delta > 120:
            continue
        score = len(wanted & present)
        if score <= 0:
            continue
        candidates.append((delta, -score, candidate))
    if not candidates:
        return parsed_name
    candidates.sort()
    best = candidates[0]
    # Refuse to guess when two indexed folders are equally plausible.
    if len(candidates) > 1 and candidates[1][0] == best[0] and candidates[1][1] == best[1]:
        return parsed_name
    return best[2]


def _resolve_snapshot_names_from_index(snapshots: list[SnapshotMeta], snapshot_root: str, subvolumes: list[str], btrfs_index) -> list[SnapshotMeta]:
    """Return snapshots adjusted to actual Btrfs-indexed directory names."""

    indexed = _indexed_snapshot_subvolumes(snapshot_root, subvolumes, btrfs_index)
    if not indexed:
        return snapshots
    resolved: list[SnapshotMeta] = []
    seen: set[str] = set()
    for snap in snapshots:
        actual_name = _resolve_indexed_snapshot_name(snap.name, indexed, subvolumes)
        if actual_name != snap.name:
            snap = SnapshotMeta(
                name=actual_name,
                path=str(Path(snapshot_root) / actual_name),
                tags=list(snap.tags),
                comment=snap.comment,
                created=snap.created or snap.name,
            )
        if snap.name in seen:
            continue
        seen.add(snap.name)
        resolved.append(snap)
    return sorted(resolved, key=lambda item: item.name)


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
    metadata and checked later at send time. The index is also used to correct
    small timestamp differences between the Timeshift list entry and the real
    snapshot directory name.
    """

    result = source.run(timeshift_cmd(sudo, timeshift_command, ["--list"]))
    snapshots = _resolve_snapshot_names_from_index(parse_timeshift_list(result.stdout, snapshot_root), snapshot_root, subvolumes, btrfs_index)
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
