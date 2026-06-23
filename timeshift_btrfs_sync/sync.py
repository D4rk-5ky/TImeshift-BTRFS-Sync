"""Main destination-pull sync workflow."""

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
    """Create/validate destination directories and set compression property."""

    root = config.destination.target_root
    if root.exists():
        if not root.is_dir():
            raise SyncError(f"Destination target_root exists but is not a directory: {root}")
    elif config.destination.create_target_root:
        root.mkdir(parents=True, exist_ok=True)
    else:
        raise SyncError(f"Destination target_root does not exist: {root}")
    snapshots_root = root / "snapshots"
    snapshots_root.mkdir(parents=True, exist_ok=True)
    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)

    # Best-effort compression property for future received writes.
    btrfs.set_local_compression(root, config.destination.sudo, config.destination.btrfs_command, config.destination.compression)
    btrfs.set_local_compression(snapshots_root, config.destination.sudo, config.destination.btrfs_command, config.destination.compression)


def list_source_snapshots(config: AppConfig, ssh: SSHRunner, *, include_btrfs_info: bool = True) -> list[SnapshotMeta]:
    """Discover source Timeshift snapshots."""

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
    """Print source snapshots in table form."""

    if not snapshots:
        print("No source snapshots found.")
        return
    print(f"{'SNAPSHOT':<22} {'TAGS':<8} {'SUBVOLUMES':<20} COMMENT")
    for snap in snapshots:
        print(f"{snap.name:<22} {''.join(snap.tags) or '-':<8} {','.join(snap.subvolumes.keys()) or '-':<20} {snap.comment or ''}")


def _dest_subvolume_path(config: AppConfig, snapshot_name: str, subvolume_name: str) -> Path:
    """Return the final local path for one received subvolume.

    Example:
      <target_root>/snapshots/2026-06-22_18-00-01/@
    """

    return config.destination.target_root / "snapshots" / snapshot_name / subvolume_name


def _target_snapshot_dir(config: AppConfig, snapshot_name: str) -> Path:
    """Return the local directory passed to `btrfs receive`.

    `btrfs receive <dir>` creates the incoming subvolume inside this directory.
    """

    return config.destination.target_root / "snapshots" / snapshot_name


def _preview_send_path(config: AppConfig, snapshot_name: str, subvolume: SubvolumeMeta) -> str:
    """Return the send path that would be used, without creating cache snapshots.

    Dry-run uses this so it can show paths without changing source or
    destination.
    """

    if subvolume.readonly is True:
        return subvolume.path
    if config.source.cache_root:
        return btrfs.readonly_cache_path(config.source.cache_root, snapshot_name, subvolume.name)
    return "<no-cache-root-configured>"


def _ensure_source_send_path(config: AppConfig, ssh: SSHRunner, snapshot_name: str, subvolume: SubvolumeMeta) -> str:
    """Return a real read-only source path, creating cache snapshots if needed.

    This calls only remote `sudo btrfs ...` commands. It never uses source-side
    mkdir/cat/find/helper scripts.
    """

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


def _select_parent(config: AppConfig, ssh: SSHRunner, state: dict, source_by_name: dict[str, SnapshotMeta], snapshot: SnapshotMeta, subvolume_name: str, *, dry_run: bool) -> tuple[str | None, str | None]:
    """Choose newest valid incremental parent for one subvolume."""

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


