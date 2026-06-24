"""Persistent local state for completed transfers."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os
import tempfile
from .models import SnapshotMeta, SubvolumeMeta

STATE_VERSION = 1


def empty_state() -> dict[str, Any]:
    """Return a new empty state document."""

    return {"version": STATE_VERSION, "snapshots": {}}


def load_state(path: Path) -> dict[str, Any]:
    """Load state.json or return empty state."""

    if not path.exists():
        return empty_state()
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        return empty_state()
    data.setdefault("version", STATE_VERSION)
    data.setdefault("snapshots", {})
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically write state.json."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def snapshot_is_synced(state: dict[str, Any], snapshot: str, required_subvolumes: list[str] | None = None) -> bool:
    """Return True when a snapshot is recorded as fully synced."""

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
    original_meta: SubvolumeMeta | None = None,
    send_meta: SubvolumeMeta | None = None,
) -> None:
    """Record one successful send/receive in state.

    `original_meta` describes the Timeshift snapshot path, for example
    /timeshift-btrfs/snapshots/<name>/@. `send_meta` describes the exact
    subvolume that was streamed with `btrfs send`, which can be a read-only
    source cache snapshot. Keeping both UUIDs lets later runs safely choose a
    high-watermark floor after destination pruning without adding tombstone
    entries for every pruned snapshot.
    """

    snapshots = state.setdefault("snapshots", {})
    snap_state = snapshots.setdefault(snapshot.name, {"name": snapshot.name, "tags": snapshot.tags, "comment": snapshot.comment, "created": snapshot.created, "path": str(Path("snapshots") / snapshot.name), "subvolumes": {}})
    snap_state["tags"] = snapshot.tags
    snap_state["comment"] = snapshot.comment
    snap_state["created"] = snapshot.created
    # The actual streamed source identity is the UUID of the send path. If the
    # source Timeshift snapshot was writable, send_path points at a read-only
    # cache snapshot and that cache UUID is what the destination records as
    # Received UUID.
    send_source_uuid = (send_meta.uuid if send_meta else None) or (received_meta.received_uuid if received_meta else None) or subvolume.uuid
    original_source_uuid = (original_meta.uuid if original_meta else None) or subvolume.uuid

    snap_state.setdefault("subvolumes", {})[subvolume.name] = {
        "status": "ok",
        "name": subvolume.name,
        "source_path": subvolume.path,
        "send_path": send_path,
        # Backward-compatible name. New code treats this as the exact source
        # UUID that was streamed, not necessarily the writable Timeshift source
        # subvolume UUID.
        "source_uuid": send_source_uuid,
        "send_source_uuid": send_source_uuid,
        "original_source_uuid": original_source_uuid,
        "source_parent_uuid": (send_meta.parent_uuid if send_meta else None) or subvolume.parent_uuid,
        "source_received_uuid": (send_meta.received_uuid if send_meta else None) or subvolume.received_uuid,
        "original_source_parent_uuid": original_meta.parent_uuid if original_meta else subvolume.parent_uuid,
        "original_source_received_uuid": original_meta.received_uuid if original_meta else subvolume.received_uuid,
        "destination_path": str(destination_path),
        "destination_uuid": received_meta.uuid if received_meta else None,
        "destination_parent_uuid": received_meta.parent_uuid if received_meta else None,
        "destination_received_uuid": received_meta.received_uuid if received_meta else None,
        "parent_snapshot": parent_snapshot,
        "parent_source_path": parent_source_path,
        "source_uuid_inferred_from_destination_received_uuid": bool(send_meta is None and subvolume.uuid is None and received_meta and received_meta.received_uuid),
    }


def remove_snapshot_from_state(state: dict[str, Any], snapshot: str) -> None:
    """Remove a pruned snapshot from state."""

    state.setdefault("snapshots", {}).pop(snapshot, None)


def latest_synced_before(state: dict[str, Any], snapshot_name: str, subvolume_name: str, source_names: set[str]) -> tuple[str, dict[str, Any]] | None:
    """Return newest synced parent snapshot for incremental send."""

    candidates: list[tuple[str, dict[str, Any]]] = []
    for name, item in state.get("snapshots", {}).items():
        if name >= snapshot_name or name not in source_names:
            continue
        sub = item.get("subvolumes", {}).get(subvolume_name)
        if sub and sub.get("status") == "ok":
            candidates.append((name, sub))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[-1]
