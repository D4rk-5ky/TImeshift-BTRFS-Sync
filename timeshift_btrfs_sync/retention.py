"""Destination retention/pruning logic."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from . import btrfs
from . import remote_index
from .config import AppConfig
from .models import SnapshotMeta, tags_text
from .state import (
    remove_snapshot_from_state,
    resolve_destination_path,
    save_state,
    state_send_path_is_app_cache,
    state_send_path_is_protected_timeshift_original,
)
from .log import emit_success_summary, get_logger
from .ssh import SSHRunner


@dataclass(slots=True)
class PrunePlan:
    """Dry-run friendly prune plan."""

    keep: set[str] = field(default_factory=set)
    delete: set[str] = field(default_factory=set)
    reasons: dict[str, list[str]] = field(default_factory=dict)

    def add_keep(self, snapshot: str, reason: str) -> None:
        """Mark a snapshot as kept and remember the human reason."""

        self.keep.add(snapshot)
        self.delete.discard(snapshot)
        self.reasons.setdefault(snapshot, []).append(f"keep: {reason}")

    def add_delete(self, snapshot: str, reason: str) -> None:
        """Mark a snapshot as deletable only when it is not already protected."""

        if snapshot not in self.keep:
            self.delete.add(snapshot)
        self.reasons.setdefault(snapshot, []).append(f"delete: {reason}")


def _is_app_created_ondemand(snapshot_state: dict, marker: str) -> bool:
    """Return true when a state entry is a tag O snapshot with the app marker."""

    if "O" not in snapshot_state.get("tags", []):
        return False
    marker = marker.lower().strip()
    if not marker:
        return False
    return marker in str(snapshot_state.get("comment") or "").lower()



def _delete_reason_for_snapshot(
    config: AppConfig,
    snapshots: dict,
    name: str,
    *,
    app_created_ondemand: set[str],
    normal_ondemand: set[str],
) -> str:
    """Explain why a snapshot is outside the active retention rules."""

    snapshot_state = snapshots.get(name, {})
    tags = snapshot_state.get("tags", []) or []
    tag_text = tags_text(snapshot_state.get('tags', []))

    if name in app_created_ondemand:
        return (
            "app-created on-demand snapshot outside "
            f"manual_snapshot.retention_count={config.manual_snapshot.retention_count}; tags={tag_text}"
        )

    if name in normal_ondemand:
        return f"normal on-demand snapshot outside retention.ondemand={config.retention.ondemand}; tags={tag_text}"

    matched_rules: list[str] = []
    for tag, count in config.retention.counts_by_tag().items():
        if tag == "O" or count <= 0:
            continue
        if tag in tags:
            matched_rules.append(f"tag {tag} keeps newest {count}")

    if matched_rules:
        return f"outside active Timeshift tag retention ({'; '.join(matched_rules)}); tags={tag_text}"

    return f"not protected by any active retention rule; tags={tag_text}"


def _delete_reasons(plan: PrunePlan, name: str) -> list[str]:
    """Return delete reasons without the internal prefix."""

    reasons: list[str] = []
    for reason in plan.reasons.get(name, []):
        if reason.startswith("delete: "):
            reasons.append(reason.removeprefix("delete: "))
    return reasons or ["outside retention"]

def _source_cache_delete_paths(config: AppConfig, snapshot_state: dict) -> list[tuple[str, str]]:
    """Return app-owned source send-cache paths for a prune decision.

    Original Timeshift snapshot paths are deliberately excluded even when they
    were used as direct read-only send sources. Timeshift owns
    source.snapshot_root; this app only prunes app-created source-cache paths.
    Older state did not store send_path_kind, so the compatibility fallback
    still treats only paths below source.cache_root as app-owned.
    """

    if not config.source.cleanup_superseded_cache or not config.source.cache_root:
        return []
    paths: dict[str, str] = {}
    for subvol_name, subvol in snapshot_state.get("subvolumes", {}).items():
        if not isinstance(subvol, dict):
            continue
        send_path = subvol.get("send_path")
        if (
            isinstance(send_path, str)
            and state_send_path_is_app_cache(subvol, cache_root=config.source.cache_root)
            and btrfs.path_is_under_cache(send_path, config.source.cache_root)
        ):
            paths[subvol_name] = send_path
    return sorted(paths.items())


def _protected_timeshift_send_paths(config: AppConfig, snapshot_state: dict) -> list[tuple[str, str]]:
    """Return direct Timeshift send paths that prune must never delete."""

    paths: dict[str, str] = {}
    for subvol_name, subvol in snapshot_state.get("subvolumes", {}).items():
        if not isinstance(subvol, dict):
            continue
        send_path = subvol.get("send_path")
        if isinstance(send_path, str) and state_send_path_is_protected_timeshift_original(subvol, cache_root=config.source.cache_root):
            paths[subvol_name] = send_path
    return sorted(paths.items())


def _destination_delete_paths(config: AppConfig, snapshot_state: dict) -> list[tuple[str, Path]]:
    """Return tracked destination subvolume paths for a prune decision."""

    paths: dict[str, Path] = {}
    for subvol_name, subvol in snapshot_state.get("subvolumes", {}).items():
        destination_path = subvol.get("destination_path")
        if destination_path:
            paths[subvol_name] = resolve_destination_path(config.destination.target_root, destination_path)
    return sorted(paths.items())


def source_snapshot_state(snapshots: Iterable[SnapshotMeta]) -> dict:
    """Return temporary state-like data from source Timeshift snapshots.

    Initial/full sync uses this to apply the same retention rules before
    transferring anything. Only snapshot-level Timeshift metadata is needed for
    that decision; transfer identity fields are intentionally absent because no
    destination state exists yet.
    """

    return {
        "version": 1,
        "snapshots": {
            snap.name: {
                "name": snap.name,
                "tags": list(snap.tags),
                "comment": snap.comment,
                "created": snap.created,
                "path": (Path("snapshots") / snap.name).as_posix(),
                "subvolumes": {},
            }
            for snap in snapshots
        },
    }


def initial_sync_keep_names(config: AppConfig, snapshots: Iterable[SnapshotMeta]) -> set[str]:
    """Return source snapshot names that a fresh destination should seed.

    This uses the same retention planner as prune so a new/full sync does not
    waste time sending snapshots that the post-sync retention step would delete
    immediately.
    """

    return build_prune_plan(config, source_snapshot_state(snapshots)).keep


def _cleanup_source_cache_for_pruned_snapshot(
    config: AppConfig,
    ssh: SSHRunner,
    snapshot_name: str,
    snapshot_state: dict,
    source_cache_index: remote_index.BtrfsIndex | None = None,
) -> bool:
    """Return True when source send-cache for one pruned snapshot is gone or absent."""

    cache_paths = _source_cache_delete_paths(config, snapshot_state)
    protected_paths = _protected_timeshift_send_paths(config, snapshot_state)
    if protected_paths:
        print("  source Timeshift originals: protected; not deleted by this app")
        for subvol_name, send_path in protected_paths:
            print(f"  protected {subvol_name}: {send_path}")
    if not cache_paths:
        print("  source send-cache: no tracked app-created cache paths; confirmed gone")
        return True

    print("  source send-cache: deleting/checking app-created cache paths only")
    if source_cache_index is not None and source_cache_index.root_missing:
        print("  warning: source send-cache root could not be indexed; keeping state entry for retry")
        return False

    ok = True
    by_parent: dict[str, list[tuple[str, str]]] = {}
    for subvol_name, send_path in cache_paths:
        by_parent.setdefault(str(Path(send_path).parent), []).append((subvol_name, send_path))

    for parent_dir, paths in sorted(by_parent.items()):
        if source_cache_index is not None:
            if not source_cache_index.contains(parent_dir):
                print(f"  cache parent: already gone on source, confirmed {parent_dir}")
                continue
            existing_children = {send_path for _, send_path in paths if source_cache_index.contains(send_path)}
        else:
            parent_exists = btrfs.remote_cache_existing_paths(
                ssh,
                sudo=config.source.sudo,
                btrfs_command=config.source.btrfs_command,
                cache_root=config.source.cache_root,
                paths=[parent_dir],
            )
            if parent_exists is None:
                print("  warning: could not list source send-cache root; keeping state entry for retry")
                return False
            if parent_dir not in parent_exists:
                print(f"  cache parent: already gone on source, confirmed {parent_dir}")
                continue

            existing_children = btrfs.remote_cache_existing_child_paths(
                ssh,
                sudo=config.source.sudo,
                btrfs_command=config.source.btrfs_command,
                cache_root=config.source.cache_root,
                parent_path=parent_dir,
                paths=[send_path for _, send_path in paths],
            )
            if existing_children is None:
                print(f"  warning: could not list source send-cache children; keeping state entry for retry: {parent_dir}")
                return False

        for subvol_name, send_path in sorted(paths):
            if send_path not in existing_children:
                print(f"  cache {subvol_name}: already gone on source, confirmed {send_path}")
                continue
            print(f"  cache {subvol_name}: deleting {send_path}")
            result = btrfs.remote_delete_subvolume(
                ssh,
                config.source.sudo,
                config.source.btrfs_command,
                send_path,
                check=False,
            )
            if result.returncode == 0:
                if source_cache_index is not None:
                    source_cache_index.discard(send_path)
            else:
                ok = False
                print("  warning: source send-cache subvolume cleanup failed; keeping state entry for retry")

        empty = (
            source_cache_index.is_empty(parent_dir)
            if source_cache_index is not None
            else btrfs.remote_cache_is_empty(
                ssh,
                sudo=config.source.sudo,
                btrfs_command=config.source.btrfs_command,
                cache_root=config.source.cache_root,
                path=parent_dir,
            )
        )
        if empty is True:
            result = btrfs.remote_delete_subvolume(
                ssh,
                config.source.sudo,
                config.source.btrfs_command,
                parent_dir,
                check=False,
            )
            if result.returncode == 0:
                if source_cache_index is not None:
                    source_cache_index.discard(parent_dir)
                print(f"  cache parent: deleted {parent_dir}")
            else:
                ok = False
                print("  warning: source send-cache parent cleanup failed; keeping state entry for retry")
        elif empty is None:
            ok = False
            print(f"  cache parent: could not verify empty, keeping state entry for retry: {parent_dir}")
        else:
            ok = False
            print(f"  cache parent: still has cached subvolumes, keeping state entry for retry: {parent_dir}")
    return ok


def build_prune_plan(config: AppConfig, state: dict) -> PrunePlan:
    """Build retention plan from state without deleting anything.

    On-demand cleanup is intentionally split into two independent decisions:

    * manual_snapshot.cleanup_enabled controls app-created tag O snapshots whose
      saved Timeshift comment contains manual_snapshot.marker.
    * retention.cleanup_ondemand controls normal/user-created tag O snapshots.

    This prevents normal manual Timeshift snapshots from being deleted merely
    because the app also has its own on-demand snapshot retention rule.
    """

    snapshots = state.get("snapshots", {})
    names = sorted(snapshots.keys())
    plan = PrunePlan()
    if not names:
        return plan

    marker = config.manual_snapshot.marker.lower().strip()
    app_created_ondemand = {
        name for name in names if _is_app_created_ondemand(snapshots[name], marker)
    }
    normal_ondemand = {
        name
        for name in names
        if "O" in snapshots[name].get("tags", []) and name not in app_created_ondemand
    }

    for name in config.retention.protected_snapshots:
        if name in snapshots:
            plan.add_keep(name, "protected")
    if config.retention.keep_latest:
        plan.add_keep(names[-1], "newest synced snapshot")

    # App-created on-demand retention. This only affects snapshots with the
    # configured marker in the saved Timeshift comment.
    if config.manual_snapshot.cleanup_enabled:
        manual_count = config.manual_snapshot.retention_count
        selected = sorted(app_created_ondemand, reverse=True)
        for name in selected[:manual_count]:
            plan.add_keep(name, f"app-created on-demand retention count {manual_count}")
    else:
        for name in sorted(app_created_ondemand):
            plan.add_keep(name, "app-created on-demand cleanup disabled")

    # Normal/user-created on-demand retention. This is independent from the
    # app-created rule above and is disabled by default for safety.
    if config.retention.cleanup_ondemand:
        selected = sorted(normal_ondemand, reverse=True)
        for name in selected[: config.retention.ondemand]:
            plan.add_keep(name, f"normal on-demand retention count {config.retention.ondemand}")
    else:
        for name in sorted(normal_ondemand):
            plan.add_keep(name, "normal on-demand cleanup disabled")

    # Non-O Timeshift tag retention. Tag O is handled separately above so the
    # two on-demand cleanup switches stay independent.
    for tag, count in config.retention.counts_by_tag().items():
        if tag == "O" or count <= 0:
            continue
        tagged = [name for name in names if tag in snapshots[name].get("tags", [])]
        tagged.sort(reverse=True)
        for name in tagged[:count]:
            plan.add_keep(name, f"tag {tag} retention count {count}")

    if config.retention.keep_latest_common_parent:
        plan.add_keep(names[-1], "latest common parent safety")

    for name in names:
        if name not in plan.keep:
            plan.add_delete(
                name,
                _delete_reason_for_snapshot(
                    config,
                    snapshots,
                    name,
                    app_created_ondemand=app_created_ondemand,
                    normal_ondemand=normal_ondemand,
                ),
            )
    plan.delete -= plan.keep
    return plan


def _delete_destination_snapshot_for_prune(config: AppConfig, state: dict, snapshot_name: str) -> bool:
    """Return True when destination subvolumes for one pruned snapshot are gone."""

    if not config.destination.target_root.exists():
        print(f"  destination: target root unavailable, keeping state entry for retry: {config.destination.target_root}")
        return False

    item = state.get("snapshots", {}).get(snapshot_name, {})
    snap_path = config.destination.target_root / "snapshots" / snapshot_name
    destination_paths = _destination_delete_paths(config, item)
    subvol_paths = [path for _, path in destination_paths]
    if not subvol_paths:
        print("  destination: no tracked destination subvolume paths; checking snapshot parent only")
    ok = True
    for subvol_name, subvol_path in sorted(destination_paths, key=lambda item: len(item[1].parts), reverse=True):
        if not subvol_path.exists():
            print(f"  destination {subvol_name}: already gone, confirmed {subvol_path}")
            continue
        print(f"  destination {subvol_name}: deleting {subvol_path}")
        try:
            btrfs.delete_local_subvolume(subvol_path, config.destination.sudo, config.destination.btrfs_command)
        except Exception as exc:
            ok = False
            print(f"  warning: destination subvolume cleanup failed; keeping state entry for retry: {exc}")
    if snap_path.exists():
        try:
            snap_path.rmdir()
        except OSError:
            pass
    destination_gone = not any(path.exists() for path in subvol_paths) and not snap_path.exists()
    if destination_gone:
        print("  destination: confirmed gone")
    elif ok:
        print(f"  destination: still present, keeping state entry for retry: {snap_path}")
    return ok and destination_gone


def _delete_prune_item(
    config: AppConfig,
    state: dict,
    plan: PrunePlan,
    source_cache_ssh: SSHRunner | None,
    name: str,
    source_cache_index: remote_index.BtrfsIndex | None = None,
) -> bool:
    """Delete both sides for one prune item, then remove state only after confirmation."""

    snapshot_state = state.get("snapshots", {}).get(name, {})
    print()
    print("RETENTION DELETE")
    print(f"  snapshot: {name}")
    print(f"  tags:     {tags_text(snapshot_state.get('tags', []))}")
    for reason in _delete_reasons(plan, name):
        print(f"  why:      {reason}")
    print("  action:   deleting destination subvolumes and source send-cache; state is removed after both sides are confirmed gone")

    print()
    print("Retention Delete Destination")
    destination_gone = _delete_destination_snapshot_for_prune(config, state, name)

    print()
    print("Retention Delete Source send-cache")
    source_cache_gone = True
    if source_cache_ssh:
        source_cache_gone = _cleanup_source_cache_for_pruned_snapshot(
            config,
            source_cache_ssh,
            name,
            snapshot_state,
            source_cache_index=source_cache_index,
        )
    else:
        print("  source send-cache: not checked this run")

    print()
    print("State")
    if destination_gone and source_cache_gone:
        remove_snapshot_from_state(state, name)
        print("  removed; destination and source send-cache are confirmed gone")
        return True

    print("  kept; cleanup can be retried safely on the next prune")
    return False


def print_prune_plan(config: AppConfig, plan: PrunePlan, state: dict, *, dry_run: bool) -> None:
    """Write an easy-to-read retention summary to terminal and .succes."""

    snapshots = state.get("snapshots", {})
    mode_text = "dry-run plan" if dry_run else "real deletion plan"
    lines = [
        "",
        "RETENTION SUMMARY",
        "=================",
        f"  mode:              {mode_text}",
        f"  snapshots in state:{len(snapshots):>5}",
        f"  kept by rules:     {len(plan.keep):>5}",
        f"  delete candidates: {len(plan.delete):>5}",
    ]

    if not plan.delete:
        lines += ["  deletion:          none", ""]
        emit_success_summary("\n".join(lines))
        return

    lines += ["", "RETENTION DELETE PLAN", "---------------------"]
    for name in sorted(plan.delete):
        snapshot_state = snapshots.get(name, {})
        action = "WOULD DELETE" if dry_run else "DELETE"
        lines.append(f"  [{action}] {name}  tags={tags_text(snapshot_state.get('tags', []))}")
        destination_paths = _destination_delete_paths(config, snapshot_state)
        if destination_paths:
            lines.append("      destination subvolumes:")
            for subvol_name, destination_path in destination_paths:
                lines.append(f"        {subvol_name}: {destination_path}")
        cache_paths = _source_cache_delete_paths(config, snapshot_state)
        if cache_paths:
            lines.append("      app-owned source send-cache subvolumes:")
            for subvol_name, send_path in cache_paths:
                lines.append(f"        {subvol_name}: {send_path}")
        protected_paths = _protected_timeshift_send_paths(config, snapshot_state)
        if protected_paths:
            lines.append("      protected Timeshift original send paths, not deleted by prune:")
            for subvol_name, send_path in protected_paths:
                lines.append(f"        {subvol_name}: {send_path}")
        for reason in _delete_reasons(plan, name):
            lines.append(f"      why: {reason}")
    lines.append("")
    emit_success_summary("\n".join(lines))


def prune(config: AppConfig, state: dict, *, dry_run: bool, yes_delete: bool) -> PrunePlan:
    """Apply destination retention rules."""

    plan = build_prune_plan(config, state)
    print_prune_plan(config, plan, state, dry_run=dry_run)
    if dry_run:
        print("Dry-run: no retention deletes were performed.")
        return plan
    if plan.delete and not yes_delete:
        raise RuntimeError("Refusing to delete without --yes-delete")
    deleted = 0
    source_cache_ssh = (
        SSHRunner(config.ssh)
        if plan.delete and config.source.cleanup_superseded_cache and config.source.cache_root
        else None
    )
    source_cache_index = (
        remote_index.build_remote_btrfs_index(
            source_cache_ssh,
            config.source.cache_root,
            sudo=config.source.sudo,
            btrfs_command=config.source.btrfs_command,
            include_root=True,
        )
        if source_cache_ssh and config.source.cache_root
        else None
    )
    if source_cache_index is not None:
        print()
        print(f"Source send-cache index: {len(source_cache_index.by_path)} indexed subvolume(s) below {source_cache_index.root}")
    for name in sorted(plan.delete):
        if _delete_prune_item(config, state, plan, source_cache_ssh, name, source_cache_index=source_cache_index):
            deleted += 1
    save_state(config.state_file, state)
    summary = "\n".join(
        [
            "",
            "RETENTION DELETE SUMMARY",
            "========================",
            f"  attempted snapshots: {len(plan.delete)}",
            f"  completed snapshots: {deleted}",
            f"  retry snapshots:     {len(plan.delete) - deleted}",
            f"  remaining in state:{len(state.get('snapshots', {})):>5}",
            "",
        ]
    )
    print(summary)
    logger = get_logger()
    if logger:
        logger.success_text(summary + "\n")
    return plan
