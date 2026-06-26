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
from .commands import stream_pipeline
from .config import AppConfig
from .models import SnapshotMeta, SubvolumeMeta
from .ssh import SSHRunner
from .log import emit_success_summary
from .state import latest_synced_before, mark_subvolume_synced, refresh_snapshot_metadata_from_source, resolve_destination_path, save_state, snapshot_is_synced


class SyncError(RuntimeError):
    """Raised for sync safety errors."""


def _local_meta(config: AppConfig, path: str | Path, name: str, required: bool = True) -> SubvolumeMeta | None:
    return btrfs.get_subvolume_meta("local", path, name, config.destination.sudo, config.destination.btrfs_command, required=required)


def _remote_meta(config: AppConfig, ssh: SSHRunner, path: str | Path, name: str, required: bool = True) -> SubvolumeMeta | None:
    return btrfs.get_subvolume_meta("remote", path, name, config.source.sudo, config.source.btrfs_command, ssh=ssh, required=required)


def _human_blank() -> None:
    """Print one blank line to separate human-readable status blocks."""

    print()


def _human_rule(text: str = "----") -> None:
    """Print a visual separator with blank lines around it."""

    print()
    print(text)
    print()



def _tags_text(tags: list[str] | tuple[str, ...] | None) -> str:
    """Return compact Timeshift tags for terminal summaries."""

    return "".join(tags or []) or "-"


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
            "tags": _tags_text(snapshot.tags),
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
    """Create/validate destination directories."""

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
    if config.log_dir is not None:
        config.log_dir.mkdir(parents=True, exist_ok=True)


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


def source_snapshot_index(snapshots) -> dict[str, SnapshotMeta]:
    return {snap.name: snap for snap in snapshots if snap.subvolumes}