def sync_once(config: AppConfig, state: dict, *, dry_run: bool, limit: int | None = None, only_snapshot: str | None = None, only_missing: bool = True) -> int:
    """Run one sync pass.

    A sync pass processes source snapshots oldest-to-newest. After every
    successful subvolume receive, state.json is updated immediately. That is how
    later snapshots in the same run can become incremental.
    """

    prepare_destination(config)

    # Create one SSH runner. The same runner config supplies the SSH command and
    # optional SSHPASS environment for normal commands and streaming sends.
    ssh = SSHRunner(config.ssh)
    ssh.test()

    # Discovery uses Timeshift for names/tags and Btrfs for subvolume metadata.
    print("Reading source Timeshift snapshots with sudo timeshift --list...")
    snapshots = [snap for snap in list_source_snapshots(config, ssh, include_btrfs_info=True) if snap.subvolumes]
    source_by_name = {snap.name: snap for snap in snapshots}

    if only_snapshot:
        snapshots = [snap for snap in snapshots if snap.name == only_snapshot]
        if not snapshots:
            raise SyncError(f"Source snapshot not found: {only_snapshot}")

    transferred = 0
    for snapshot in sorted(snapshots, key=lambda s: s.sort_key()):
        expected = [name for name in config.source.subvolumes if name in snapshot.subvolumes]
        if only_missing and snapshot_is_synced(state, snapshot.name, expected):
            continue
        if limit is not None and transferred >= limit:
            break

        target_dir = _target_snapshot_dir(config, snapshot.name)
        print(f"Snapshot {snapshot.name} tags={''.join(snapshot.tags) or '-'}")
        if dry_run:
            print(f"  would ensure local directory: {target_dir}")
            if config.destination.compression:
                print(f"  would set destination compression property: {config.destination.compression}")
        else:
            target_dir.mkdir(parents=True, exist_ok=True)
            if config.destination.set_compression_before_receive:
                btrfs.set_local_compression(target_dir, config.destination.sudo, config.destination.btrfs_command, config.destination.compression)

        for subvol_name in config.source.subvolumes:
            subvolume = snapshot.subvolumes.get(subvol_name)
            if not subvolume:
                continue
            dest_path = _dest_subvolume_path(config, snapshot.name, subvol_name)
            already = state.get("snapshots", {}).get(snapshot.name, {}).get("subvolumes", {}).get(subvol_name)
            if already and already.get("status") == "ok" and dest_path.exists():
                print(f"  {subvol_name}: already synced")
                continue
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
                if config.stream.use_mbuffer:
                    print(f"    stream: would use {' '.join(config.stream.command() or [])}")
                continue

            print(f"  {subvol_name}: {mode} send/receive")
            # Build remote send command. If parent_send_path is set, btrfs send
            # receives `-p <parent>` and sends an incremental stream.
            send_cmd = btrfs.remote_send_cmd(
                ssh,
                sudo=config.source.sudo,
                btrfs_command=config.source.btrfs_command,
                current_path=current_send_path,
                parent_path=parent_send_path,
                compressed_data=config.source.send_compressed_data,
                proto=config.source.send_proto,
            )

            # Build local receive command. Compression properties were set on
            # the target directory before receive if configured.
            receive_cmd = btrfs.local_receive_cmd(target_dir, config.destination.sudo, config.destination.btrfs_command)

            # Optional mbuffer is inserted as the middle command. Password auth
            # environment is passed to the SSH side so streamed sends work with
            # sshpass too.
            stream_pipeline(send_cmd, receive_cmd, middle_cmd=config.stream.command(), verbose=True, left_env=ssh.environment())

            received_meta = None
            if dest_path.exists():
                # After receive, optionally set compression on the received
                # subvolume itself. This is best-effort and should not affect
                # incremental parent validity.
                if config.destination.set_compression_after_receive:
                    btrfs.set_local_compression(dest_path, config.destination.sudo, config.destination.btrfs_command, config.destination.compression)
                try:
                    received_meta = btrfs.local_subvolume_show(dest_path, config.destination.sudo, subvol_name, config.destination.btrfs_command)
                    received_meta.readonly = btrfs.local_readonly(dest_path, config.destination.sudo, config.destination.btrfs_command)
                except Exception:
                    received_meta = None
            mark_subvolume_synced(state, snapshot=snapshot, subvolume=subvolume, destination_path=dest_path, parent_snapshot=parent_name, parent_source_path=parent_send_path, send_path=current_send_path, received_meta=received_meta)
            save_state(config.state_file, state)
            transferred += 1

    print("No missing subvolumes to sync." if transferred == 0 else f"Synced {transferred} subvolume(s).")
    return transferred
