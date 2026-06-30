"""Main destination-pull sync workflow.

The important performance/safety rule in this version is:

* Discovery is fast and only uses Timeshift names plus configured subvolume
  names. It does not run `btrfs subvolume show` for every snapshot unless
  source.verify_subvolumes_at_discovery is enabled.
* Before the first real incremental send for each subvolume name in a run, the
  selected parent is always verified with Btrfs metadata on both sides. Later
  incrementals in the same run reuse that verified chain and only refresh local
  destination metadata after receive.
"""

from __future__ import annotations

from pathlib import Path
from . import btrfs, timeshift
from . import preflight, remote_index
from .commands import stream_pipeline
from .config import AppConfig
from .models import SnapshotMeta, SubvolumeMeta, tags_text
from .source import SourceRunner
from .log import emit_success_summary
from .retention import initial_sync_keep_names
from .state import latest_synced_before, mark_subvolume_synced, refresh_state_metadata_and_report, resolve_destination_path, save_state, snapshot_is_synced


class SyncError(RuntimeError):
    """Raised for sync safety errors."""


def _local_meta(config: AppConfig, path: str | Path, name: str, required: bool = True) -> SubvolumeMeta | None:
    return btrfs.get_subvolume_meta("local", path, name, config.destination.sudo, config.destination.btrfs_command, required=required)


def _source_meta(config: AppConfig, source: SourceRunner, path: str | Path, name: str, required: bool = True) -> SubvolumeMeta | None:
    return btrfs.source_get_subvolume_meta(source, path, name, config.source.sudo, config.source.btrfs_command, required=required)


def _human_blank() -> None:
    """Print one blank line to separate human-readable status blocks."""

    print()


def _human_rule(text: str = "----") -> None:
    """Print a visual separator with blank lines around it."""

    print()
    print(text)
    print()



def _record_sync_event(
    events: list[dict],
    *,
    mode: str,
    snapshot: SnapshotMeta,
    subvolume_name: str,
    source_path: str,
    destination_path: Path,
    parent_name: str | None,
    parent_send_path: str | None,
    status: str,
) -> None:
    """Add one planned or completed transfer to the run summary."""

    events.append(
        {
            "mode": mode,
            "snapshot": snapshot.name,
            "tags": tags_text(snapshot.tags),
            "subvolume": subvolume_name,
            "source": source_path,
            "destination": str(destination_path),
            "parent": parent_name or "-",
            "parent_source": parent_send_path or "-",
            "status": status,
        }
    )


def _print_sync_summary(
    events: list[dict],
    *,
    dry_run: bool,
    skipped_by_floor: int,
    already_synced: int,
) -> None:
    """Write a terminal-friendly transfer summary to terminal and .succes.

    The readable statistics intentionally go to the separate .succes file, not
    the normal .log file. Mail uses .succes as the plain-text success body.
    """

    full_count = sum(1 for event in events if event.get("mode") == "full")
    incremental_count = sum(1 for event in events if event.get("mode") == "incremental")
    mode_text = "dry-run plan" if dry_run else "completed transfers"
    lines = [
        "SYNC SUMMARY",
        "============",
        f"  mode:              {mode_text}",
        f"  full syncs:        {full_count}",
        f"  incremental syncs: {incremental_count}",
        f"  total listed:      {len(events)}",
        f"  already synced:    {already_synced}",
        f"  skipped by floor:  {skipped_by_floor}",
    ]

    if not events:
        lines += ["  transfers:         none", ""]
        emit_success_summary("\n".join(lines))
        return

    lines += ["", "SYNC TRANSFERS", "--------------"]
    for event in events:
        action = "FULL SYNC" if event["mode"] == "full" else "INCREMENTAL SYNC"
        if dry_run:
            action = "WOULD " + action
        lines.append(f"  [{action}] {event['snapshot']}  subvol={event['subvolume']}  tags={event['tags']}")
        lines.append(f"      parent:      {event['parent']}")
        if event["parent_source"] != "-":
            lines.append(f"      parent path: {event['parent_source']}")
        lines.append(f"      source:      {event['source']}")
        lines.append(f"      destination: {event['destination']}")
    lines.append("")
    emit_success_summary("\n".join(lines))

def prepare_destination(config: AppConfig) -> None:
    """Create/validate destination helper folders before writes.

    The destination target root itself is handled by sync path preflight. Helper
    folders such as ``snapshots/``, the state/lock directory, and optional
    ``log_dir`` are accepted as either ordinary directories or Btrfs subvolumes.
    When missing, the app tries ``btrfs subvolume create`` first and falls back
    to mkdir if Btrfs creation is not possible at that location.
    """

    root = config.destination.target_root
    if not root.exists():
        raise SyncError(f"Destination target_root was not created by preflight: {root}")
    if not root.is_dir():
        raise SyncError(f"Destination target_root exists but is not a directory: {root}")
    try:
        preflight.prepare_destination_helper_paths(config, dry_run=False)
    except preflight.PathPreflightError as exc:
        raise SyncError(str(exc)) from exc


def list_source_snapshots(config: AppConfig, source: SourceRunner, *, include_btrfs_info: bool = True) -> list[SnapshotMeta]:
    """Discover source Timeshift snapshots."""

    return timeshift.list_source_snapshots(
        source,
        snapshot_root=config.source.snapshot_root,
        subvolumes=config.source.subvolumes,
        sudo=config.source.sudo,
        timeshift_command=config.source.timeshift_command,
        btrfs_command=config.source.btrfs_command,
        include_btrfs_info=include_btrfs_info,
    )


def source_snapshot_index(snapshots) -> dict[str, SnapshotMeta]:
    return {snap.name: snap for snap in snapshots if snap.subvolumes}


