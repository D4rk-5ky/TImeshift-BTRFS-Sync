"""Main destination-pull sync workflow.

This module coordinates the full process:
  1. Prepare destination folders.
  2. Ask the source over SSH for Timeshift snapshots.
  3. Pick full or incremental btrfs send for each missing subvolume.
  4. Pipe remote `btrfs send` into local `btrfs receive`.
  5. Save successful transfers to state.json.
"""

from __future__ import annotations

from pathlib import Path
import shlex

from . import btrfs, timeshift
from .commands import stream_pipeline
from .config import AppConfig
from .models import SnapshotMeta, SubvolumeMeta
from .ssh import SSHRunner
from .state import latest_synced_before, mark_subvolume_synced, save_state, snapshot_is_synced


class SyncError(RuntimeError):
    """Raised for sync-specific safety errors."""


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

    # Received snapshots go under snapshots/. App metadata goes under the
    # hidden .ts-btrfs-sync directory configured in config.py.
    (root / "snapshots").mkdir(parents=True, exist_ok=True)
    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)


def list_source_snapshots(config: AppConfig, ssh: SSHRunner, *, include_btrfs_info: bool = True) -> list[SnapshotMeta]:
    """Return Timeshift snapshots from the source using the configured paths."""

    return timeshift.list_remote_snapshots(
        ssh,
        snapshot_root=config.source.snapshot_root,
        subvolumes=config.source.subvolumes,
        sudo=config.source.sudo,
        include_btrfs_info=include_btrfs_info,
    )


def print_snapshot_table(snapshots: list[SnapshotMeta]) -> None:
    """Pretty-print discovered source snapshots for `list-source`."""

    if not snapshots:
        print("No source snapshots found.")
        return
    print(f"{'SNAPSHOT':<22} {'TAGS':<8} {'SUBVOLUMES':<20} COMMENT")
    for snap in snapshots:
        tags = "".join(snap.tags) or "-"
        subvols = ",".join(snap.subvolumes.keys()) or "-"
        comment = snap.comment or ""
        print(f"{snap.name:<22} {tags:<8} {subvols:<20} {comment}")


def _copy_remote_info_json(config: AppConfig, ssh: SSHRunner, snapshot: SnapshotMeta, target_dir: Path) -> None:
    """Copy Timeshift's info.json beside the received backup snapshot."""

    remote_info = f"{snapshot.path}/info.json"
    result = ssh.run(f"cat {shlex.quote(remote_info)}", check=False)
    if result.returncode == 0 and result.stdout:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "info.json").write_text(result.stdout, encoding="utf-8")


def _dest_subvolume_path(config: AppConfig, snapshot_name: str, subvolume_name: str) -> Path:
    """Return where one received subvolume should exist locally."""

    return config.destination.target_root / "snapshots" / snapshot_name / subvolume_name


def _target_snapshot_dir(config: AppConfig, snapshot_name: str) -> Path:
    """Return the local directory that receives one Timeshift snapshot."""

    return config.destination.target_root / "snapshots" / snapshot_name


def _cache_root(config: AppConfig) -> str:
    """Return the source-side cache root for read-only send snapshots."""

    return config.source.cache_root or timeshift.default_cache_root(config.source.snapshot_root)


def _ensure_source_send_path(
    config: AppConfig,
    ssh: SSHRunner,
    snapshot_name: str,
    subvolume: SubvolumeMeta,
) -> str:
    """Return a real remote path safe for btrfs send.

    This may create a read-only cache snapshot on the source if the Timeshift
    subvolume itself is writable.
    """

    cache_path = timeshift.cache_path_for(_cache_root(config), snapshot_name, subvolume.name)
    return btrfs.remote_ensure_readonly_send_path(
        ssh,
        config.source.sudo,
        subvolume.path,
        cache_path,
    )


def _preview_source_send_path(config: AppConfig, snapshot_name: str, subvolume: SubvolumeMeta) -> str:
    """Predict the send path for dry-run output without creating anything."""

    if subvolume.readonly is True:
        return subvolume.path
    return timeshift.cache_path_for(_cache_root(config), snapshot_name, subvolume.name)


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
    """Choose the newest valid incremental parent for one subvolume.

    Returns `(parent_snapshot_name, parent_send_path)`. If no safe parent exists,
    both values are None and the caller will do a full send.
    """

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

    # In real mode we ensure the parent has a read-only send path before using
    # it with `btrfs send -p`. In dry-run mode we only show the path that would
    # be used.
    if dry_run:
        parent_send_path = _preview_source_send_path(config, parent_name, parent_subvol)
    else:
        parent_send_path = _ensure_source_send_path(config, ssh, parent_name, parent_subvol)
    return parent_name, parent_send_path


