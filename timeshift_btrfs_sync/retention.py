"""Destination retention/pruning logic."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from . import btrfs
from .config import AppConfig
from .state import remove_snapshot_from_state, save_state


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
            plan.add_delete(name, "outside retention")
    plan.delete -= plan.keep
    return plan


def _delete_snapshot(config: AppConfig, state: dict, snapshot_name: str) -> None:
    """Delete one received destination snapshot."""

    item = state.get("snapshots", {}).get(snapshot_name, {})
    snap_path = config.destination.target_root / "snapshots" / snapshot_name
    subvol_paths = [Path(sub["destination_path"]) for sub in item.get("subvolumes", {}).values() if sub.get("destination_path")]
    for subvol_path in sorted(subvol_paths, key=lambda p: len(p.parts), reverse=True):
        if subvol_path.exists():
            btrfs.delete_local_subvolume(subvol_path, config.destination.sudo, config.destination.btrfs_command)
    if snap_path.exists():
        try:
            snap_path.rmdir()
        except OSError:
            pass
    remove_snapshot_from_state(state, snapshot_name)


def print_prune_plan(plan: PrunePlan) -> None:
    """Print delete candidates."""

    if not plan.delete:
        print("Nothing to prune.")
        return
    print("Snapshots selected for deletion:")
    for name in sorted(plan.delete):
        print(f"  {name}  ({'; '.join(plan.reasons.get(name, []))})")


def prune(config: AppConfig, state: dict, *, dry_run: bool, yes_delete: bool) -> PrunePlan:
    """Apply destination retention rules."""

    plan = build_prune_plan(config, state)
    print_prune_plan(plan)
    if dry_run:
        print("Dry-run: nothing deleted.")
        return plan
    if plan.delete and not yes_delete:
        raise RuntimeError("Refusing to delete without --yes-delete")
    for name in sorted(plan.delete):
        print(f"Deleting {name}")
        _delete_snapshot(config, state, name)
    save_state(config.state_file, state)
    return plan