def confirm_source_identity_before_manual_snapshot(
    config: AppConfig,
    source: SourceRunner,
    state: dict,
    source_by_name: dict[str, SnapshotMeta] | None = None,
    load_source_index=None,
    source_cache_index: remote_index.BtrfsIndex | None = None,
    destination_index: remote_index.BtrfsIndex | None = None,
) -> tuple[str | None, str]:
    """Print and enforce the shared manual-snapshot source identity guard."""

    print("MANUAL SNAPSHOT SOURCE IDENTITY CHECK")
    if not _destination_has_existing_snapshots(config):
        print("  destination: no existing snapshots found")
        print("  first full seed is allowed; later snapshots in the same run become incremental")
        return None, "empty destination; first full seed allowed"

    print("  destination: existing snapshots found")
    print("  checking existing source Timeshift list against state.json UUID history")
    if source_by_name is None:
        if load_source_index is None:
            raise SyncError("Internal error: source Timeshift index is required for manual snapshot identity check")
        source_by_name = load_source_index()

    confirmed_name, reason = _find_confirmed_sync_floor(
        config,
        source,
        state,
        source_by_name,
        source_cache_index=source_cache_index,
        destination_index=destination_index,
    )
    if not confirmed_name:
        raise SyncError(
            "Refusing to create manual Timeshift snapshot.\n\n"
            "The destination already contains snapshots, but the configured source "
            "could not be matched to any already received snapshot in state.json.\n"
            "This may be the wrong mounted OS, wrong snapshot_root, wrong source host, "
            "or a backup target from another source.\n"
            f"Reason: {reason}\n\n"
            "Use an empty/separate target_root for a new full backup, or repair "
            "state/cache so a matching source/destination parent can be proven."
        )
    print(f"  confirmed source anchor: {confirmed_name}")
    print(f"  reason: {reason}")
    return confirmed_name, reason



def _is_app_manual_snapshot(snapshot: SnapshotMeta, marker: str) -> bool:
    """Return True for source Timeshift O snapshots created by this app.

    The app cannot rely on state.json for interrupted runs, because an on-demand
    snapshot may have been created before any destination receive completed. The
    source Timeshift list still contains the comment/tag, so this source-side
    check lets the next run notice older pending app-created snapshots and keep
    them in the normal oldest-to-newest send order.
    """

    marker_text = (marker or "").strip().lower()
    if not marker_text:
        return False
    return "O" in snapshot.tags and marker_text in str(snapshot.comment or "").lower()


def _pending_app_manual_snapshots(
    config: AppConfig,
    state: dict,
    source_by_name: dict[str, SnapshotMeta],
) -> list[SnapshotMeta]:
    """Return app-created on-demand snapshots that still need syncing.

    This protects interrupted retry behavior. If a previous run created an
    automatic Timeshift on-demand snapshot and then failed before completing the
    send/receive, the next run should still process that existing source
    snapshot in normal oldest-to-newest order. It must not suppress creation of
    a fresh on-demand snapshot, because the previous one may be old.
    """

    pending: list[SnapshotMeta] = []
    for snapshot in source_by_name.values():
        if not _is_app_manual_snapshot(snapshot, config.manual_snapshot.marker):
            continue
        expected = [name for name in config.source.subvolumes if name in snapshot.subvolumes]
        if not expected:
            continue
        if not snapshot_is_synced(state, snapshot.name, expected):
            pending.append(snapshot)
    return _snapshots_in_sync_order(pending)

def _maybe_create_manual_snapshot(
    config: AppConfig,
    source: SourceRunner,
    *,
    state: dict,
    source_by_name: dict[str, SnapshotMeta],
    dry_run: bool,
    only_snapshot: str | None,
    source_cache_index: remote_index.BtrfsIndex | None = None,
    destination_index: remote_index.BtrfsIndex | None = None,
) -> bool:
    """Optionally create a source Timeshift tag O snapshot before sync.

    This function only creates the source-side Timeshift snapshot. It never
    sends it directly and never turns it into a special targeted sync. After a
    real creation the caller must re-read ``timeshift --list``; the newly
    created snapshot is then handled by the normal oldest-to-newest sync loop,
    exactly like any other Timeshift snapshot.

    For safety, the source list is read before this function is called. If the
    destination already contains snapshots, the app walks state.json
    newest-to-oldest and requires a UUID-confirmed match between the configured
    source and an already received destination snapshot before it asks Timeshift
    to create a new snapshot. If the destination is empty, the first full seed
    is allowed.

    Returns True only when a real source snapshot was created and the caller
    should read `timeshift --list` again.
    """

    manual = config.manual_snapshot
    if not manual.enabled:
        return False
    if only_snapshot:
        print("Manual snapshot creation: skipped because --snapshot was specified.")
        _human_rule("----")
        return False

    pending_manual = _pending_app_manual_snapshots(config, state, source_by_name)
    if pending_manual:
        _human_blank()
        print("PENDING APP ON-DEMAND SNAPSHOT(S)")
        print(f"  existing pending: {', '.join(snapshot.name for snapshot in pending_manual)}")
        print("  recovery:         they remain in the normal oldest-to-newest sync order")
        print("  create policy:    still create a fresh on-demand snapshot for this run")
        print("  reason:           the previous app-created on-demand snapshot may be old after an interrupted run")
        _human_rule("----")

    _human_blank()
    confirm_source_identity_before_manual_snapshot(
        config,
        source,
        state,
        source_by_name,
        source_cache_index=source_cache_index,
        destination_index=destination_index,
    )
    _human_rule("----")

    _human_blank()
    print("MANUAL SNAPSHOT CREATE")
    print(f"  tag:     O (Timeshift default; --tags O is intentionally omitted)")
    print(f"  comment: {manual.comment}")

    if manual.marker and manual.marker.lower() not in manual.comment.lower():
        print()
        print(f"WARNING: manual_snapshot.comment does not contain marker {manual.marker!r};")
        print("         marker-based retention may not recognize this snapshot later.")

    if dry_run:
        print()
        print("Dry-run: would run source Timeshift --create --scripted --comments ...")
        _human_rule("----")
        return False

    timeshift.create_source_manual_snapshot(
        source,
        sudo=config.source.sudo,
        timeshift_command=config.source.timeshift_command,
        comment=manual.comment,
    )
    print()
    print("Requested source Timeshift on-demand snapshot. Reading source list after creation.")
    _human_rule("----")
    return True