def sync_once(
    config: AppConfig,
    state: dict,
    *,
    dry_run: bool,
    limit: int | None = None,
    only_snapshot: str | None = None,
    only_missing: bool = True,
) -> int:
    """Run one sync pass and return number of transferred subvolumes."""

    prepare_destination(config)
    ssh = SSHRunner(config.ssh)
    ssh.test()

    # Get source snapshot metadata, including Btrfs UUIDs/read-only flags.
    print("Reading source Timeshift snapshots...")
    snapshots = list_source_snapshots(config, ssh, include_btrfs_info=True)
    snapshots = [snap for snap in snapshots if snap.subvolumes]
    source_by_name = {snap.name: snap for snap in snapshots}

    # Optional CLI filter for testing one snapshot at a time.
    if only_snapshot:
        snapshots = [snap for snap in snapshots if snap.name == only_snapshot]
        if not snapshots:
            raise SyncError(f"Source snapshot not found: {only_snapshot}")

    transferred = 0
    for snapshot in sorted(snapshots, key=lambda s: s.sort_key()):
        # Determine which configured subvolumes actually exist in this snapshot.
        expected = [name for name in config.source.subvolumes if name in snapshot.subvolumes]

        # Skip completed snapshots unless --resend was used.
        if only_missing and snapshot_is_synced(state, snapshot.name, expected):
            continue

        # --limit is useful for first live testing, e.g. transfer only one
        # subvolume and inspect the destination before continuing.
        if limit is not None and transferred >= limit:
            break

        target_dir = _target_snapshot_dir(config, snapshot.name)
        print(f"Snapshot {snapshot.name} tags={''.join(snapshot.tags) or '-'}")

        if dry_run:
            print(f"  would ensure local directory: {target_dir}")
        else:
            target_dir.mkdir(parents=True, exist_ok=True)
            _copy_remote_info_json(config, ssh, snapshot, target_dir)

        for subvol_name in config.source.subvolumes:
            subvolume = snapshot.subvolumes.get(subvol_name)
            if not subvolume:
                continue

            dest_path = _dest_subvolume_path(config, snapshot.name, subvol_name)
            already = state.get("snapshots", {}).get(snapshot.name, {}).get("subvolumes", {}).get(subvol_name)

            # If state says it is OK and the path exists, skip it.
            if already and already.get("status") == "ok" and dest_path.exists():
                print(f"  {subvol_name}: already synced")
                continue

            # If a destination path exists but state does not trust it, stop.
            # This avoids overwriting user data or colliding with a failed run.
            if dest_path.exists() and not dry_run:
                raise SyncError(f"Destination path already exists but is not recorded as synced: {dest_path}")

            parent_name, parent_send_path = _select_parent(
                config,
                ssh,
                state,
                source_by_name,
                snapshot,
                subvol_name,
                dry_run=dry_run,
            )

            # Choose the current send path. It may be the original Timeshift
            # subvolume or an app-created read-only cache snapshot.
            if dry_run:
                current_send_path = _preview_source_send_path(config, snapshot.name, subvolume)
            else:
                current_send_path = _ensure_source_send_path(config, ssh, snapshot.name, subvolume)
            mode = "incremental" if parent_send_path else "full"

            if dry_run:
                parent_text = f" parent={parent_name}" if parent_name else ""
                print(f"  {subvol_name}: would {mode} send{parent_text}")
                print(f"    source: {current_send_path}")
                print(f"    dest:   {dest_path}")
                continue

            print(f"  {subvol_name}: {mode} send/receive")

            # Build both sides of the streaming pipeline:
            #   ssh source 'btrfs send ...' | btrfs receive target_dir
            send_cmd = btrfs.remote_send_cmd(
                ssh,
                config.source.sudo,
                current_send_path,
                parent_path=parent_send_path,
            )
            receive_cmd = btrfs.local_receive_cmd(target_dir, config.destination.sudo)
            stream_pipeline(send_cmd, receive_cmd, verbose=True)

            # Read metadata from the received subvolume for state/debugging. If
            # this fails after a successful receive, the transfer is still saved
            # but destination UUID fields will be None.
            received_meta = None
            if dest_path.exists():
                try:
                    received_meta = btrfs.local_subvolume_show(dest_path, config.destination.sudo, subvol_name)
                    received_meta.readonly = btrfs.local_readonly(dest_path, config.destination.sudo)
                except Exception:
                    received_meta = None

            # Mark success only after btrfs receive completed. This is the most
            # important safety rule for reliable incremental backups.
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

    if transferred == 0:
        print("No missing subvolumes to sync.")
    else:
        print(f"Synced {transferred} subvolume(s).")
    return transferred
