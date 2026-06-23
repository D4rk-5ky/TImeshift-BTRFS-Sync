"""Command-line interface for timeshift-btrfs-sync.

The CLI is intentionally thin: it parses arguments, loads the config, acquires
the lock where needed, and calls the real implementation in sync.py,
retention.py, or timeshift.py.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import json

from . import __version__
from .config import ConfigError, load_config
from .lock import FileLock
from .retention import prune
from .ssh import SSHRunner
from .state import load_state
from .sync import list_source_snapshots, print_snapshot_table, sync_once
from .timeshift import create_remote_manual_snapshot


# Template written by `ts-btrfs init-config`. It is kept here so users can
# bootstrap a config without separately copying config.example.toml.
EXAMPLE_CONFIG = '''# timeshift-btrfs-sync destination-pull config
# Run this app on the BACKUP/DESTINATION machine.

name = "kubuntu-timeshift"
default_dry_run = true
prune_after_sync = false

[ssh]
host = "source-machine.example.lan"
user = "btrbk-source"
# port = 22
# identity_file = "/root/.ssh/timeshift-btrfs-sync"
extra_args = ["-o", "BatchMode=yes"]

[source]
# Timeshift Btrfs snapshot root on the SOURCE machine.
# Common examples:
#   /timeshift-btrfs/snapshots
#   /run/timeshift/backup/timeshift-btrfs/snapshots
snapshot_root = "/timeshift-btrfs/snapshots"

# App-managed read-only cache on the SOURCE if original snapshots are writable.
# If omitted, this defaults to: <parent-of-snapshot_root>/.ts-btrfs-sync/send-cache
# cache_root = "/timeshift-btrfs/.ts-btrfs-sync/send-cache"

# Timeshift usually stores @ and sometimes @home.
subvolumes = ["@", "@home"]

# Use "" if the SSH user can run btrfs/timeshift directly without sudo.
sudo = "sudo -n"
timeshift_command = "timeshift"

[destination]
# Local Btrfs backup root on the BACKUP/DESTINATION machine.
target_root = "/Backups/Kubuntu/timeshift-btrfs"
sudo = "sudo -n"
create_target_root = true

[retention]
hourly = 6
daily = 7
weekly = 4
monthly = 6
boot = 5
ondemand = 10

# Optional extension. Not native Timeshift behavior.
yearly = 0

keep_latest = true
keep_latest_common_parent = true
protected_snapshots = []
'''


def _resolve_dry_run(args, config) -> bool:
    """Decide whether this command should make changes or only preview them."""

    if getattr(args, "dry_run", False):
        return True
    if getattr(args, "run", False):
        return False
    return config.default_dry_run


def cmd_init_config(args) -> int:
    """Write an example TOML config to disk."""

    path = Path(args.path).expanduser()
    if path.exists() and not args.force:
        print(f"Refusing to overwrite existing file: {path}", file=sys.stderr)
        print("Use --force to overwrite.", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    print(f"Wrote example config: {path}")
    return 0


def cmd_list_source(args) -> int:
    """List Timeshift snapshots found on the remote/source machine."""

    config = load_config(args.config)
    ssh = SSHRunner(config.ssh)
    snapshots = list_source_snapshots(config, ssh, include_btrfs_info=not args.fast)
    print_snapshot_table(snapshots)
    return 0


def cmd_test_ssh(args) -> int:
    """Check that SSH to the source machine works."""

    config = load_config(args.config)
    ssh = SSHRunner(config.ssh)
    ssh.test()
    print("SSH connection works.")
    return 0


def cmd_sync(args) -> int:
    """Run one pull-sync pass, optionally followed by pruning."""

    config = load_config(args.config)
    dry_run = _resolve_dry_run(args, config)

    # Sync modifies state and possibly the destination filesystem, so it must be
    # protected by the per-config lock file.
    with FileLock(config.lock_file):
        state = load_state(config.state_file)
        sync_once(
            config,
            state,
            dry_run=dry_run,
            limit=args.limit,
            only_snapshot=args.snapshot,
            only_missing=not args.resend,
        )
        if args.prune or config.prune_after_sync:
            prune(config, state, dry_run=dry_run, yes_delete=args.yes_delete)
    return 0


def cmd_prune(args) -> int:
    """Apply retention rules to already-synced destination snapshots."""

    config = load_config(args.config)
    dry_run = _resolve_dry_run(args, config)
    with FileLock(config.lock_file):
        state = load_state(config.state_file)
        prune(config, state, dry_run=dry_run, yes_delete=args.yes_delete)
    return 0


def cmd_create_manual(args) -> int:
    """Create a Timeshift on-demand/manual snapshot on the source machine."""

    config = load_config(args.config)
    ssh = SSHRunner(config.ssh)
    create_remote_manual_snapshot(
        ssh,
        sudo=config.source.sudo,
        timeshift_command=config.source.timeshift_command,
        comment=args.comment,
    )
    print("Requested remote Timeshift on-demand snapshot.")
    return 0


def cmd_show_state(args) -> int:
    """Show the local state.json in either table or JSON form."""

    config = load_config(args.config)
    state = load_state(config.state_file)
    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0

    snapshots = state.get("snapshots", {})
    if not snapshots:
        print("State is empty.")
        return 0

    print(f"{'SNAPSHOT':<22} {'TAGS':<8} SUBVOLUMES")
    for name in sorted(snapshots.keys()):
        item = snapshots[name]
        tags = "".join(item.get("tags", [])) or "-"
        subvols = ",".join(sorted(item.get("subvolumes", {}).keys())) or "-"
        print(f"{name:<22} {tags:<8} {subvols}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create and return the argparse parser with all subcommands."""

    parser = argparse.ArgumentParser(
        prog="ts-btrfs",
        description="Destination-pull Btrfs send/receive sync for Timeshift Btrfs snapshots.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    # All actual work is reached through subcommands.
    sub = parser.add_subparsers(dest="command", required=True)

    # init-config: create an editable starting TOML file.
    p = sub.add_parser("init-config", help="write an example TOML config")
    p.add_argument("--path", default="./ts-btrfs.toml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init_config)

    # test-ssh: quick connection check before doing anything with Btrfs.
    p = sub.add_parser("test-ssh", help="test SSH connectivity to the source machine")
    p.add_argument("--config", "-c", required=True)
    p.set_defaults(func=cmd_test_ssh)

    # list-source: show discovered source snapshots and subvolumes.
    p = sub.add_parser("list-source", help="list Timeshift snapshots on the source machine")
    p.add_argument("--config", "-c", required=True)
    p.add_argument("--fast", action="store_true", help="skip btrfs subvolume metadata reads")
    p.set_defaults(func=cmd_list_source)

    # sync: pull missing snapshots. It defaults to the config's dry-run setting,
    # but --dry-run or --run can override that per invocation.
    p = sub.add_parser("sync", help="pull missing snapshots from source to destination")
    p.add_argument("--config", "-c", required=True)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show what would happen")
    mode.add_argument("--run", action="store_true", help="actually run send/receive")
    p.add_argument("--limit", type=int, help="maximum number of subvolumes to transfer")
    p.add_argument("--snapshot", help="sync only this snapshot name")
    p.add_argument("--resend", action="store_true", help="try to send even if state says it is already synced")
    p.add_argument("--prune", action="store_true", help="run retention pruning after sync")
    p.add_argument("--yes-delete", action="store_true", help="required for real pruning deletes")
    p.set_defaults(func=cmd_sync)

    # prune: run retention without syncing first.
    p = sub.add_parser("prune", help="apply destination retention rules")
    p.add_argument("--config", "-c", required=True)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--run", action="store_true")
    p.add_argument("--yes-delete", action="store_true", help="required for real deletion")
    p.set_defaults(func=cmd_prune)

    # create-manual: ask source Timeshift to create a tag O snapshot.
    p = sub.add_parser("create-manual", help="create a remote Timeshift on-demand snapshot with tag O")
    p.add_argument("--config", "-c", required=True)
    p.add_argument("--comment", required=True)
    p.set_defaults(func=cmd_create_manual)

    # show-state: inspect the local state file for troubleshooting.
    p = sub.add_parser("show-state", help="show local sync state")
    p.add_argument("--config", "-c", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show_state)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point used by both console_scripts and python -m."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except BlockingIOError:
        print("Another ts-btrfs process is already running for this config.", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
