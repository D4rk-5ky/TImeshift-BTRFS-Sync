"""Main destination-pull sync workflow.

The important performance/safety rule in this version is:

* Discovery is fast and only uses Timeshift names plus configured subvolume
  names. It does not run `btrfs subvolume show` and `btrfs property get` for
  every snapshot.
* Before the first real incremental send for each subvolume name in a run, the
  selected parent is verified with Btrfs metadata on both sides. Later
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
from .state import latest_synced_before, mark_subvolume_synced, save_state, snapshot_is_synced


class SyncError(RuntimeError):
    """Raised for sync safety errors."""


def _human_blank() -> None:
    """Print one blank line to separate human-readable status blocks."""

    print()


def _human_rule(text: str = "----") -> None:
    """Print a visual separator with blank lines around it."""

    print()
    print(text)
    print()

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
    if config.log_dir is not None:
        config.log_dir.mkdir(parents=True, exist_ok=True)

    # Best-effort compression property for future received writes.
    btrfs.set_local_compression(root, config.destination.sudo, config.destination.btrfs_command, config.destination.compression)
    btrfs.set_local_compression(snapshots_root, config.destination.sudo, config.destination.btrfs_command, config.destination.compression)


def list_source_snapshots(config: AppConfig, ssh: SSHRunner, *, include_btrfs_info: bool = True) -> list[SnapshotMeta]:
    """Discover source Timeshift snapshots.

    `include_btrfs_info=False` is the fast path: it parses Timeshift snapshot
    names/tags and constructs @/@home paths without running btrfs show/property
    for every snapshot. Btrfs checks are then delayed until an actual send or a
    selected incremental-parent verification.
    """

    return timeshift.list_remote_snapshots(
        ssh,
        snapshot_root=config.source.snapshot_root,
        subvolumes=config.source.subvolumes,
        sudo=config.source.sudo,
        timeshift_command=config.source.timeshift_command,
        btrfs_command=config.source.btrfs_command,
        include_btrfs_info=include_btrfs_info,
    )




def verify_source_identity_for_manual_snapshot(
    config: AppConfig,
    ssh: SSHRunner,
    state: dict,
    source_by_name: dict[str, SnapshotMeta],
) -> tuple[str, str]:
    """Require the configured source to match existing state before creating.

    This is used before the app asks source Timeshift to create a new manual
    snapshot. It prevents writing a new stale snapshot to the wrong mounted OS
    or wrong source host. The match is not name-only: it walks state.json
    newest-to-oldest, finds an entry that still exists in `timeshift --list`,
    and confirms Btrfs UUID / destination received_uuid identity.
    """

    confirmed_name, reason = _find_confirmed_sync_floor(config, ssh, state, source_by_name)
    if not confirmed_name:
        raise SyncError(
            "Refusing to create manual Timeshift snapshot.\n\n"
            "manual_snapshot.require_verified_source = true, but the configured source "
            "could not be matched to any already received snapshot in state.json.\n"
            "This may be the wrong mounted OS, wrong snapshot_root, wrong source host, "
            "or a first-ever sync with no trusted state yet.\n"
            f"Reason: {reason}\n\n"
            "Run a normal sync first with manual_snapshot.enabled = false, or set "
            "manual_snapshot.require_verified_source = false only if you intentionally "
            "want to allow first-run/unverified manual snapshot creation."
        )
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

    This is controlled by [manual_snapshot]. For safety, the source list is read
    before this function is called. When manual_snapshot.require_verified_source
    is true, the app walks state.json newest-to-oldest and requires a
    UUID-confirmed match between the configured source and an already received
    destination snapshot before it asks Timeshift to create a new snapshot.

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

    if manual.require_verified_source:
        _human_blank()
        print("MANUAL SNAPSHOT SOURCE IDENTITY CHECK")
        print("  require_verified_source: true")
        print("  checking existing source Timeshift list against state.json UUID history")

        confirmed_name, reason = verify_source_identity_for_manual_snapshot(config, ssh, state, source_by_name)

        print(f"  confirmed source anchor: {confirmed_name}")
        print(f"  reason: {reason}")
        _human_rule("----")
    else:
        _human_blank()
        print("MANUAL SNAPSHOT SOURCE IDENTITY CHECK")
        print("  require_verified_source: false")
        print("  WARNING: creating a manual Timeshift snapshot without UUID-confirming the source first")
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

    # The cache layout is <cache_root>/<snapshot-name>/<subvolume>. The
    # per-snapshot parent was created with `btrfs subvolume create`. After @ and
    # @home have both been deleted, deleting the parent succeeds. If another
    # cached subvolume still exists, Btrfs refuses the delete; that is expected
    # and safely ignored.
    parent_cache_dir = str(Path(parent_send_path).parent)
    if btrfs.path_is_under_cache(parent_cache_dir, config.source.cache_root):
        parent_result = btrfs.remote_delete_subvolume(
            ssh,
            config.source.sudo,
            config.source.btrfs_command,
            parent_cache_dir,
            check=False,
            log_stderr=False,
            mirror_stderr=False,
        )
        if parent_result.returncode == 0:
            print()
            print(f"SOURCE CACHE PARENT DELETE: {parent_cache_dir}")
        elif parent_result.stderr and "directory not empty" not in parent_result.stderr.lower():
            print()
            print("WARNING: source cache parent cleanup failed; leaving parent in place")
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
        btrfs.local_subvolume_show(dest_path, config.destination.sudo, subvolume_name, config.destination.btrfs_command)
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


def _read_remote_send_metadata(config: AppConfig, ssh: SSHRunner, path: str, subvolume_name: str) -> SubvolumeMeta:
    """Read Btrfs metadata for the exact source path that will be sent.

    This is intentionally called only for selected send paths, not for every
    snapshot during discovery.
    """

    meta = btrfs.remote_try_subvolume_show(ssh, config.source.sudo, config.source.btrfs_command, path, subvolume_name)
    if not meta:
        raise SyncError(f"Source send path is not a Btrfs subvolume or cannot be read: {path}")
    # Keep readonly=True if `subvolume show` already detected `Flags: readonly`.
    # Do not overwrite a known value with None from a failed/unsupported property read.
    prop_ro = btrfs.remote_readonly(ssh, config.source.sudo, config.source.btrfs_command, path)
    if prop_ro is not None:
        meta.readonly = prop_ro
    return meta


def _parent_metadata_matches(remote_parent: SubvolumeMeta, local_parent: SubvolumeMeta, state_parent: dict | None = None) -> tuple[bool, str]:
    """Compare source parent metadata with destination parent metadata.

    For a received Btrfs subvolume, `Received UUID` should match the UUID of the
    source snapshot that was sent. That is the strongest cheap check that the
    local destination parent actually belongs to the current source parent.
    """

    if remote_parent.uuid and local_parent.received_uuid and remote_parent.uuid == local_parent.received_uuid:
        return True, "destination received_uuid matches current source parent uuid"

    # Fallback for state created by previous versions. This is weaker than the
    # local received_uuid comparison, but it can still help explain/validate old
    # state if destination metadata is incomplete.
    if state_parent:
        old_source_uuid = state_parent.get("source_uuid")
        old_destination_uuid = state_parent.get("destination_uuid")
        if remote_parent.uuid and old_source_uuid and remote_parent.uuid == old_source_uuid:
            if not old_destination_uuid or old_destination_uuid == local_parent.uuid:
                return True, "state source_uuid matches current source parent uuid"

    details = (
        f"source uuid={remote_parent.uuid}, "
        f"destination uuid={local_parent.uuid}, "
        f"destination received_uuid={local_parent.received_uuid}"
    )
    return False, details


def _verify_incremental_parent(
    config: AppConfig,
    ssh: SSHRunner,
    *,
    parent_name: str,
    parent_subvol: SubvolumeMeta,
    parent_send_path: str,
    subvolume_name: str,
    state_parent: dict | None,
) -> None:
    """Verify that the selected incremental parent is safe to use.

    This prevents this dangerous situation:

      * destination already contains snapshots from OS-A,
      * config now points at OS-B,
      * snapshot names happen to overlap,
      * app tries to use OS-A destination snapshot as parent for OS-B source.

    The check costs only two Btrfs metadata reads for the selected parent:
      * remote/source `btrfs subvolume show <parent_send_path>`
      * local/destination `btrfs subvolume show <target_root>/snapshots/<parent>/@`
    """

    remote_parent = _read_remote_send_metadata(config, ssh, parent_send_path, subvolume_name)
    local_parent_path = _dest_subvolume_path(config, parent_name, subvolume_name)
    if not local_parent_path.exists():
        raise SyncError(f"Incremental parent is recorded but missing on destination: {local_parent_path}")

    try:
        local_parent = btrfs.local_subvolume_show(local_parent_path, config.destination.sudo, subvolume_name, config.destination.btrfs_command)
    except Exception as exc:
        raise SyncError(f"Cannot read destination parent metadata: {local_parent_path}: {exc}") from exc

    ok, reason = _parent_metadata_matches(remote_parent, local_parent, state_parent)
    if ok:
        _human_blank()
        print(f"  {subvolume_name}: parent guard ok for {parent_name} ({reason})")
        _human_blank()
        return

    message = (
        f"Refusing incremental send: parent metadata mismatch for {parent_name}/{subvolume_name}.\n"
        f"This can happen if the destination contains snapshots from another OS/source.\n"
        f"Details: {reason}\n"
        f"Use a separate destination target_root or an empty destination for this source."
    )
    if config.source.allow_incremental_without_parent_match:
        _human_blank()
        print("WARNING: " + message.replace("\n", " "))
        _human_blank()
        return
    raise SyncError(message)



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


def _remote_path_matches_state_uuid(
    config: AppConfig,
    ssh: SSHRunner,
    *,
    state_subvol: dict,
    source_subvol: SubvolumeMeta,
    subvolume_name: str,
) -> tuple[bool, str]:
    """Check whether a source path still matches a synced destination UUID.

    The high-watermark/prune skip must not be based only on names. It verifies
    the source candidate against the received destination identity. This keeps an
    old pruned snapshot from being re-sent while still protecting against using a
    similarly named snapshot from another OS/source.
    """

    destination_path_text = state_subvol.get("destination_path")
    if not isinstance(destination_path_text, str) or not destination_path_text:
        return False, "state has no destination_path"
    destination_path = Path(destination_path_text)
    local_received_uuid: str | None = None
    try:
        local_meta = btrfs.local_subvolume_show(
            destination_path,
            config.destination.sudo,
            subvolume_name,
            config.destination.btrfs_command,
        )
        local_received_uuid = local_meta.received_uuid
    except Exception as exc:
        return False, f"cannot read destination metadata for {destination_path}: {exc}"

    source_path = source_subvol.path
    candidate_paths: list[str] = []
    send_path = state_subvol.get("send_path")
    if isinstance(send_path, str) and send_path:
        candidate_paths.append(send_path)
    if source_path not in candidate_paths:
        candidate_paths.append(source_path)

    failures: list[str] = []
    for candidate_path in candidate_paths:
        remote_meta = btrfs.remote_try_subvolume_show(
            ssh,
            config.source.sudo,
            config.source.btrfs_command,
            candidate_path,
            subvolume_name,
        )
        if not remote_meta or not remote_meta.uuid:
            failures.append(f"{candidate_path}: not found or no UUID")
            continue

        expected = _state_uuid_values_for_path(state_subvol, path=candidate_path, source_path=source_path)
        if local_received_uuid:
            expected.add(local_received_uuid)

        if remote_meta.uuid in expected:
            return True, f"{candidate_path} UUID matches destination received_uuid/state"

        failures.append(f"{candidate_path}: UUID {remote_meta.uuid} did not match expected state/destination UUIDs")

    return False, "; ".join(failures)


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

    source_names = set(source_by_name.keys())
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
            sub_ok, reason = _remote_path_matches_state_uuid(
                config,
                ssh,
                state_subvol=state_subvol,
                source_subvol=source_subvol,
                subvolume_name=subvolume_name,
            )
            reasons.append(f"{subvolume_name}: {reason}")
            if not sub_ok:
                ok = False
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
    verified_parent_subvolumes: set[str] | None = None,
) -> tuple[str | None, str | None]:
    """Choose and optionally verify newest valid incremental parent.

    Fast discovery does not have metadata for every remote snapshot. This
    function keeps things fast by reading metadata only for the candidate parent
    that would actually be used for the first incremental send for a subvolume
    type during this run. After that, the chain is trusted for the same
    subvolume name because every new parent was just created by this process.
    """

    source_names = set(source_by_name.keys())
    state_parent = latest_synced_before(state, snapshot.name, subvolume_name, source_names)

    candidate_names: list[str] = []
    state_parent_data: dict[str, dict] = {}
    if state_parent:
        parent_name, parent_state = state_parent
        candidate_names.append(parent_name)
        state_parent_data[parent_name] = parent_state

    # Also look at the filesystem for matching date-named snapshots. This helps
    # if state.json is missing but destination snapshots are present.
    for name in _filesystem_parent_candidates(config, snapshot.name, subvolume_name, source_names):
        if name not in candidate_names:
            candidate_names.append(name)

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

        # Prefer the exact parent send_path already saved in state. This avoids
        # re-running source-side readonly/cache checks for a parent that was just
        # sent earlier in the same run. If state is missing/incomplete, fall back
        # to ensuring the source parent path exists.
        parent_state = state_parent_data.get(parent_name)
        parent_send_path = parent_state.get("send_path") if parent_state else None
        if not parent_send_path:
            parent_send_path = _ensure_source_send_path(config, ssh, parent_name, parent_subvol)

        if config.source.verify_incremental_parent:
            already_verified = bool(
                verified_parent_subvolumes is not None
                and config.source.verify_incremental_parent_once_per_run
                and subvolume_name in verified_parent_subvolumes
            )
            if already_verified:
                _human_blank()
                print(f"  {subvolume_name}: parent guard already verified earlier in this run; trusting incremental chain")
                _human_blank()
            else:
                _verify_incremental_parent(
                    config,
                    ssh,
                    parent_name=parent_name,
                    parent_subvol=parent_subvol,
                    parent_send_path=parent_send_path,
                    subvolume_name=subvolume_name,
                    state_parent=parent_state,
                )
                if verified_parent_subvolumes is not None:
                    verified_parent_subvolumes.add(subvolume_name)
        return parent_name, parent_send_path

    # No usable parent was found. If the destination is not empty, this may be a
    # user mistake such as pointing another OS at an existing backup location.
    if _destination_has_existing_snapshots(config) and not state.get("snapshots") and not config.source.allow_incremental_without_parent_match:
        raise SyncError(
            "Destination already contains snapshots, but state.json has no usable parent. "
            "Refusing to guess because this could mix snapshots from different OS installs. "
            "Use an empty/separate target_root or enable allow_incremental_without_parent_match only if you understand the risk."
        )
    return None, None


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

    def discover_source_snapshot_list(*, reason: str) -> tuple[list[SnapshotMeta], dict[str, SnapshotMeta]]:
        """Read source Timeshift snapshots and return both list and name lookup."""

        # Discovery uses Timeshift for names/tags. By default it avoids expensive
        # btrfs metadata probes for every snapshot/subvolume. This makes dry-run
        # and initial planning much faster on systems with many snapshots. If the
        # user wants the old verification behavior, they can set
        # source.verify_subvolumes_at_discovery = true.
        print(f"Reading source Timeshift snapshots with sudo timeshift --list ({reason})...")
        _human_blank()
        if config.source.verify_subvolumes_at_discovery:
            print("Discovery verification: enabled, checking every configured subvolume with btrfs.")
        else:
            print("Discovery verification: fast mode, delaying btrfs checks until send time.")
        _human_rule("----")
        discovered = [
            snap
            for snap in list_source_snapshots(
                config,
                ssh,
                include_btrfs_info=config.source.verify_subvolumes_at_discovery,
            )
            if snap.subvolumes
        ]
        return discovered, {snap.name: snap for snap in discovered}

    snapshots, source_by_name = discover_source_snapshot_list(reason="before manual snapshot safety check")

    created_manual_snapshot = _maybe_create_manual_snapshot(
        config,
        ssh,
        state=state,
        source_by_name=source_by_name,
        dry_run=dry_run,
        only_snapshot=only_snapshot,
    )
    if created_manual_snapshot:
        snapshots, source_by_name = discover_source_snapshot_list(reason="after manual snapshot creation")

    sync_floor_name: str | None = None
    if only_snapshot:
        snapshots = [snap for snap in snapshots if snap.name == only_snapshot]
        if not snapshots:
            raise SyncError(f"Source snapshot not found: {only_snapshot}")
    else:
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

    # Tracks which subvolume chains have already passed the expensive parent
    # guard during this run. Example: once @ has matched source-parent UUID to
    # destination received_uuid, later @ incrementals in the same run do not
    # repeat the same remote/local metadata comparison.
    verified_parent_subvolumes: set[str] = set()

    skipped_by_floor = 0
    for snapshot in sorted(snapshots, key=lambda s: s.sort_key()):
        if sync_floor_name and snapshot.name <= sync_floor_name:
            skipped_by_floor += 1
            continue

        expected = [name for name in config.source.subvolumes if name in snapshot.subvolumes]
        if only_missing and snapshot_is_synced(state, snapshot.name, expected):
            continue
        if limit is not None and transferred >= limit:
            break

        target_dir = _target_snapshot_dir(config, snapshot.name)
        print(f"Snapshot {snapshot.name} tags={''.join(snapshot.tags) or '-'}")
        _human_blank()
        if dry_run:
            print(f"  would ensure local directory: {target_dir}")
            if config.destination.compression:
                print(f"  would set destination compression property: {config.destination.compression}")
            _human_blank()

        for subvol_name in config.source.subvolumes:
            subvolume = snapshot.subvolumes.get(subvol_name)
            if not subvolume:
                continue
            dest_path = _dest_subvolume_path(config, snapshot.name, subvol_name)
            already = state.get("snapshots", {}).get(snapshot.name, {}).get("subvolumes", {}).get(subvol_name)
            if already and already.get("status") == "ok" and dest_path.exists():
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
                verified_parent_subvolumes=verified_parent_subvolumes,
            )
            current_send_path = _preview_send_path(config, snapshot.name, subvolume) if dry_run else _ensure_source_send_path(config, ssh, snapshot.name, subvolume)
            mode = "incremental" if parent_send_path else "full"

            if dry_run:
                parent_text = f" parent={parent_name}" if parent_name else ""
                print(f"  {subvol_name}: would {mode} send{parent_text}")
                print()
                print(f"    source: {current_send_path}")
                print()
                print(f"    dest:   {dest_path}")
                if parent_name and config.source.verify_incremental_parent:
                    print()
                    if config.source.verify_incremental_parent_once_per_run:
                        print("    safety: real run verifies the first incremental parent per subvolume, then trusts the chain")
                    else:
                        print("    safety: real run verifies every incremental parent source UUID against destination received_uuid")
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
            if config.destination.set_compression_before_receive:
                btrfs.set_local_compression(target_dir, config.destination.sudo, config.destination.btrfs_command, config.destination.compression)

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

            # Build local receive command. Compression properties were set on
            # the target directory before receive if configured.
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
                    received_meta = btrfs.local_subvolume_show(dest_path, config.destination.sudo, subvol_name, config.destination.btrfs_command)
                    received_meta.readonly = btrfs.local_readonly(dest_path, config.destination.sudo, config.destination.btrfs_command)
                except Exception:
                    received_meta = None

                # A received Timeshift/Btrfs snapshot is normally read-only.
                # Setting the compression property on a read-only subvolume fails,
                # so the safe/default behavior is to set compression on the receive
                # parent before receive and skip after-receive property changes when
                # the received subvolume is read-only.
                if config.destination.set_compression_after_receive:
                    if received_meta and received_meta.readonly is True:
                        print(f"  {subvol_name}: destination is read-only; skipping after-receive compression property")
                    else:
                        btrfs.set_local_compression(dest_path, config.destination.sudo, config.destination.btrfs_command, config.destination.compression)

            # Save both the original Timeshift source UUID and the exact send-path
            # UUID. When source cache is used, those are different subvolumes. This
            # metadata lets later runs establish a prune-safe high-watermark without
            # maintaining tombstones for every deleted destination snapshot.
            try:
                original_meta = btrfs.remote_try_subvolume_show(ssh, config.source.sudo, config.source.btrfs_command, subvolume.path, subvol_name)
            except Exception:
                original_meta = None
            try:
                send_meta = btrfs.remote_try_subvolume_show(ssh, config.source.sudo, config.source.btrfs_command, current_send_path, subvol_name)
            except Exception:
                send_meta = None

            mark_subvolume_synced(state, snapshot=snapshot, subvolume=subvolume, destination_path=dest_path, parent_snapshot=parent_name, parent_source_path=parent_send_path, send_path=current_send_path, received_meta=received_meta, original_meta=original_meta, send_meta=send_meta)
            save_state(config.state_file, state)

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
    return transferred
