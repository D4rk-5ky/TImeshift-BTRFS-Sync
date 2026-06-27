"""Persistent local state for completed transfers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable
import json
import os
import tempfile
from .models import SnapshotMeta, SubvolumeMeta

STATE_VERSION = 1


def empty_state() -> dict[str, Any]:
    """Return a new empty state document."""

    return {"version": STATE_VERSION, "snapshots": {}}


def _safe_relative_path(path: Path) -> Path:
    """Return a normalized relative path or raise ValueError.

    State paths are deliberately stored relative to destination.target_root so
    moving the whole backup to a new mount point only requires changing
    destination.target_root in the config. Parent-directory escapes are refused
    because these paths may later be used for destructive prune/delete actions.
    """

    if path.is_absolute():
        raise ValueError(f"path is absolute, not target-root relative: {path}")
    if not path.parts or path == Path("."):
        raise ValueError("empty destination-relative path")
    if any(part == ".." for part in path.parts):
        raise ValueError(f"destination-relative path escapes target_root: {path}")
    return Path(*path.parts)


def destination_path_to_relative(destination_path: Path, target_root: Path) -> str:
    """Convert a destination subvolume path to a target_root-relative string.

    New state stores paths like:
      snapshots/2026-06-23_07-10-24/@

    Older state versions stored absolute paths. If an old absolute path is no
    longer below the current target_root because the backup was moved, infer the
    relative path from the standard layout suffix starting at the last
    "snapshots" path component.
    """

    path = Path(destination_path)
    if not path.is_absolute():
        return _safe_relative_path(path).as_posix()

    root = Path(target_root)
    try:
        rel = path.relative_to(root)
    except ValueError:
        parts = path.parts
        snapshot_indexes = [idx for idx, part in enumerate(parts) if part == "snapshots"]
        if not snapshot_indexes:
            raise ValueError(
                f"absolute destination path is not under target_root and has no snapshots/ suffix: {path}"
            )
        rel = Path(*parts[snapshot_indexes[-1] :])
    return _safe_relative_path(rel).as_posix()


def resolve_destination_path(target_root: Path, stored_path: str | Path) -> Path:
    """Resolve a state destination_path against the current target_root.

    This accepts both new relative state paths and older absolute state paths.
    Older absolute paths are treated as migrated state and are resolved under the
    current target_root when they contain the standard snapshots/<name>/<subvol>
    suffix. Destructive actions therefore stay rooted under the configured
    destination.target_root after a backup move.
    """

    rel = destination_path_to_relative(Path(stored_path), target_root)
    return Path(target_root) / rel


def normalize_destination_paths(state: dict[str, Any], target_root: Path) -> dict[str, Any]:
    """Normalize in-memory state paths to target_root-relative values.

    Invalid legacy paths are left untouched so the later operational check can
    report a precise error instead of silently rewriting an unknown path.
    """

    snapshots = state.setdefault("snapshots", {})
    for snapshot_name, item in snapshots.items():
        if isinstance(item, dict):
            item["path"] = (Path("snapshots") / str(snapshot_name)).as_posix()
            for sub in item.get("subvolumes", {}).values():
                if not isinstance(sub, dict):
                    continue
                destination_path = sub.get("destination_path")
                if not isinstance(destination_path, str) or not destination_path:
                    continue
                try:
                    sub["destination_path"] = destination_path_to_relative(Path(destination_path), target_root)
                except ValueError:
                    # Keep the original value. The caller that tries to use it
                    # will raise/report the exact path problem.
                    pass
    return state


def load_state(path: Path, target_root: Path | None = None) -> dict[str, Any]:
    """Load state.json or return empty state.

    When target_root is provided, destination paths are normalized in memory to
    the current target-root-relative format.
    """

    if not path.exists():
        data = empty_state()
    else:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = empty_state()
    data.setdefault("version", STATE_VERSION)
    data.setdefault("snapshots", {})
    if target_root is not None:
        normalize_destination_paths(data, target_root)
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


def refresh_snapshot_metadata_from_source(state: dict[str, Any], snapshots: Iterable[SnapshotMeta]) -> list[str]:
    """Refresh mutable Timeshift metadata for already-known snapshots.

    Timeshift can later change snapshot tags/comments, for example promoting an
    existing daily snapshot to weekly/monthly retention. Those changes should
    update state.json without re-sending the snapshot and without touching the
    Btrfs UUID/parent/send metadata that proves transfer identity.

    Matching is by Timeshift snapshot name/timestamp. Only snapshot-level
    mutable metadata is changed:
      * tags
      * comment
      * created
      * path

    ``path`` is the destination target-root-relative snapshot directory used in
    state.json, not the source Timeshift path.

    Returns the sorted snapshot names whose metadata changed.
    """

    snapshots_state = state.setdefault("snapshots", {})
    changed: list[str] = []
    for snapshot in snapshots:
        item = snapshots_state.get(snapshot.name)
        if not isinstance(item, dict):
            continue
        new_values = {
            "tags": list(snapshot.tags),
            "comment": snapshot.comment,
            "created": snapshot.created,
            "path": (Path("snapshots") / snapshot.name).as_posix(),
        }
        touched = False
        for key, value in new_values.items():
            if item.get(key) != value:
                item[key] = value
                touched = True
        if touched:
            item.setdefault("name", snapshot.name)
            item.setdefault("subvolumes", {})
            changed.append(snapshot.name)
    return sorted(changed)


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
    destination_root: Path,
    parent_snapshot: str | None,
    parent_source_path: str | None,
    send_path: str,
    received_meta: SubvolumeMeta | None,
    original_meta: SubvolumeMeta | None = None,
    send_meta: SubvolumeMeta | None = None,
) -> None:
    """Record one successful send/receive in state.

    `destination_path` is stored relative to `destination_root`. This keeps
    state.json portable when the whole backup target is moved to another mount
    point and only destination.target_root changes in the config.

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
    snap_state["path"] = (Path("snapshots") / snapshot.name).as_posix()
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
        "destination_path": destination_path_to_relative(destination_path, destination_root),
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


def refresh_state_metadata_and_report(
    state: dict[str, Any], snapshots: Iterable[SnapshotMeta], state_file: Path, *, dry_run: bool
) -> list[str]:
    """Refresh only Timeshift tags/comment/created/path, report, and save."""

    changed = refresh_snapshot_metadata_from_source(state, snapshots)
    if not changed:
        return []
    print(
        "STATE METADATA REFRESH\n"
        "  source: latest Timeshift --list metadata\n"
        "  updated fields: tags, comment, created, path\n"
        "  preserved fields: UUIDs, parent chain, send paths, destination paths, status\n"
        f"  snapshot(s): {', '.join(changed)}"
    )
    if dry_run:
        print("  dry-run: state.json would be updated, but was not written")
    else:
        save_state(state_file, state)
        print("  state.json updated")
    print()
    return changed


def latest_synced_before(state: dict[str, Any], snapshot_name: str, subvolume_name: str, source_names: set[str] | None = None) -> tuple[str, dict[str, Any]] | None:
    """Return newest older synced parent candidate.

    A parent may be usable even after Timeshift removed the original source
    snapshot, as long as state still has an app-created read-only send_path.
    """

    candidates: list[tuple[str, dict[str, Any]]] = []
    for name, item in state.get("snapshots", {}).items():
        if name >= snapshot_name:
            continue
        sub = item.get("subvolumes", {}).get(subvolume_name)
        if not sub or sub.get("status") != "ok":
            continue
        if source_names and name not in source_names and not sub.get("send_path"):
            continue
        candidates.append((name, sub))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[-1]