def _snapshots_in_sync_order(snapshots) -> list[SnapshotMeta]:
    """Return source snapshots oldest-to-newest for Btrfs send."""

    return sorted(snapshots, key=lambda s: s.sort_key())


def _select_initial_sync_snapshots(config: AppConfig, source_by_name: dict[str, SnapshotMeta]) -> list[SnapshotMeta]:
    """Return retention-kept source snapshots for a fresh destination seed."""

    keep_names = initial_sync_keep_names(config, source_by_name.values())
    selected = [source_by_name[name] for name in sorted(keep_names) if name in source_by_name]
    skipped = len(source_by_name) - len(selected)
    _human_blank()
    print("FULL SYNC RETENTION SELECTION")
    print(f"  source snapshots:  {len(source_by_name)}")
    print(f"  selected to send:  {len(selected)}")
    print(f"  skipped by rules:  {skipped}")
    if selected:
        print(f"  first selected:    {selected[0].name}")
        print(f"  newest selected:   {selected[-1].name}")
    print("  sending order:     oldest selected to newest selected")
    print("  reason:            fresh destination only receives snapshots kept by retention")
    _human_rule("----")
    return selected

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


def _destination_has_existing_snapshots(config: AppConfig) -> bool:
    """Return True when the destination has real received snapshot content.

    Important bug fix: an earlier version created the empty destination snapshot
    directory before selecting a parent. That empty directory made the guard
    think the destination already contained backups and it refused the first full
    send. Here we only count folders that contain at least one configured
    subvolume name, for example @ or @home.
    """

    snapshots_root = config.destination.target_root / "snapshots"
    if not snapshots_root.exists():
        return False
    for child in snapshots_root.iterdir():
        if not child.is_dir():
            continue
        for subvol_name in config.source.subvolumes:
            if (child / subvol_name).exists():
                return True
    return False



def _snapshot_destination_paths_exist(config: AppConfig, snapshot_name: str, subvolume_names: list[str]) -> bool:
    """Return True only when every expected destination subvolume path exists."""

    return all(_dest_subvolume_path(config, snapshot_name, name).exists() for name in subvolume_names)

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


def _send_path_kind_text(config: AppConfig, send_path: str, original_path: str) -> str:
    """Return human text explaining who owns the selected send path."""

    if Path(send_path) == Path(original_path):
        return "Timeshift original read-only snapshot; protected from app prune"
    if btrfs.path_is_under_cache(send_path, config.source.cache_root):
        return "app-created source send-cache snapshot; prune may delete with destination retention"
    return "external read-only send path; protected from app prune"


def _ensure_source_send_path(
    config: AppConfig,
    source: SourceRunner,
    snapshot_name: str,
    subvolume: SubvolumeMeta,
    source_cache_index: remote_index.BtrfsIndex | None = None,
) -> str:
    """Return a real read-only source path, creating cache snapshots if needed.

    This calls only source-side `sudo btrfs ...` commands. It never uses source-side
    mkdir/cat/find/helper scripts.
    """

    return btrfs.source_ensure_readonly_send_path(
        source,
        sudo=config.source.sudo,
        btrfs_command=config.source.btrfs_command,
        original_path=subvolume.path,
        cache_root=config.source.cache_root,
        snapshot_name=snapshot_name,
        subvolume_name=subvolume.name,
        create_readonly_cache=config.source.create_readonly_cache,
        cache_index=source_cache_index,
    )


def _cleanup_incomplete_destination_receive(
    config: AppConfig,
    dest_path: Path,
    subvolume_name: str,
    destination_index: remote_index.BtrfsIndex | None = None,
) -> None:
    """Delete an incomplete destination receive before retrying.

    If the user presses Ctrl+C or SSH drops while `btrfs receive` is running,
    the destination can be left with a partially received subvolume. It is not in
    state.json, so it is unsafe to treat as completed. The safest automatic
    recovery is to delete that incomplete Btrfs subvolume and receive it again.

    Only Btrfs subvolumes are deleted automatically. If the path is a normal
    non-empty directory, the app refuses and asks for manual cleanup.
    """

    if not dest_path.exists():
        return
    if not config.destination.cleanup_incomplete_receive:
        raise SyncError(f"Destination path already exists but is not recorded as synced: {dest_path}")

    _human_blank()
    print(f"  {subvolume_name}: found incomplete destination receive not recorded in state.json")
    print("  retry policy: delete only this incomplete destination path now")
    print("  order policy: keep the existing snapshot queue order; resend when this snapshot/subvolume is reached")
    print()
    print(f"LOCAL INCOMPLETE DELETE: {dest_path}")
    print()

    try:
        # Confirm it is a Btrfs subvolume before deleting it. This avoids using
        # the backup tool as a dangerous rm -rf replacement.
        _local_meta(config, dest_path, subvolume_name)
        btrfs.delete_local_subvolume(dest_path, config.destination.sudo, config.destination.btrfs_command)
    except Exception as exc:
        # If it is just an empty ordinary directory, removing it is safe. If it
        # contains files, stop and let the user inspect it manually.
        try:
            if dest_path.is_dir() and not any(dest_path.iterdir()):
                dest_path.rmdir()
            else:
                raise
        except Exception:
            raise SyncError(
                "Destination path exists but is not a deletable Btrfs subvolume "
                "or empty directory. Clean it manually before retrying:\n"
                f"  {dest_path}\n"
                f"Original cleanup error: {exc}"
            ) from exc

    # Remove the now-empty snapshot folder if possible. It will be recreated just
    # before btrfs receive.
    try:
        parent = dest_path.parent
        if parent.exists() and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except Exception:
        pass

    if destination_index is not None:
        destination_index.remove_tree(dest_path)

    print("  incomplete destination receive removed")
    print("  retrying this snapshot/subvolume at its current oldest-to-newest queue position")
    _human_rule("---")


