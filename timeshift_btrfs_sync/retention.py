"""Destination pruning/retention logic.

Pruning only touches the backup destination. Source snapshots are never deleted
by this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import btrfs
from .config import AppConfig
from .state import remove_snapshot_from_state, save_state


@dataclass(slots=True)
class PrunePlan:
    """Dry-run friendly plan showing which snapshots are kept/deleted."""

    keep: set[str] = field(default_factory=set)
    delete: set[str] = field(default_factory=set)
    reasons: dict[str, list[str]] = field(default_factory=dict)

    def add_keep(self, snapshot: str, reason: str) -> None:
        """Mark a snapshot as kept and record why."""

        self.keep.add(snapshot)
        self.reasons.setdefault(snapshot, []).append(f"keep: {reason}")

    def add_delete(self, snapshot: str, reason: str) -> None:
        """Mark a snapshot as delete candidate unless already kept."""

        if snapshot not in self.keep:
            self.delete.add(snapshot)
        self.reasons.setdefault(snapshot, []).append(f"delete: {reason}")


def build_prune_plan(config: AppConfig, state: dict) -> PrunePlan:
    """Build a retention plan from state.json without deleting anything."""

    snapshots = state.get("snapshots", {})
    names = sorted(snapshots.keys())
    plan = PrunePlan()
    if not names:
        return plan

    # Explicitly protected snapshots always win.
    for name in config.retention.protected_snapshots:
        if name in snapshots:
            plan.add_keep(name, "protected")

    # Keep newest snapshot as a general safety net.
    if config.retention.keep_latest:
        plan.add_keep(names[-1], "newest synced snapshot")

    # Keep newest N snapshots for each Timeshift tag.
    for tag, count in config.retention.counts_by_tag().items():
        if count <= 0:
            continue
        tagged = [name for name in names if tag in snapshots[name].get("tags", [])]
        tagged.sort(reverse=True)
        for name in tagged[:count]:
            plan.add_keep(name, f"tag {tag} retention count {count}")

    # Keep newest snapshot as the most likely future incremental parent.
    if config.retention.keep_latest_common_parent:
        plan.add_keep(names[-1], "latest common parent safety")

    # Anything not kept is a delete candidate.
    for name in names:
        if name not in plan.keep:
            plan.add_delete(name, "outside retention")
    plan.delete -= plan.keep
    return plan


def _snapshot_path(config: AppConfig, snapshot_name: str) -> Path:
    """Return the local destination folder for one snapshot."""

    return config.destination.target_root / "snapshots" / snapshot_name


def _delete_snapshot(config: AppConfig, state: dict, snapshot_name: str) -> None:
    """Delete one destination snapshot and remove it from state."""

    item = state.get("snapshots", {}).get(snapshot_name, {})
    snap_path = _snapshot_path(config, snapshot_name)

    # Delete actual Btrfs subvolumes first. Sort deepest paths first for safety.
    subvol_paths = [Path(sub["destination_path"]) for sub in item.get("subvolumes", {}).values() if sub.get("destination_path")]
    for subvol_path in sorted(subvol_paths, key=lambda p: len(p.parts), reverse=True):
        if subvol_path.exists():
            btrfs.delete_local_subvolume(subvol_path, config.destination.sudo)

    # Remove the now-empty snapshot directory if possible.
    if snap_path.exists():
        try:
            snap_path.rmdir()
        except OSError:
            # Leave unexpected files in place instead of deleting user data.
            pass

    remove_snapshot_from_state(state, snapshot_name)


def print_prune_plan(plan: PrunePlan) -> None:
    """Print the delete side of the plan in a readable form."""

    if not plan.delete:
        print("Nothing to prune.")
        return
    print("Snapshots selected for deletion:")
    for name in sorted(plan.delete):
        print(f"  {name}  ({'; '.join(plan.reasons.get(name, []))})")


def prune(config: AppConfig, state: dict, *, dry_run: bool, yes_delete: bool) -> PrunePlan:
    """Apply destination retention rules.

    In dry-run mode this only prints what would be deleted. Real deletion also
    requires --yes-delete to avoid accidental pruning.
    """

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
