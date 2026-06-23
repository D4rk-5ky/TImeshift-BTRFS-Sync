"""Main destination-pull sync workflow.

This module coordinates the backup process:
  1. Prepare local destination paths.
  2. Discover source Timeshift snapshots through SSH.
  3. Choose full or incremental Btrfs send.
  4. Stream remote `btrfs send` into local `btrfs receive`.
  5. Save successful transfers to state.json.
"""

from __future__ import annotations

from pathlib import Path

from . import btrfs, timeshift
from .commands import stream_pipeline
from .config import AppConfig
from .models import SnapshotMeta, SubvolumeMeta
from .ssh import SSHRunner
from .state import latest_synced_before, mark_subvolume_synced, save_state, snapshot_is_synced


class SyncError(RuntimeError):
    """Raised for sync safety errors."""


def prepare_destination(config: AppConfig) -> None:
    """Create/validate local destination folders before syncing."""

    root = config.destination.target_root
    if root.exists():
        if not root.is_dir():
            raise SyncError(f"Destination target_root exists but is not a directory: {root}")
    elif config.destination.create_target_root:
        root.mkdir(parents=True, exist_ok=True)
    else:
        raise SyncError(f"Destination target_root does not exist: {root}")

    # Normal backup layout and hidden app metadata layout.
    (root / "snapshots").mkdir(parents=True, exist_ok=True)
    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)


def list_source_snapshots(config: AppConfig, ssh: SSHRunner, *, include_btrfs_info: bool = True) -> list[SnapshotMeta]:
    """Discover snapshots from the source using Timeshift and Btrfs."""

    return timeshift.list_remote_snapshots(
        ssh,
        snapshot_root=config.source.snapshot_root,
        subvolumes=config.source.subvolumes,
        sudo=config.source.sudo,
        timeshift_command=config.source.timeshift_command,
        btrfs_command=config.source.btrfs_command,
        include_btrfs_info=include_btrfs_info,
    )


def print_snapshot_table(snapshots: list[SnapshotMeta]) -> None:
    """Print source snapshots in a compact table."""

    if not snapshots:
        print("No source snapshots found.")
        return
    print(f"{'SNAPSHOT':<22} {'TAGS':<8} {'SUBVOLUMES':<20} COMMENT")
    for snap in snapshots:
        print(f"{snap.name:<22} {''.join(snap.tags) or '-':<8} {','.join(snap.subvolumes.keys()) or '-':<20} {snap.comment or ''}")


def _dest_subvolume_path(config: AppConfig, snapshot_name: str, subvolume_name: str) -> Path:
    """Return where one received destination subvolume should live."""

    return config.destination.target_root / "snapshots" / snapshot_name / subvolume_name


def _target_snapshot_dir(config: AppConfig, snapshot_name: str) -> Path:
    """Return the local destination snapshot directory."""

    return config.destination.target_root / "snapshots" / snapshot_name


def _preview_send_path(config: AppConfig, snapshot_name: str, subvolume: SubvolumeMeta) -> str:
    """Return the source path that would be used, without creating anything."""

    if subvolume.readonly is True:
        return subvolume.path
    if config.source.cache_root:
        return btrfs.readonly_cache_path(config.source.cache_root, snapshot_name, subvolume.name)
    return "<no-cache-root-configured>"


def _ensure_source_send_path(config: AppConfig, ssh: SSHRunner, snapshot_name: str, subvolume: SubvolumeMeta) -> str:
    """Ensure and return a read-only source path for btrfs send."""

    return btrfs.remote_ensure_readonly_send_path(
        ssh,
        sudo=config.source.sudo,
        btrfs_command=config.source.btrfs_command,
        original_path=subvolume.path,
        cache_root=config.source.cache_root,
        snapshot_name=snapshot_name,
        subvolume_name=subvolume.name,
        create_readonly_cache=config.source.create_readonly_cache,
    )


def _select_parent(
    config: AppConfig,
    ssh: SSHRunner,
    state: dict,
    source_by_name: dict[str, SnapshotMeta],
    snapshot: SnapshotMeta,
    subvolume_name: str,
    *,
    dry_run: bool,
) -> tuple[str | None, str | None]:
    """Choose the newest valid incremental parent for one subvolume."""

    parent = latest_synced_before(state, snapshot.name, subvolume_name, set(source_by_name.keys()))
    if not parent:
        return None, None
    parent_name, _parent_state = parent
    parent_snapshot = source_by_name.get(parent_name)
    if not parent_snapshot:
        return None, None
    parent_subvol = parent_snapshot.subvolumes.get(subvolume_name)
    if not parent_subvol:
        return None, None
    if dry_run:
        return parent_name, _preview_send_path(config, parent_name, parent_subvol)
    return parent_name, _ensure_source_send_path(config, ssh, parent_name, parent_subvol)


