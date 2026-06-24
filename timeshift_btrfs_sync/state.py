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
) -> None:
    """Record one successful send/receive in state."""

    snapshots = state.setdefault("snapshots", {})
    snap_state = snapshots.setdefault(snapshot.name, {"name": snapshot.name, "tags": snapshot.tags, "comment": snapshot.comment, "created": snapshot.created, "path": str(Path("snapshots") / snapshot.name), "subvolumes": {}})
    snap_state["tags"] = snapshot.tags
    snap_state["comment"] = snapshot.comment
    snap_state["created"] = snapshot.created
    # Prefer explicit source metadata when we have it. In the fast path we do
    # not read remote source UUID metadata for every send. After `btrfs receive`,
    # the local destination `Received UUID` is the UUID of the source subvolume
    # that was sent, so we can still save the useful source identity using only
    # local metadata.
    inferred_source_uuid = subvolume.uuid or (received_meta.received_uuid if received_meta else None)

    snap_state.setdefault("subvolumes", {})[subvolume.name] = {
        "status": "ok",
        "name": subvolume.name,
        "source_path": subvolume.path,
        "send_path": send_path,
        "source_uuid": inferred_source_uuid,
        "source_parent_uuid": subvolume.parent_uuid,
        "source_received_uuid": subvolume.received_uuid,
        "destination_path": str(destination_path),
        "destination_uuid": received_meta.uuid if received_meta else None,
        "destination_parent_uuid": received_meta.parent_uuid if received_meta else None,
        "destination_received_uuid": received_meta.received_uuid if received_meta else None,
        "parent_snapshot": parent_snapshot,
        "parent_source_path": parent_source_path,
        "source_uuid_inferred_from_destination_received_uuid": bool(subvolume.uuid is None and received_meta and received_meta.received_uuid),
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