def confirm_source_identity_before_manual_snapshot(
    config: AppConfig,
    ssh: SSHRunner,
    state: dict,
    source_by_name: dict[str, SnapshotMeta] | None = None,
    load_source_index=None,
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

    confirmed_name, reason = _find_confirmed_sync_floor(config, ssh, state, source_by_name)
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


def _maybe_create_manual_snapshot(
    config: AppConfig,
    ssh: SSHRunner,
    *,
    state: dict,
    source_by_name: dict[str, SnapshotMeta],
    dry_run: bool,
    only_snapshot: str | None,
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

    _human_blank()
    confirm_source_identity_before_manual_snapshot(config, ssh, state, source_by_name)
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

    timeshift.create_remote_manual_snapshot(
        ssh,
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


def _cleanup_superseded_source_cache(
    config: AppConfig,
    ssh: SSHRunner,
    *,
    parent_send_path: str | None,
    current_send_path: str,
    subvolume_name: str,
    parent_name: str | None,
) -> None:
    """Delete source cache snapshots that are no longer needed.

    Btrfs incremental send needs the previous source parent to exist while the
    next snapshot is being sent. Therefore the app must NOT delete the current
    newest cache snapshot immediately after sending it; that snapshot becomes
    the parent for the next incremental send, including the next run.

    What is safe to delete after a successful incremental send is the *old*
    parent cache snapshot. Once current_send_path has been received and saved to
    state.json, current_send_path is the new parent and parent_send_path is
    superseded.
    """

    if not config.source.cleanup_superseded_cache:
        return
    if not parent_send_path:
        # A full send has no old parent to clean up.
        return
    if parent_send_path == current_send_path:
        # Defensive guard; never delete the cache snapshot that was just sent.
        return
    if not btrfs.path_is_under_cache(parent_send_path, config.source.cache_root):
        # The parent was an original read-only Timeshift snapshot, not a cache.
        return

    _human_blank()
    print(f"  {subvolume_name}: cleaning superseded source cache parent from {parent_name}")
    print()
    print(f"SOURCE CACHE DELETE: {parent_send_path}")

    result = btrfs.remote_delete_subvolume(
        ssh,
        config.source.sudo,
        config.source.btrfs_command,
        parent_send_path,
        check=False,
    )
    if result.returncode != 0:
        print()
        print("WARNING: source cache subvolume cleanup failed; leaving it in place")
        if result.stderr.strip():
            print(result.stderr.strip())
        _human_blank()
        return

    parent_cache_dir = str(Path(parent_send_path).parent)
    if btrfs.path_is_under_cache(parent_cache_dir, config.source.cache_root):
        remaining = btrfs.remote_list_child_subvolumes(
            ssh,
            sudo=config.source.sudo,
            btrfs_command=config.source.btrfs_command,
            path=parent_cache_dir,
        )

        if remaining is None:
            print()
            print(f"SOURCE CACHE PARENT KEEP: could not verify that {parent_cache_dir} has no child subvolumes")
        elif remaining:
            children = [btrfs.cache_child_display_path(parent_cache_dir, child) for child in remaining]
            print()
            print(f"SOURCE CACHE PARENT KEEP: {parent_cache_dir} still contains cached {', '.join(children)}")
        else:
            parent_result = btrfs.remote_delete_subvolume(
                ssh, config.source.sudo, config.source.btrfs_command, parent_cache_dir, check=False
            )
            if parent_result.returncode == 0:
                print()
                print(f"SOURCE CACHE PARENT DELETE: {parent_cache_dir}")
            else:
                print()
                print("WARNING: source cache parent cleanup failed; leaving parent in place")
                if parent_result.stderr.strip():
                    print(parent_result.stderr.strip())
    _human_rule("---")


def _cleanup_incomplete_destination_receive(config: AppConfig, dest_path: Path, subvolume_name: str) -> None:
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

    print("  incomplete destination receive removed; retrying transfer")
    _human_rule("---")


def _read_local_destination_parent_metadata(
    config: AppConfig,
    *,
    parent_name: str,
    subvolume_name: str,
) -> SubvolumeMeta:
    """Read metadata for the destination snapshot that would be the receiver parent."""

    local_parent_path = _dest_subvolume_path(config, parent_name, subvolume_name)
    if not local_parent_path.exists():
        raise SyncError(f"Incremental parent is recorded but missing on destination: {local_parent_path}")

    try:
        return _local_meta(config, local_parent_path, subvolume_name)
    except Exception as exc:
        raise SyncError(f"Cannot read destination parent metadata: {local_parent_path}: {exc}") from exc


def _match_source_path_to_destination_received_uuid(
    config: AppConfig,
    ssh: SSHRunner,
    *,
    source_path: str,
    subvolume_name: str,
    destination_meta: SubvolumeMeta | None = None,
    destination_path: Path | None = None,
    label: str = "source path",
    expected_uuids: set[str] | None = None,
    require_readonly: bool = False,
) -> tuple[bool, str]:
    """Check whether a source subvolume UUID matches the destination identity."""

    if destination_meta is None:
        if destination_path is None:
            raise ValueError("destination_meta or destination_path is required")
        try:
            destination_meta = _local_meta(config, destination_path, subvolume_name)
        except Exception as exc:
            return False, f"cannot read destination metadata for {destination_path}: {exc}"

    remote_meta = _remote_meta(config, ssh, source_path, subvolume_name, required=False)
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
    ssh: SSHRunner,
    *,
    parent_name: str,
    parent_subvol: SubvolumeMeta,
    subvolume_name: str,
    state_parent: dict | None,
) -> tuple[str | None, str]:
    """Select a safe source parent path for incremental send without recreating it."""

    local_parent = _read_local_destination_parent_metadata(config, parent_name=parent_name, subvolume_name=subvolume_name)
    candidates: list[tuple[str, str]] = []
    saved_send_path = state_parent.get("send_path") if state_parent else None
    if isinstance(saved_send_path, str) and saved_send_path:
        candidates.append(("saved state send_path", saved_send_path))

    original_source_path = parent_subvol.path
    if original_source_path and all(path != original_source_path for _, path in candidates):
        candidates.append(("original Timeshift source path", original_source_path))

    failures: list[str] = []
    for label, path in candidates:
        ok, reason = _match_source_path_to_destination_received_uuid(
            config,
            ssh,
            source_path=path,
            subvolume_name=subvolume_name,
            destination_meta=local_parent,
            label=label,
            require_readonly=True,
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
    """Return UUID values that may safely identify the remote path.

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


def _find_confirmed_sync_floor(config: AppConfig, ssh: SSHRunner, state: dict, source_by_name: dict[str, SnapshotMeta]) -> tuple[str | None, str]:
    """Return newest state snapshot that still exists on source and matches UUIDs.

    After destination pruning, old source snapshots may still exist on the source
    side. Without a floor, sync would see those pruned snapshots as missing and
    send them again. Instead of adding a long list of tombstones, we walk
    state.json newest-to-oldest and find the newest snapshot that:

    * is still listed by `timeshift --list` on the source,
    * is fully synced locally for the configured subvolumes,
    * has matching Btrfs UUID identity between source and destination.

    Source snapshots older than or equal to this confirmed floor are skipped by
    normal sync. If the newest state entry no longer exists on the source, the
    search automatically walks backward until it finds a safe matching anchor.
    """

    state_snapshots = state.get("snapshots", {})
    if not state_snapshots:
        return None, "state is empty"

    source_names = source_by_name.keys()
    checked_missing = 0
    checked_mismatch: list[str] = []

    for name in sorted(state_snapshots.keys(), reverse=True):
        if name not in source_names:
            checked_missing += 1
            continue
        if not snapshot_is_synced(state, name, config.source.subvolumes):
            continue

        source_snapshot = source_by_name[name]
        state_snapshot = state_snapshots.get(name, {})
        state_subvolumes = state_snapshot.get("subvolumes", {})

        reasons: list[str] = []
        ok = True
        for subvolume_name in config.source.subvolumes:
            source_subvol = source_snapshot.subvolumes.get(subvolume_name)
            state_subvol = state_subvolumes.get(subvolume_name)
            if not source_subvol or not state_subvol:
                ok = False
                reasons.append(f"{subvolume_name}: missing source or state subvolume")
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
            source_path = source_subvol.path
            candidate_paths = [path for path in (state_subvol.get("send_path"), source_path) if isinstance(path, str) and path]
            for path in dict.fromkeys(candidate_paths):
                sub_ok, reason = _match_source_path_to_destination_received_uuid(
                    config,
                    ssh,
                    source_path=path,
                    subvolume_name=subvolume_name,
                    destination_path=destination_path,
                    label=path,
                    expected_uuids=_state_uuid_values_for_path(state_subvol, path=path, source_path=source_path),
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
    ssh: SSHRunner,
    state: dict,
    source_by_name: dict[str, SnapshotMeta],
    snapshot: SnapshotMeta,
    subvolume_name: str,
    *,
    dry_run: bool,
    trusted_parent_send_paths: set[str] | None = None,
) -> tuple[str | None, str | None]:
    """Choose the newest valid incremental parent.

    The source parent path is chosen by UUID identity, not by recreating cache
    snapshots. For each candidate destination parent, the app tries the saved
    state send_path first, then the original Timeshift source path. One of those
    paths must currently have a UUID matching the destination parent's
    received_uuid. Otherwise the candidate is rejected and the next older parent
    candidate is tried.
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
    # every older synced state candidate, newest first, so the validator can walk
    # back until it finds a source path whose UUID matches destination received_uuid.
    for name in sorted(state.get("snapshots", {}).keys(), reverse=True):
        if name >= snapshot.name or name not in source_names or name in candidate_names:
            continue
        item = state.get("snapshots", {}).get(name, {})
        sub = item.get("subvolumes", {}).get(subvolume_name) if isinstance(item, dict) else None
        if isinstance(sub, dict) and sub.get("status") == "ok":
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
        if not parent_snapshot:
            continue
        parent_subvol = parent_snapshot.subvolumes.get(subvolume_name)
        if not parent_subvol:
            continue

        if dry_run:
            # Dry-run remains fast. It explains that real mode will verify the
            # parent before using it.
            return parent_name, _preview_send_path(config, parent_name, parent_subvol)

        parent_state = state_parent_data.get(parent_name)
        saved_send_path = parent_state.get("send_path") if parent_state else None
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
            ssh,
            parent_name=parent_name,
            parent_subvol=parent_subvol,
            subvolume_name=subvolume_name,
            state_parent=parent_state,
        )
        if parent_send_path:
            _human_blank()
            print(f"  {subvolume_name}: parent guard ok for {parent_name} ({reason})")
            _human_blank()
            return parent_name, parent_send_path

        candidate_failures.append(f"{parent_name}/{subvolume_name}: {reason}")

    # No usable parent was found. Full send is allowed only when the destination
    # has no snapshots at all. If snapshots already exist in the backup target,
    # refusing is safer than mixing a new full-send chain into the wrong folder.
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
    else:
        prepare_destination(config)

    # Create one SSH runner. The same runner config supplies the SSH command and
    # optional SSHPASS environment for normal commands and streaming sends.
    ssh = SSHRunner(config.ssh)
    ssh.test()

    def discover_source_index(reason: str) -> dict[str, SnapshotMeta]:
        print(f"Reading source Timeshift snapshots with sudo timeshift --list ({reason})...")
        _human_blank()
        print(
            "Discovery verification: enabled, checking every configured subvolume with btrfs."
            if config.source.verify_subvolumes_at_discovery
            else "Discovery verification: fast mode, delaying btrfs checks until send time."
        )
        _human_rule("----")
        return source_snapshot_index(list_source_snapshots(config, ssh, include_btrfs_info=config.source.verify_subvolumes_at_discovery))

    source_by_name = discover_source_index("before manual snapshot safety check")
    before_manual_snapshot_names = set(source_by_name)

    created_manual_snapshot = _maybe_create_manual_snapshot(
        config,
        ssh,
        state=state,
        source_by_name=source_by_name,
        dry_run=dry_run,
        only_snapshot=only_snapshot,
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

    refreshed_metadata = refresh_snapshot_metadata_from_source(state, source_by_name.values())
    if refreshed_metadata:
        _human_blank()
        print("STATE METADATA REFRESH")
        print("  source: latest Timeshift --list metadata")
        print("  updated fields: tags, comment, created, path")
        print("  preserved fields: UUIDs, parent chain, send paths, destination paths, status")
        print(f"  snapshot(s): {', '.join(refreshed_metadata)}")
        if dry_run:
            print("  dry-run: state.json would be updated, but was not written")
        else:
            save_state(config.state_file, state)
            print("  state.json updated")
        _human_rule("----")

    sync_floor_name: str | None = None
    if only_snapshot:
        snapshots_to_sync = [source_by_name[only_snapshot]] if only_snapshot in source_by_name else []
        if not snapshots_to_sync:
            raise SyncError(f"Source snapshot not found: {only_snapshot}")
    else:
        snapshots_to_sync = source_by_name.values()
        sync_floor_name, sync_floor_reason = _find_confirmed_sync_floor(config, ssh, state, source_by_name)
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
            already_synced += len(expected)
            continue
        if limit is not None and transferred >= limit:
            break

        target_dir = _target_snapshot_dir(config, snapshot.name)
        print(f"Snapshot {snapshot.name} tags={''.join(snapshot.tags) or '-'}")
        _human_blank()
        if dry_run:
            print(f"  would ensure local directory: {target_dir}")
            _human_blank()

        for subvol_name in config.source.subvolumes:
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
                _cleanup_incomplete_destination_receive(config, dest_path, subvol_name)

            parent_name, parent_send_path = _select_parent(
                config,
                ssh,
                state,
                source_by_name,
                snapshot,
                subvol_name,
                dry_run=dry_run,
                trusted_parent_send_paths=trusted_parent_send_paths,
            )
            current_send_path = _preview_send_path(config, snapshot.name, subvolume) if dry_run else _ensure_source_send_path(config, ssh, snapshot.name, subvolume)
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
            # environment is passed to the SSH side so streamed sends work with
            # sshpass too.
            stream_pipeline(
                send_cmd,
                receive_cmd,
                middle_cmd=config.stream.command(),
                verbose=True,
                left_env=ssh.environment(),
                # If stream.btrfs_verbose is enabled, let Btrfs operation
                # output appear live in the terminal. mbuffer remains the real
                # byte/throughput progress display.
                passthrough_left_stderr=config.stream.btrfs_verbose,
                passthrough_right_stdout=config.stream.btrfs_verbose,
                passthrough_right_stderr=config.stream.btrfs_verbose,
            )
            _human_rule("---")

            received_meta = None
            original_meta = None
            send_meta = None
            if dest_path.exists():
                try:
                    received_meta = _local_meta(config, dest_path, subvol_name)
                except Exception:
                    received_meta = None

            # Save both the original Timeshift source UUID and the exact send-path
            # UUID. When source cache is used, those are different subvolumes. This
            # metadata lets later runs establish a prune-safe high-watermark without
            # maintaining tombstones for every deleted destination snapshot.
            try:
                original_meta = _remote_meta(config, ssh, subvolume.path, subvol_name, required=False)
            except Exception:
                original_meta = None
            try:
                send_meta = _remote_meta(config, ssh, current_send_path, subvol_name, required=False)
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

            # Source-side read-only cache snapshots are temporary. After a
            # successful incremental receive, the old parent cache is no longer
            # needed because the current send path becomes the new parent. The
            # current/latest cache is kept for future incremental sends.
            _cleanup_superseded_source_cache(
                config,
                ssh,
                parent_send_path=parent_send_path,
                current_send_path=current_send_path,
                subvolume_name=subvol_name,
                parent_name=parent_name,
            )

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