def _read_local_destination_parent_metadata(
    config: AppConfig,
    *,
    parent_name: str,
    subvolume_name: str,
    destination_index: remote_index.BtrfsIndex | None = None,
) -> SubvolumeMeta:
    """Read metadata for the destination snapshot that would be the receiver parent."""

    local_parent_path = _dest_subvolume_path(config, parent_name, subvolume_name)
    indexed_meta = destination_index.meta(local_parent_path) if destination_index is not None else None
    if indexed_meta:
        return indexed_meta
    if not local_parent_path.exists():
        raise SyncError(f"Incremental parent is recorded but missing on destination: {local_parent_path}")

    try:
        return _local_meta(config, local_parent_path, subvolume_name)
    except Exception as exc:
        raise SyncError(f"Cannot read destination parent metadata: {local_parent_path}: {exc}") from exc


def _match_source_path_to_destination_received_uuid(
    config: AppConfig,
    source: SourceRunner,
    *,
    source_path: str,
    subvolume_name: str,
    destination_meta: SubvolumeMeta | None = None,
    destination_path: Path | None = None,
    label: str = "source path",
    expected_uuids: set[str] | None = None,
    require_readonly: bool = False,
    source_cache_index: remote_index.BtrfsIndex | None = None,
    destination_index: remote_index.BtrfsIndex | None = None,
) -> tuple[bool, str]:
    """Check whether a source subvolume UUID matches the destination identity."""

    if destination_meta is None:
        if destination_path is None:
            raise ValueError("destination_meta or destination_path is required")
        destination_meta = destination_index.meta(destination_path) if destination_index is not None else None
        if destination_meta is None:
            try:
                destination_meta = _local_meta(config, destination_path, subvolume_name)
            except Exception as exc:
                return False, f"cannot read destination metadata for {destination_path}: {exc}"

    remote_meta = None
    if source_cache_index is not None and btrfs.path_is_under_cache(source_path, config.source.cache_root):
        remote_meta = source_cache_index.meta(source_path)
    if remote_meta is None:
        remote_meta = _source_meta(config, source, source_path, subvolume_name, required=False)
    if not remote_meta or not remote_meta.uuid:
        return False, f"{label} not found or has no UUID: {source_path}"

    allowed = set(expected_uuids or set())
    if destination_meta.received_uuid:
        allowed.add(destination_meta.received_uuid)
    if not allowed:
        return False, "destination parent has no received_uuid; cannot prove matching source parent"
    if remote_meta.uuid not in allowed:
        expected = ", ".join(sorted(allowed))
        return False, f"{label} UUID {remote_meta.uuid} does not match destination/state UUID(s) {expected}: {source_path}"
    if require_readonly and remote_meta.readonly is False:
        return False, f"{label} UUID matches, but it is not read-only: {source_path}"

    readonly_note = "read-only confirmed" if remote_meta.readonly is True else "read-only flag not reported"
    return True, f"destination received_uuid/state matches {label} UUID ({readonly_note})"


def _select_verified_parent_send_path(
    config: AppConfig,
    source: SourceRunner,
    *,
    parent_name: str,
    parent_subvol: SubvolumeMeta | None,
    subvolume_name: str,
    state_parent: dict | None,
    source_cache_index: remote_index.BtrfsIndex | None = None,
    destination_index: remote_index.BtrfsIndex | None = None,
) -> tuple[str | None, str]:
    """Select a safe source parent path for incremental send without recreating it."""

    local_parent = _read_local_destination_parent_metadata(
        config,
        parent_name=parent_name,
        subvolume_name=subvolume_name,
        destination_index=destination_index,
    )
    candidates: list[tuple[str, str]] = []
    saved_send_path = state_parent.get("send_path") if state_parent else None
    if isinstance(saved_send_path, str) and saved_send_path:
        candidates.append(("saved state send_path", saved_send_path))

    original_source_path = parent_subvol.path if parent_subvol else ""
    if original_source_path and all(path != original_source_path for _, path in candidates):
        candidates.append(("original Timeshift source path", original_source_path))

    failures: list[str] = []
    for label, path in candidates:
        ok, reason = _match_source_path_to_destination_received_uuid(
            config,
            source,
            source_path=path,
            subvolume_name=subvolume_name,
            destination_meta=local_parent,
            label=label,
            require_readonly=True,
            source_cache_index=source_cache_index,
            destination_index=destination_index,
        )
        if ok:
            return path, reason
        failures.append(reason)

    cache_hint = ""
    if isinstance(saved_send_path, str) and btrfs.path_is_under_cache(saved_send_path, config.source.cache_root):
        cache_hint = (
            "\n\nThe saved source parent was a read-only cache snapshot. If that cache "
            "snapshot was deleted, a recreated cache snapshot would get a new Btrfs "
            "UUID and cannot be used as the parent for this destination snapshot."
        )

    destination_path = _dest_subvolume_path(config, parent_name, subvolume_name)
    reason = "; ".join(failures) if failures else "no source parent candidates were available"
    return (
        None,
        f"destination parent {destination_path} has received_uuid={local_parent.received_uuid}; "
        f"no source parent path matched. {reason}{cache_hint}",
    )

def _state_uuid_values_for_path(state_subvol: dict, *, path: str, source_path: str) -> set[str]:
    """Return UUID values that may safely identify the source path.

    State from newer versions has both original_source_uuid and
    send_source_uuid. Older state may only have source_uuid and
    destination_received_uuid. For the exact send_path, destination_received_uuid
    is a strong identifier because Btrfs receive stores the UUID of the streamed
    source subvolume there. For the original Timeshift path, original_source_uuid
    is the strong identifier when available.
    """

    values: set[str] = set()
    send_path = state_subvol.get("send_path")

    def add_key(key: str) -> None:
        value = state_subvol.get(key)
        if isinstance(value, str) and value and value != "-":
            values.add(value)

    if path == send_path:
        add_key("send_source_uuid")
        add_key("source_uuid")
        add_key("destination_received_uuid")

    if path == source_path:
        add_key("original_source_uuid")
        # For direct sends, source_path and send_path are the same path. Older
        # states also used source_uuid for direct sends, so allow those values
        # only when the saved send path is missing or is the original path.
        if not send_path or send_path == source_path:
            add_key("send_source_uuid")
            add_key("source_uuid")
            add_key("destination_received_uuid")

    return values