def sync_once(
    config: AppConfig,
    state: dict,
    *,
    dry_run: bool,
    limit: int | None = None,
    only_snapshot: str | None = None,
    only_missing: bool = True,
) -> int:
    """Run one sync pass and return the number of transferred subvolumes."""

    prepare_destination(config)
    ssh = SSHRunner(config.ssh)
    ssh.test()

    # Discovery uses only sudo timeshift and sudo btrfs on the source.
    print("Reading source Timeshift snapshots with sudo timeshift --list...")
    snapshots = [snap for snap in list_source_snapshots(config, ssh, include_btrfs_info=True) if snap.subvolumes]
    source_by_name = {snap.name: snap for snap in snapshots}

    # Optional test/debug filter for a single snapshot.
    if only_snapshot:
        snapshots = [snap for snap in snapshots if snap.name == only_snapshot]
        if not snapshots:
            raise SyncError(f"Source snapshot not found: {only_snapshot}")

    transferred = 0
    for snapshot in sorted(snapshots, key=lambda s: s.sort_key()):
        expected = [name for name in config.source.subvolumes if name in snapshot.subvolumes]

        # Skip if state already says this snapshot/subvolume set is complete.
        if only_missing and snapshot_is_synced(state, snapshot.name, expected):
            continue
        if limit is not None and transferred >= limit:
            break

        target_dir = _target_snapshot_dir(config, snapshot.name)
        print(f"Snapshot {snapshot.name} tags={''.join(snapshot.tags) or '-'}")
        if dry_run:
            print(f"  would ensure local directory: {target_dir}")
        else:
            target_dir.mkdir(parents=True, exist_ok=True)

        for subvol_name in config.source.subvolumes:
            subvolume = snapshot.subvolumes.get(subvol_name)
            if not subvolume:
                continue

            dest_path = _dest_subvolume_path(config, snapshot.name, subvol_name)
            already = state.get("snapshots", {}).get(snapshot.name, {}).get("subvolumes", {}).get(subvol_name)
            if already and already.get("status") == "ok" and dest_path.exists():
                print(f"  {subvol_name}: already synced")
                continue

            # Do not overwrite unexpected local paths. This usually means an old
            # failed run or manually-created data needs to be inspected.
            if dest_path.exists() and not dry_run:
                raise SyncError(f"Destination path already exists but is not recorded as synced: {dest_path}")

            parent_name, parent_send_path = _select_parent(config, ssh, state, source_by_name, snapshot, subvol_name, dry_run=dry_run)
            current_send_path = _preview_send_path(config, snapshot.name, subvolume) if dry_run else _ensure_source_send_path(config, ssh, snapshot.name, subvolume)
            mode = "incremental" if parent_send_path else "full"

            if dry_run:
                parent_text = f" parent={parent_name}" if parent_name else ""
                print(f"  {subvol_name}: would {mode} send{parent_text}")
                print(f"    source: {current_send_path}")
                print(f"    dest:   {dest_path}")
                continue

            print(f"  {subvol_name}: {mode} send/receive")
            send_cmd = btrfs.remote_send_cmd(
                ssh,
                sudo=config.source.sudo,
                btrfs_command=config.source.btrfs_command,
                current_path=current_send_path,
                parent_path=parent_send_path,
            )
            receive_cmd = btrfs.local_receive_cmd(target_dir, config.destination.sudo)
            stream_pipeline(send_cmd, receive_cmd, verbose=True)

            # Try to read destination metadata for state/debugging. If this read
            # fails after successful receive, state is still recorded with None UUIDs.
            received_meta = None
            if dest_path.exists():
                try:
                    received_meta = btrfs.local_subvolume_show(dest_path, config.destination.sudo, subvol_name)
                    received_meta.readonly = btrfs.local_readonly(dest_path, config.destination.sudo)
                except Exception:
                    received_meta = None

            # State is updated only after the receive pipeline succeeded.
            mark_subvolume_synced(
                state,
                snapshot=snapshot,
                subvolume=subvolume,
                destination_path=dest_path,
                parent_snapshot=parent_name,
                parent_source_path=parent_send_path,
                send_path=current_send_path,
                received_meta=received_meta,
            )
            save_state(config.state_file, state)
            transferred += 1

    print("No missing subvolumes to sync." if transferred == 0 else f"Synced {transferred} subvolume(s).")
    return transferred
