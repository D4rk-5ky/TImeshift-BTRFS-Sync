"""Persistent JSON state for completed transfers.

Btrfs incremental sends depend on knowing which snapshots already exist on both
source and destination. This file stores that knowledge in state.json after each
successful subvolume transfer.
"""

from __future__ import annotations

from pathlib import Path
import json
import os
import tempfile
from typing import Any

from .models import SnapshotMeta, SubvolumeMeta


# Increment this later if state.json format changes in a breaking way.
STATE_VERSION = 1


def empty_state() -> dict[str, Any]:
    """Return a brand-new empty state document."""

    return {
        "version": STATE_VERSION,
        "snapshots": {},
    }


def load_state(path: Path) -> dict[str, Any]:
    """Load state.json, or return an empty state if it does not exist."""

    if not path.exists():
        return empty_state()
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    # Be defensive: if the JSON is not the expected shape, use an empty state
    # instead of crashing with confusing dict/list errors later.
    if not isinstance(data, dict):
        return empty_state()
    data.setdefault("version", STATE_VERSION)
    data.setdefault("snapshots", {})
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically write state.json.

    The file is written to a temporary file in the same directory and then
    renamed over the old state. That prevents half-written state if the process
    is interrupted during write.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        # If os.replace succeeded, the temp file no longer exists. If something
        # failed before that, remove the temp file to avoid clutter.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def snapshot_is_synced(state: dict[str, Any], snapshot: str, required_subvolumes: list[str] | None = None) -> bool:
    """Return True when a snapshot is recorded as fully synced.

    If required_subvolumes is provided, every listed subvolume must have status
    `ok`. This allows a snapshot with @ but not @home to be treated correctly.
    """

    item = state.get("snapshots", {}).get(snapshot)
    if not item:
        return False
    subvols = item.get("subvolumes", {})
    if required_subvolumes:
        return all(name in subvols and subvols[name].get("status") == "ok" for name in required_subvolumes)
    return bool(subvols) and all(value.get("status") == "ok" for value in subvols.values())


def mark_subvolume_synced(
    state: dict[str, Any],
    *,
    snapshot: SnapshotMeta,
    subvolume: SubvolumeMeta,
    destination_path: Path,
    parent_snapshot: str | None,
    parent_source_path: str | None,
    send_path: str,
    received_meta: SubvolumeMeta | None,
) -> None:
    """Record one successfully received subvolume in state.json.

    This is called only after the btrfs send/receive pipeline completed. The
    stored parent information is what lets the next run choose an incremental
    parent instead of always doing a full send.
    """

    snapshots = state.setdefault("snapshots", {})
    snap_state = snapshots.setdefault(
        snapshot.name,
        {
            "name": snapshot.name,
            "tags": snapshot.tags,
            "comment": snapshot.comment,
            "created": snapshot.created,
            "path": str(Path("snapshots") / snapshot.name),
            "subvolumes": {},
        },
    )

    # Refresh snapshot-level metadata each time in case Timeshift's info.json
    # was improved or parsed better in a later run.
    snap_state["tags"] = snapshot.tags
    snap_state["comment"] = snapshot.comment
    snap_state["created"] = snapshot.created

    # Store both source and destination UUIDs. They are helpful for debugging
    # and for future stronger validation of incremental chains.
    snap_state.setdefault("subvolumes", {})[subvolume.name] = {
        "status": "ok",
        "name": subvolume.name,
        "source_path": subvolume.path,
        "send_path": send_path,
        "source_uuid": subvolume.uuid,
        "source_parent_uuid": subvolume.parent_uuid,
        "source_received_uuid": subvolume.received_uuid,
        "destination_path": str(destination_path),
        "destination_uuid": received_meta.uuid if received_meta else None,
        "destination_parent_uuid": received_meta.parent_uuid if received_meta else None,
        "destination_received_uuid": received_meta.received_uuid if received_meta else None,
        "parent_snapshot": parent_snapshot,
        "parent_source_path": parent_source_path,
    }


def remove_snapshot_from_state(state: dict[str, Any], snapshot: str) -> None:
    """Remove one snapshot entry after it has been pruned from disk."""

    state.setdefault("snapshots", {}).pop(snapshot, None)


def synced_snapshot_names(state: dict[str, Any]) -> list[str]:
    """Return sorted names of snapshots known in local state."""

    return sorted(state.get("snapshots", {}).keys())


def latest_synced_before(
    state: dict[str, Any],
    snapshot_name: str,
    subvolume_name: str,
    source_names: set[str],
) -> tuple[str, dict[str, Any]] | None:
    """Find the newest usable incremental parent before a snapshot.

    A parent is usable only if:
      - it is older than the current snapshot,
      - it still exists on the source, and
      - the same subvolume was successfully synced before.
    """

    candidates: list[tuple[str, dict[str, Any]]] = []
    for name, item in state.get("snapshots", {}).items():
        if name >= snapshot_name:
            continue
        if name not in source_names:
            continue
        sub = item.get("subvolumes", {}).get(subvolume_name)
        if not sub or sub.get("status") != "ok":
            continue
        candidates.append((name, sub))
    if not candidates:
        return None

    # Snapshot names are timestamp-like, so lexical sort gives the newest parent
    # at the end.
    candidates.sort(key=lambda x: x[0])
    return candidates[-1]