def _find_confirmed_sync_floor(
    config: AppConfig,
    source: SourceRunner,
    state: dict,
    source_by_name: dict[str, SnapshotMeta],
    *,
    source_cache_index: remote_index.BtrfsIndex | None = None,
    destination_index: remote_index.BtrfsIndex | None = None,
) -> tuple[str | None, str]:
    """Return newest state snapshot that still exists on source and matches UUIDs.

    After destination pruning, old source snapshots may still exist on the source
    side. Without a floor, sync would see those pruned snapshots as missing and
    send them again. Instead of adding a long list of tombstones, we walk
    state.json newest-to-oldest and find the newest snapshot that:

    * is still listed by `timeshift --list` on the source,
    * is fully synced locally for the configured subvolumes,
    * has matching Btrfs UUID identity between source and destination.

    Source snapshots older than or equal to this confirmed floor are skipped by
    normal sync. If the original Timeshift snapshot no longer exists, the search
    can still confirm a floor through the saved app-created send_path in state.
    """

    state_snapshots = state.get("snapshots", {})
    if not state_snapshots:
        return None, "state is empty"

    source_names = source_by_name.keys()
    checked_missing = 0
    checked_mismatch: list[str] = []

    for name in sorted(state_snapshots.keys(), reverse=True):
        source_snapshot = source_by_name.get(name)
        if name not in source_names:
            checked_missing += 1
        if not snapshot_is_synced(state, name, config.source.subvolumes):
            continue

        state_snapshot = state_snapshots.get(name, {})
        state_subvolumes = state_snapshot.get("subvolumes", {})

        reasons: list[str] = []
        ok = True
        for subvolume_name in config.source.subvolumes:
            source_subvol = source_snapshot.subvolumes.get(subvolume_name) if source_snapshot else None
            state_subvol = state_subvolumes.get(subvolume_name)
            if not state_subvol:
                ok = False
                reasons.append(f"{subvolume_name}: missing state subvolume")
                break
            destination_path_text = state_subvol.get("destination_path")
            if not isinstance(destination_path_text, str) or not destination_path_text:
                ok = False
                reasons.append(f"{subvolume_name}: state has no destination_path")
                break
            try:
                destination_path = resolve_destination_path(config.destination.target_root, destination_path_text)
            except ValueError as exc:
                ok = False
                reasons.append(f"{subvolume_name}: invalid destination_path in state: {exc}")
                break

            sub_reasons: list[str] = []
            source_path = source_subvol.path if source_subvol else ""
            candidate_paths = [path for path in (state_subvol.get("send_path"), source_path) if isinstance(path, str) and path]
            if not candidate_paths:
                ok = False
                reasons.append(f"{subvolume_name}: no saved send_path and original source snapshot is not listed")
                break
            for path in dict.fromkeys(candidate_paths):
                sub_ok, reason = _match_source_path_to_destination_received_uuid(
                    config,
                    source,
                    source_path=path,
                    subvolume_name=subvolume_name,
                    destination_path=destination_path,
                    label=path,
                    expected_uuids=_state_uuid_values_for_path(state_subvol, path=path, source_path=source_path),
                    source_cache_index=source_cache_index,
                    destination_index=destination_index,
                )
                sub_reasons.append(reason)
                if sub_ok:
                    reasons.append(f"{subvolume_name}: {reason}")
                    break
            else:
                ok = False
                reasons.append(f"{subvolume_name}: {'; '.join(sub_reasons)}")
                break

        if ok:
            if name not in source_names:
                return name, "newest state snapshot is no longer in Timeshift, but saved source send-cache UUIDs are confirmed"
            if checked_missing:
                return name, f"newest state snapshot was not on source; walked back {checked_missing} entr{'y' if checked_missing == 1 else 'ies'} and confirmed UUIDs"
            return name, "newest state/source snapshot confirmed by UUIDs"

        checked_mismatch.append(f"{name}: {'; '.join(reasons)}")

    if checked_mismatch:
        return None, "no state/source snapshot passed UUID confirmation; latest mismatch: " + checked_mismatch[0]
    if checked_missing:
        return None, f"no state snapshot still exists on source; checked {checked_missing} missing entr{'y' if checked_missing == 1 else 'ies'}"
    return None, "no usable fully synced state snapshot found"

def _filesystem_parent_candidates(config: AppConfig, snapshot_name: str, subvolume_name: str, source_names: set[str]) -> list[str]:
    """Find local destination parent candidates by matching snapshot names.

    This lets the app recover/adopt a valid parent even if state.json is missing
    or incomplete, as long as local Btrfs `Received UUID` matches the source
    parent's UUID.
    """

    snapshots_root = config.destination.target_root / "snapshots"
    if not snapshots_root.exists():
        return []
    candidates: list[str] = []
    for child in snapshots_root.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if name >= snapshot_name or name not in source_names:
            continue
        if (child / subvolume_name).exists():
            candidates.append(name)
    candidates.sort(reverse=True)
    return candidates


