"""Destination retention/pruning logic."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from . import btrfs
from .config import AppConfig
from .models import tags_text
from .state import remove_snapshot_from_state, resolve_destination_path, save_state
from .log import emit_success_summary
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
    """Return source cache send paths that follow a destination prune decision."""

    if not config.source.cleanup_superseded_cache or not config.source.cache_root:
        return []
    paths: dict[str, str] = {}
    for subvol_name, subvol in snapshot_state.get("subvolumes", {}).items():
        send_path = subvol.get("send_path")
        if isinstance(send_path, str) and btrfs.path_is_under_cache(send_path, config.source.cache_root):
            paths[subvol_name] = send_path
    return sorted(paths.items())


def _cleanup_source_cache_for_pruned_snapshot(
    config: AppConfig,
    ssh: SSHRunner,
    snapshot_name: str,
    snapshot_state: dict,
) -> None:
    """Best-effort source cache cleanup for one destination-pruned snapshot."""

    cache_paths = _source_cache_delete_paths(config, snapshot_state)
    if not cache_paths:
        return

    print()
    print("SOURCE CACHE RETENTION CLEANUP")
    print(f"  snapshot: {snapshot_name}")

    existing_paths = btrfs.remote_cache_existing_paths(
        ssh,
        sudo=config.source.sudo,
        btrfs_command=config.source.btrfs_command,
        cache_root=config.source.cache_root,
        paths=[send_path for _, send_path in cache_paths],
    )
    if existing_paths is None:
        print("  warning: could not list source cache; skipping source cache cleanup")
        return

    parent_dirs: set[str] = set()
    for subvol_name, send_path in cache_paths:
        if send_path not in existing_paths:
            print(f"  cache {subvol_name}: missing on source, skipping {send_path}")
            continue
        parent_dirs.add(str(Path(send_path).parent))
        print(f"  cache {subvol_name}: deleting {send_path}")
        result = btrfs.remote_delete_subvolume(
            ssh,
            config.source.sudo,
            config.source.btrfs_command,
            send_path,
            check=False,
        )
        if result.returncode != 0:
            print("  warning: source cache subvolume cleanup failed; leaving it in place")

    for parent_dir in sorted(parent_dirs):
        empty = btrfs.remote_cache_is_empty(
            ssh,
            sudo=config.source.sudo,
            btrfs_command=config.source.btrfs_command,
            cache_root=config.source.cache_root,
            path=parent_dir,
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
                print(f"  cache parent: deleted {parent_dir}")
            else:
                print("  warning: source cache parent cleanup failed; leaving it in place")
        elif empty is None:
            print(f"  cache parent: could not verify empty, keeping {parent_dir}")
        else:
            print(f"  cache parent: still has cached subvolumes, keeping {parent_dir}")


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


def _delete_snapshot(config: AppConfig, state: dict, snapshot_name: str) -> None:
    """Delete one received destination snapshot."""

    item = state.get("snapshots", {}).get(snapshot_name, {})
    snap_path = config.destination.target_root / "snapshots" / snapshot_name
    subvol_paths = [
        resolve_destination_path(config.destination.target_root, sub["destination_path"])
        for sub in item.get("subvolumes", {}).values()
        if sub.get("destination_path")
    ]
    for subvol_path in sorted(subvol_paths, key=lambda p: len(p.parts), reverse=True):
        if subvol_path.exists():
            btrfs.delete_local_subvolume(subvol_path, config.destination.sudo, config.destination.btrfs_command)
    if snap_path.exists():
        try:
            snap_path.rmdir()
        except OSError:
            pass
    remove_snapshot_from_state(state, snapshot_name)


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
        cache_paths = _source_cache_delete_paths(config, snapshot_state)
        if cache_paths:
            lines.append("      source cache cleanup follows this destination prune decision:")
            for subvol_name, send_path in cache_paths:
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
    for name in sorted(plan.delete):
        snapshot_state = state.get("snapshots", {}).get(name, {})
        print()
        print("RETENTION DELETE")
        print(f"  snapshot: {name}")
        print(f"  tags:     {tags_text(snapshot_state.get('tags', []))}")
        for reason in _delete_reasons(plan, name):
            print(f"  why:      {reason}")
        print("  action:   deleting destination subvolumes and removing state.json entry")
        _delete_snapshot(config, state, name)
        if source_cache_ssh:
            _cleanup_source_cache_for_pruned_snapshot(config, source_cache_ssh, name, snapshot_state)
        deleted += 1
    save_state(config.state_file, state)
    emit_success_summary(
        "\n".join(
            [
                "",
                "RETENTION DELETE SUMMARY",
                "========================",
                f"  deleted snapshots: {deleted}",
                f"  remaining in state:{len(state.get('snapshots', {})):>5}",
                "",
            ]
        )
    )
    return plan
