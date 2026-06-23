"""Timeshift-specific helpers.

This module discovers Timeshift snapshot folders on the source machine and can
ask Timeshift to create a new on-demand/manual snapshot. Btrfs metadata reading
is delegated to btrfs.py.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from .commands import quote_join, sudo_prefix
from .models import SnapshotMeta, SubvolumeMeta
from .ssh import SSHRunner
from . import btrfs


# This Python snippet is executed on the source machine over SSH. It is kept as a
# string so the backup machine does not need this package installed on the source.
# It only uses Python's standard library to list snapshot folders and parse
# Timeshift's info.json files.
REMOTE_LIST_SCRIPT = r'''
import json
import os
import sys

root = sys.argv[1]
subvols = json.loads(sys.argv[2])

def normalize_tags(value):
    allowed = set("HDWMBOY")
    tags = []
    if isinstance(value, list):
        raw = "".join(str(v) for v in value)
    elif value is None:
        raw = ""
    else:
        raw = str(value)
    for ch in raw.upper():
        if ch in allowed and ch not in tags:
            tags.append(ch)
    return tags

out = []
if os.path.isdir(root):
    for name in sorted(os.listdir(root)):
        snap_path = os.path.join(root, name)
        if not os.path.isdir(snap_path):
            continue
        info_path = os.path.join(snap_path, "info.json")
        info = {}
        if os.path.exists(info_path):
            try:
                with open(info_path, "r", encoding="utf-8", errors="replace") as fh:
                    info = json.load(fh)
            except Exception:
                info = {}
        tags = normalize_tags(info.get("tags", info.get("tag", info.get("type"))))
        comment = info.get("comments", info.get("comment", info.get("description")))
        created = info.get("created", info.get("date", info.get("timestamp")))
        found_subvols = []
        for subvol in subvols:
            subvol_path = os.path.join(snap_path, subvol)
            if os.path.isdir(subvol_path):
                found_subvols.append({"name": subvol, "path": subvol_path})
        out.append({
            "name": name,
            "path": snap_path,
            "tags": tags,
            "comment": comment,
            "created": created,
            "subvolumes": found_subvols,
        })
print(json.dumps(out, sort_keys=True))
'''


def default_cache_root(snapshot_root: str) -> str:
    """Return the default remote read-only send-cache root.

    Example:
      /timeshift-btrfs/snapshots
      -> /timeshift-btrfs/.ts-btrfs-sync/send-cache
    """

    return str(Path(snapshot_root).parent / ".ts-btrfs-sync" / "send-cache")


def cache_path_for(cache_root: str, snapshot_name: str, subvolume_name: str) -> str:
    """Return the cache subvolume path for one snapshot/subvolume pair."""

    return str(Path(cache_root) / snapshot_name / subvolume_name)


def list_remote_snapshots(
    ssh: SSHRunner,
    *,
    snapshot_root: str,
    subvolumes: list[str],
    sudo: str,
    include_btrfs_info: bool = True,
) -> list[SnapshotMeta]:
    """Discover Timeshift snapshots on the remote/source machine.

    First a tiny remote Python script lists snapshot folders and info.json data.
    Then, optionally, this function asks Btrfs for UUID/read-only metadata for
    each discovered subvolume.
    """

    # Quote the script and arguments so they can safely pass through SSH's
    # remote shell as one `python3 -c ...` command.
    script = shlex.quote(REMOTE_LIST_SCRIPT)
    subvol_json = json.dumps(subvolumes)
    cmd = f"python3 -c {script} {shlex.quote(snapshot_root)} {shlex.quote(subvol_json)}"
    result = ssh.run(cmd)
    raw = json.loads(result.stdout or "[]")

    snapshots: list[SnapshotMeta] = []
    for item in raw:
        snap = SnapshotMeta(
            name=item["name"],
            path=item["path"],
            tags=list(item.get("tags") or []),
            comment=item.get("comment"),
            created=item.get("created"),
        )
        for sv in item.get("subvolumes") or []:
            meta = SubvolumeMeta(name=sv["name"], path=sv["path"])
            if include_btrfs_info:
                try:
                    # UUIDs and read-only status are not in Timeshift metadata,
                    # so we query Btrfs directly for every discovered subvolume.
                    meta = btrfs.remote_subvolume_show(ssh, sudo, sv["path"], sv["name"])
                    meta.readonly = btrfs.remote_readonly(ssh, sudo, sv["path"])
                except Exception:
                    # Keep the snapshot visible even if metadata reading fails.
                    # The sync command will fail later if it truly needs this
                    # subvolume and cannot access it.
                    meta = SubvolumeMeta(name=sv["name"], path=sv["path"])
            snap.subvolumes[meta.name] = meta
        snapshots.append(snap)
    return snapshots


def create_remote_manual_snapshot(
    ssh: SSHRunner,
    *,
    sudo: str,
    timeshift_command: str,
    comment: str,
) -> None:
    """Create a Timeshift on-demand/manual snapshot on the source.

    Timeshift tag O means on-demand. The snapshot will then be picked up by a
    later `sync` run like any other Timeshift snapshot.
    """

    args = sudo_prefix(sudo) + [
        timeshift_command,
        "--create",
        "--scripted",
        "--tags",
        "O",
        "--comments",
        comment,
    ]
    ssh.run(quote_join(args))