def _select_parent(
    config: AppConfig,
    source: SourceRunner,
    state: dict,
    source_by_name: dict[str, SnapshotMeta],
    snapshot: SnapshotMeta,
    subvolume_name: str,
    *,
    dry_run: bool,
    trusted_parent_send_paths: set[str] | None = None,
    allow_full_seed: bool = False,
    source_cache_index: remote_index.BtrfsIndex | None = None,
    destination_index: remote_index.BtrfsIndex | None = None,
) -> tuple[str | None, str | None]:
    """Choose the newest valid incremental parent.

    The source parent path is chosen by UUID identity, not by recreating cache
    snapshots. For each candidate destination parent, the app tries the saved
    state send_path first, then the original Timeshift source path. One of those
    paths must currently have a UUID matching the destination parent's
    received_uuid. Otherwise the candidate is rejected and the next older parent
    candidate is tried.

    If the destination was empty when this sync run started, missing parents are
    allowed to fall back to full send. This is needed while seeding the first
    backup snapshot: after @ is received, the destination is no longer globally
    empty, but @home may still need its first full send.
    """

    source_names = source_by_name.keys()
    state_parent = latest_synced_before(state, snapshot.name, subvolume_name, source_names)

    candidate_names: list[str] = []
    state_parent_data: dict[str, dict] = {}
    if state_parent:
        parent_name, parent_state = state_parent
        candidate_names.append(parent_name)
        state_parent_data[parent_name] = parent_state

    # If the newest state parent no longer matches because its source cache was
    # deleted, an older state parent can still be a valid incremental parent. Add
    # every older synced state candidate, newest first. A candidate may be used
    # through its saved send_path even if Timeshift already pruned the original.
    for name in sorted(state.get("snapshots", {}).keys(), reverse=True):
        if name >= snapshot.name or name in candidate_names:
            continue
        item = state.get("snapshots", {}).get(name, {})
        sub = item.get("subvolumes", {}).get(subvolume_name) if isinstance(item, dict) else None
        if isinstance(sub, dict) and sub.get("status") == "ok" and (name in source_names or sub.get("send_path")):
            candidate_names.append(name)
            state_parent_data[name] = sub

    # Also look at the filesystem for matching date-named snapshots. This helps
    # if state.json is missing but destination snapshots are present.
    for name in _filesystem_parent_candidates(config, snapshot.name, subvolume_name, source_names):
        if name not in candidate_names:
            candidate_names.append(name)

    candidate_failures: list[str] = []
    for parent_name in candidate_names:
        parent_snapshot = source_by_name.get(parent_name)
        parent_subvol = parent_snapshot.subvolumes.get(subvolume_name) if parent_snapshot else None
        parent_state = state_parent_data.get(parent_name)
        saved_send_path = parent_state.get("send_path") if parent_state else None
        if not parent_subvol and not saved_send_path:
            continue

        if dry_run:
            # Dry-run remains fast. It explains that real mode will verify the
            # parent before using it.
            if isinstance(saved_send_path, str) and saved_send_path:
                return parent_name, saved_send_path
            return parent_name, _preview_send_path(config, parent_name, parent_subvol)

        if (
            isinstance(saved_send_path, str)
            and saved_send_path
            and trusted_parent_send_paths is not None
            and config.source.verify_incremental_parent_once_per_run
            and saved_send_path in trusted_parent_send_paths
        ):
            _human_blank()
            print(
                f"  {subvolume_name}: parent guard already proven in this run for {parent_name}; "
                "using the just-sent source parent path"
            )
            _human_blank()
            return parent_name, saved_send_path

        parent_send_path, reason = _select_verified_parent_send_path(
            config,
            source,
            parent_name=parent_name,
            parent_subvol=parent_subvol,
            subvolume_name=subvolume_name,
            state_parent=parent_state,
            source_cache_index=source_cache_index,
            destination_index=destination_index,
        )
        if parent_send_path:
            _human_blank()
            print(f"  {subvolume_name}: parent guard ok for {parent_name} ({reason})")
            _human_blank()
            return parent_name, parent_send_path

        candidate_failures.append(f"{parent_name}/{subvolume_name}: {reason}")

    # No usable parent was found. Full send is allowed when the destination was
    # empty at run start, because all snapshots/subvolumes created during that
    # run belong to this newly seeded chain. This fixes first-run multi-subvolume
    # seeding where @ makes the destination non-empty before @home is sent.
    if allow_full_seed:
        return None, None

    # Outside an empty-at-start seed run, full send is allowed only while the
    # destination has no snapshots at all. If snapshots already exist in the
    # backup target, refusing is safer than mixing a new full-send chain into the
    # wrong folder.
    if _destination_has_existing_snapshots(config):
        details = ""
        if candidate_failures:
            details = "\n\nChecked parent candidate(s):\n  " + "\n  ".join(candidate_failures)
        raise SyncError(
            "Destination already contains snapshots, but no usable matching incremental parent "
            "was found for the current source snapshot. Refusing to guess because this could "
            "mix snapshots from different OS installs or backup chains. Use an empty/separate "
            "target_root for a new full backup, or restore/repair state.json/source cache so a "
            "matching source/destination parent can be proven."
            + details
        )
    return None, None


