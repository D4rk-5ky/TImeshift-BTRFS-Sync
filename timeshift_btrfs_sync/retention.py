"""Destination retention/pruning logic.

This module decides which already-synced snapshots should be kept or deleted
from the backup destination. It uses the tags stored in state.json, matching the
Timeshift-style H/D/W/M/B/O retention model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import btrfs
from .config import AppConfig
from .state import remove_snapshot_from_state, save_state


@dataclass(slots=True)
class PrunePlan:
    """A dry-run friendly plan describing what retention would keep/delete."""

    # Snapshot names that must remain on the destination.
    keep: set[str] = field(default_factory=set)

    # Snapshot names selected for deletion.
    delete: set[str] = field(default_factory=set)

    # Human-readable reasons for keep/delete decisions.
    reasons: dict[str, list[str]] = field(default_factory=dict)

    def add_keep(self, snapshot: str, reason: str) -> None:
        """Mark a snapshot as kept and record why."""

        self.keep.add(snapshot)
        self.reasons.setdefault(snapshot, []).append(f"keep: {reason}")

    def add_delete(self, snapshot: str, reason: str) -> None:
        """Mark a snapshot for deletion unless it was already protected."""

        if snapshot not in self.keep:
            self.delete.add(snapshot)
        self.reasons.setdefault(snapshot, []).append(f"delete: {reason}")


def build_prune_plan(config: AppConfig, state: dict) -> PrunePlan:
    """Build a retention plan without touching the filesystem."""

    snapshots = state.get("snapshots", {})
    plan = PrunePlan()
    names = sorted(snapshots.keys())
    if not names:
        return plan

    # Explicitly protected snapshots win over all retention rules.
    protected = set(config.retention.protected_snapshots)
    for name in protected:
        if name in snapshots:
            plan.add_keep(name, "protected")

    # Always keeping newest is a useful safety net even if its tag count is 0.
    if config.retention.keep_latest:
        plan.add_keep(names[-1], "newest synced snapshot")

    # Apply per-tag retention. For each tag, keep the newest N snapshots that
    # contain that tag. A snapshot can be kept by more than one tag.
    counts = config.retention.counts_by_tag()
    for tag, count in counts.items():
        if count <= 0:
            continue
        tagged = [name for name in names if tag in snapshots[name].get("tags", [])]
        tagged.sort(reverse=True)
        for name in tagged[:count]:
            plan.add_keep(name, f"tag {tag} retention count {count}")

    if config.retention.keep_latest_common_parent:
        # Keep the newest synced snapshot so the next run has at least one safe
        # incremental parent and is less likely to fall back to a full send.
        plan.add_keep(names[-1], "latest common parent safety")

    # Everything not kept by one of the rules becomes a delete candidate.
    for name in names:
        if name not in plan.keep:
            plan.add_delete(name, "outside retention")

    # Final guard: keep always wins if a snapshot somehow landed in both sets.
    plan.delete -= plan.keep
    return plan


def _snapshot_path(config: AppConfig, snapshot_name: str) -> Path:
    """Return the local destination directory for one snapshot."""

    return config.destination.target_root / "snapshots" / snapshot_name


def _delete_snapshot(config: AppConfig, state: dict, snapshot_name: str) -> None:
    """Delete one destination snapshot from disk and then from state.json."""

    item = state.get("snapshots", {}).get(snapshot_name, {})
    snap_path = _snapshot_path(config, snapshot_name)

    # The state file knows which received paths are actual Btrfs subvolumes.
    subvol_paths: list[Path] = []
    for sub in item.get("subvolumes", {}).values():
        dest_path = sub.get("destination_path")
        if dest_path:
            subvol_paths.append(Path(dest_path))

    # Delete deeper paths first, in case future versions support nested
    # subvolumes. Btrfs will not delete a parent subvolume with children first.
    for subvol_path in sorted(subvol_paths, key=lambda p: len(p.parts), reverse=True):
        if subvol_path.exists():
            btrfs.delete_local_subvolume(subvol_path, config.destination.sudo)

    # Remove the now-empty Timeshift snapshot folder. If only info.json remains,
    # delete that metadata file and try again.
    if snap_path.exists():
        try:
            snap_path.rmdir()
        except OSError:
            info = snap_path / "info.json"
            if info.exists():
                info.unlink()
            try:
                snap_path.rmdir()
            except OSError:
                # Leave unexpected leftover files in place rather than deleting
                # non-Btrfs data that the user may have placed there.
                pass

    remove_snapshot_from_state(state, snapshot_name)


def print_prune_plan(plan: PrunePlan) -> None:
    """Print the delete side of a prune plan in a human-readable way."""

    if not plan.delete:
        print("Nothing to prune.")
        return
    print("Snapshots selected for deletion:")
    for name in sorted(plan.delete):
        reasons = "; ".join(plan.reasons.get(name, []))
        print(f"  {name}  ({reasons})")


def prune(config: AppConfig, state: dict, *, dry_run: bool, yes_delete: bool) -> PrunePlan:
    """Apply retention rules, optionally deleting snapshots for real.

    In dry-run mode the function only prints what would happen. In real mode,
    --yes-delete is required as an extra guard against accidental pruning.
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