def sync_once(config: AppConfig, state: dict, *, dry_run: bool, limit: int | None = None, only_snapshot: str | None = None, only_missing: bool = True) -> int:
    """Run one sync pass.

    A sync pass processes source snapshots oldest-to-newest. After every
    successful subvolume receive, state.json is updated immediately. That is how
    later snapshots in the same run can become incremental.
    """

    if dry_run:
        print("Strict dry-run: destination preparation is skipped; no target directories or internal metadata directories are created/changed.")
        _human_rule("----")

    # Create one source runner. In ssh mode it wraps SSH; in local mode it runs
    # the same source-side sudo+btrfs/timeshift commands locally and skips SSH.
    source = SourceRunner.from_config(config)
    if source.uses_ssh:
        source.test()
    else:
        print("Source mode: local; SSH setup/test skipped. Source commands run on this machine.")
        _human_rule("----")

    # Before Timeshift creates a fresh on-demand snapshot, before source cache
    # snapshots are created, and before any send/receive pipeline starts,
    # preflight verifies required roots. In real-run mode, missing configured
    # roots are created here first; if creation or Btrfs verification fails, the
    # exact configured path is reported as a hard error.
    preflight.check_required_sync_paths(config, source, dry_run=dry_run)

    if not dry_run:
        prepare_destination(config)

    destination_empty_at_start = not _destination_has_existing_snapshots(config)

    source_cache_index = (
        remote_index.build_source_btrfs_index(
            source,
            config.source.cache_root,
            sudo=config.source.sudo,
            btrfs_command=config.source.btrfs_command,
            include_root=True,
        )
        if config.source.cache_root
        else None
    )
    destination_index = remote_index.build_local_btrfs_index(
        config.destination.target_root,
        sudo=config.destination.sudo,
        btrfs_command=config.destination.btrfs_command,
        include_root=True,
    )
    _human_blank()
    print("SOURCE INDEX CACHE")
    if source_cache_index is None:
        print("  source cache: disabled; no source.cache_root configured")
    elif source_cache_index.root_missing:
        print(f"  source cache: missing or not listable; indexed 0 subvolumes below {source_cache_index.root}")
    else:
        print(f"  source cache: indexed {len(source_cache_index.by_path)} subvolume(s) below {source_cache_index.root}")
    if destination_index.root_missing:
        print(f"  destination:  missing or not listable; indexed 0 subvolumes below {destination_index.root}")
    else:
        print(f"  destination:  indexed {len(destination_index.by_path)} subvolume(s) below {destination_index.root}")
    print("  purpose:      reuse per-run path/UUID lookups instead of repeated source btrfs probes")
    _human_rule("----")

    def discover_source_index(reason: str) -> dict[str, SnapshotMeta]:
        print(f"Reading source Timeshift snapshots with sudo timeshift --list ({reason})...")
        _human_blank()
        print(
            "Discovery verification: enabled, checking every configured subvolume with btrfs."
            if config.source.verify_subvolumes_at_discovery
            else "Discovery verification: fast mode, delaying btrfs checks until send time."
        )
        _human_rule("----")
        return source_snapshot_index(list_source_snapshots(config, source, include_btrfs_info=config.source.verify_subvolumes_at_discovery))

    source_by_name = discover_source_index("before manual snapshot safety check")
    before_manual_snapshot_names = set(source_by_name)

    created_manual_snapshot = _maybe_create_manual_snapshot(
        config,
        source,
        state=state,
        source_by_name=source_by_name,
        dry_run=dry_run,
        only_snapshot=only_snapshot,
        source_cache_index=source_cache_index,
        destination_index=destination_index,
    )
    if created_manual_snapshot:
        source_by_name = discover_source_index("after manual snapshot creation")
        created_names = sorted(set(source_by_name) - before_manual_snapshot_names)
        _human_blank()
        print("MANUAL SNAPSHOT SYNC ORDER")
        if created_names:
            print(f"  detected new snapshot(s): {', '.join(created_names)}")
        else:
            print("  warning: no new snapshot name was detected after Timeshift create")
        print("  sending rule: no special early send; snapshots are processed in normal oldest-to-newest order")
        _human_rule("----")

    refreshed_metadata = refresh_state_metadata_and_report(state, source_by_name.values(), config.state_file, dry_run=dry_run)
    if refreshed_metadata:
        _human_rule("----")

    sync_floor_name: str | None = None
    if only_snapshot:
        snapshots_to_sync = [source_by_name[only_snapshot]] if only_snapshot in source_by_name else []
        if not snapshots_to_sync:
            raise SyncError(f"Source snapshot not found: {only_snapshot}")
    else:
        if destination_empty_at_start:
            snapshots_to_sync = _select_initial_sync_snapshots(config, source_by_name)
        else:
            snapshots_to_sync = source_by_name.values()
            sync_floor_name, sync_floor_reason = _find_confirmed_sync_floor(
                config,
                source,
                state,
                source_by_name,
                source_cache_index=source_cache_index,
                destination_index=destination_index,
            )
            if sync_floor_name:
                print(f"Sync floor: confirmed {sync_floor_name} ({sync_floor_reason})")
                print("Source snapshots older than or equal to this floor are skipped, so pruned destination snapshots are not re-sent.")
                _human_rule("----")
            elif state.get("snapshots"):
                print(f"Sync floor: none confirmed ({sync_floor_reason})")
                print("The app will not skip old source snapshots by high-watermark because UUID confirmation failed.")
                _human_rule("----")

    transferred = 0
    already_synced = 0
    sync_events: list[dict] = []

    # Tracks source parent paths that were successfully sent and received during
    # this run. When verify_incremental_parent_once_per_run is true, those freshly
    # created paths can be reused as the next parent without re-reading metadata.
    # Parent paths from previous runs are still validated against destination
    # received_uuid before use.
    trusted_parent_send_paths: set[str] = set()

    skipped_by_floor = 0
    for snapshot in _snapshots_in_sync_order(snapshots_to_sync):
        if sync_floor_name and snapshot.name <= sync_floor_name:
            skipped_by_floor += 1
            continue

        expected = [name for name in config.source.subvolumes if name in snapshot.subvolumes]
        if only_missing and snapshot_is_synced(state, snapshot.name, expected):
            if _snapshot_destination_paths_exist(config, snapshot.name, expected):
                already_synced += len(expected)
                continue
            print(f"Snapshot {snapshot.name}: state says synced, but at least one destination path is missing; retrying missing path(s).")
            _human_blank()
        if limit is not None and transferred >= limit:
            break

        target_dir = _target_snapshot_dir(config, snapshot.name)
        print(f"Snapshot {snapshot.name} tags={''.join(snapshot.tags) or '-'}")
        _human_blank()
        if dry_run:
            print(f"  would ensure local directory: {target_dir}")
            _human_blank()

        for subvol_name in config.source.subvolumes:
            # Incomplete destination cleanup is intentionally performed here,
            # inside the already sorted snapshot loop. This matters for failed
            # on-demand snapshots too: the app does not jump them ahead or
            # handle them specially. It deletes only the partial destination
            # path for the current snapshot/subvolume, then sends it when the
            # normal oldest-to-newest order reaches that exact item.
            subvolume = snapshot.subvolumes.get(subvol_name)
            if not subvolume:
                continue
            dest_path = _dest_subvolume_path(config, snapshot.name, subvol_name)
            already = state.get("snapshots", {}).get(snapshot.name, {}).get("subvolumes", {}).get(subvol_name)
            if already and already.get("status") == "ok" and dest_path.exists():
                already_synced += 1
                print(f"  {subvol_name}: already synced")
                _human_blank()
                continue
            if dest_path.exists() and not dry_run:
                _cleanup_incomplete_destination_receive(config, dest_path, subvol_name, destination_index)

            parent_name, parent_send_path = _select_parent(
                config,
                source,
                state,
                source_by_name,
                snapshot,
                subvol_name,
                dry_run=dry_run,
                trusted_parent_send_paths=trusted_parent_send_paths,
                allow_full_seed=destination_empty_at_start,
                source_cache_index=source_cache_index,
                destination_index=destination_index,
            )
            current_send_path = (
                _preview_send_path(config, snapshot.name, subvolume)
                if dry_run
                else _ensure_source_send_path(config, source, snapshot.name, subvolume, source_cache_index)
            )
            mode = "incremental" if parent_send_path else "full"

            if dry_run:
                _record_sync_event(
                    sync_events,
                    mode=mode,
                    snapshot=snapshot,
                    subvolume_name=subvol_name,
                    source_path=current_send_path,
                    destination_path=dest_path,
                    parent_name=parent_name,
                    parent_send_path=parent_send_path,
                    status="planned",
                )
                parent_text = f" parent={parent_name}" if parent_name else ""
                print(f"  {subvol_name}: would {mode} send{parent_text}")
                print()
                print(f"    source: {current_send_path}")
                print(f"    source-kind: {_send_path_kind_text(config, current_send_path, subvolume.path)}")
                print()
                print(f"    dest:   {dest_path}")
                if parent_name:
                    print()
                    print("    safety: real run verifies the selected parent send_path or original source UUID against destination received_uuid")
                if config.stream.use_mbuffer:
                    print()
                    print(f"    stream: would use {' '.join(config.stream.command() or [])}")
                if config.stream.btrfs_verbose:
                    print()
                    print("    btrfs: would add -v to send/receive and show operation output live")
                _human_rule("---")
                continue

            # Save the exact path that will be streamed. After receive, state is
            # updated with both original-source and send-path UUID metadata so a
            # later run can establish a prune-safe high-watermark without keeping
            # tombstones for every deleted destination snapshot.
            subvolume.send_path = current_send_path

            # Create the local receive directory only after parent selection.
            # This prevents an empty in-progress directory from being mistaken as
            # an existing backup by the safety guard.
            target_dir.mkdir(parents=True, exist_ok=True)
            _human_blank()
            print(f"  {subvol_name}: {mode} send/receive")
            print(f"    source-kind: {_send_path_kind_text(config, current_send_path, subvolume.path)}")
            # Build source send command. If parent_send_path is set, btrfs send
            # receives `-p <parent>` and sends an incremental stream.
            send_cmd = btrfs.source_send_cmd(
                source,
                sudo=config.source.sudo,
                btrfs_command=config.source.btrfs_command,
                current_path=current_send_path,
                parent_path=parent_send_path,
                compressed_data=config.source.send_compressed_data,
                proto=config.source.send_proto,
                verbose=config.stream.btrfs_verbose,
            )

            # Build local receive command. Destination compression is left to
            # the filesystem mount/property policy outside this app.
            receive_cmd = btrfs.local_receive_cmd(
                target_dir,
                config.destination.sudo,
                config.destination.btrfs_command,
                verbose=config.stream.btrfs_verbose,
            )

            # Optional mbuffer is inserted as the middle command. Password auth
            # environment is passed to the source side so streamed sends work
            # with sshpass in SSH mode. Local mode uses no extra environment.
            stream_pipeline(
                send_cmd,
                receive_cmd,
                middle_cmd=config.stream.command(),
                verbose=True,
                left_env=source.environment(),
                # If stream.btrfs_verbose is enabled, let Btrfs operation
                # output appear live in the terminal. mbuffer remains the real
                # byte/throughput progress display.
                passthrough_right_stdout=config.stream.btrfs_verbose,
            )
            _human_rule("---")

            received_meta = None
            original_meta = None
            send_meta = None
            if dest_path.exists():
                try:
                    received_meta = remote_index.refresh_local_path(
                        destination_index,
                        dest_path,
                        name=subvol_name,
                        sudo=config.destination.sudo,
                        btrfs_command=config.destination.btrfs_command,
                    )
                except Exception:
                    received_meta = None

            # Save both the original Timeshift source UUID and the exact send-path
            # UUID. When source cache is used, those are different subvolumes. This
            # metadata lets later runs establish a prune-safe high-watermark without
            # maintaining tombstones for every deleted destination snapshot.
            try:
                original_meta = _source_meta(config, source, subvolume.path, subvol_name, required=False)
            except Exception:
                original_meta = None
            try:
                if source_cache_index is not None and btrfs.path_is_under_cache(current_send_path, config.source.cache_root):
                    send_meta = source_cache_index.meta(current_send_path) or remote_index.refresh_source_path(
                        source_cache_index,
                        source,
                        current_send_path,
                        name=subvol_name,
                        sudo=config.source.sudo,
                        btrfs_command=config.source.btrfs_command,
                    )
                else:
                    send_meta = _source_meta(config, source, current_send_path, subvol_name, required=False)
            except Exception:
                send_meta = None

            mark_subvolume_synced(state, snapshot=snapshot, subvolume=subvolume, destination_path=dest_path, destination_root=config.destination.target_root, parent_snapshot=parent_name, parent_source_path=parent_send_path, send_path=current_send_path, received_meta=received_meta, original_meta=original_meta, send_meta=send_meta)
            save_state(config.state_file, state)
            trusted_parent_send_paths.add(current_send_path)
            _record_sync_event(
                sync_events,
                mode=mode,
                snapshot=snapshot,
                subvolume_name=subvol_name,
                source_path=current_send_path,
                destination_path=dest_path,
                parent_name=parent_name,
                parent_send_path=parent_send_path,
                status="synced",
            )

            # Keep every source-side read-only cache snapshot created by this
            # run. Cache snapshots are pruned only by the retention step, using
            # the same keep/delete decision as destination snapshots. This avoids
            # losing the newest common source/destination UUID merely because a
            # short-lived hourly parent was superseded during sync.

            transferred += 1

    _human_rule("----")
    if skipped_by_floor:
        print(f"Skipped {skipped_by_floor} source snapshot(s) at or below confirmed sync floor.")
    print("No missing subvolumes to sync." if transferred == 0 else f"Synced {transferred} subvolume(s).")
    print()
    _print_sync_summary(
        sync_events,
        dry_run=dry_run,
        skipped_by_floor=skipped_by_floor,
        already_synced=already_synced,
    )
    return transferred
