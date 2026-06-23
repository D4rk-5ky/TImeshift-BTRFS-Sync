"""Command-line interface for timeshift-btrfs-sync.

This file contains only argparse wiring and high-level command handlers. The
real Btrfs/Timeshift work lives in sync.py, timeshift.py, btrfs.py, and
retention.py.
"""

from __future__ import annotations

from pathlib import Path
import argparse
import json
import sys

from . import __version__, btrfs, timeshift
from .config import ConfigError, load_config
from .lock import FileLock
from .retention import prune
from .ssh import SSHRunner
from .state import load_state
from .sync import list_source_snapshots, print_snapshot_table, sync_once
from .timeshift import create_remote_manual_snapshot


# The template written by `ts-btrfs init-config`. The normal config.example.toml
# file has the same fields, but embedding this makes bootstrapping easy.
EXAMPLE_CONFIG = '''# timeshift-btrfs-sync v0.3.2 minimal-source-sudo config
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
# Source-side passwordless sudo is only needed for btrfs and timeshift.
sudo = "sudo -n"
btrfs_command = "btrfs"
timeshift_command = "timeshift"

# The app parses snapshot names from `timeshift --list`, then constructs paths as:
#   snapshot_root/<snapshot-name>/<subvolume>
snapshot_root = "/timeshift-btrfs/snapshots"
subvolumes = ["@", "@home"]

# Optional source-side read-only cache for writable Timeshift snapshots.
# Create this top-level directory manually once. The app will not run mkdir on source.
cache_root = "/timeshift-btrfs/.ts-btrfs-sync/send-cache"
create_readonly_cache = true

[destination]
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
yearly = 0
keep_latest = true
keep_latest_common_parent = true
protected_snapshots = []
'''


def _resolve_dry_run(args, config) -> bool:
    """Decide whether a command should preview or perform changes."""

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
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    print(f"Wrote example config: {path}")
    return 0


def cmd_test_ssh(args) -> int:
    """Test SSH plus the two required source sudo commands."""

    config = load_config(args.config)
    ssh = SSHRunner(config.ssh)
    ssh.test()

    # Test source Timeshift permission: discovery depends on this.
    ssh.run(timeshift.timeshift_cmd(config.source.sudo, config.source.timeshift_command, ["--list"]))

    # Test source Btrfs permission: metadata checks/send/cache depend on this.
    ssh.run(btrfs.remote_btrfs_cmd(config.source.sudo, config.source.btrfs_command, ["--version"]))

    print("SSH works. Passwordless source sudo for timeshift and btrfs works.")
    return 0


def cmd_list_source(args) -> int:
    """List Timeshift snapshots discovered on the source."""

    config = load_config(args.config)
    snapshots = list_source_snapshots(config, SSHRunner(config.ssh), include_btrfs_info=not args.fast)
    print_snapshot_table(snapshots)
    return 0


def cmd_sync(args) -> int:
    """Run one destination-pull sync pass, optionally followed by pruning."""

    config = load_config(args.config)
    dry_run = _resolve_dry_run(args, config)
    with FileLock(config.lock_file):
        state = load_state(config.state_file)
        sync_once(config, state, dry_run=dry_run, limit=args.limit, only_snapshot=args.snapshot, only_missing=not args.resend)
        if args.prune or config.prune_after_sync:
            prune(config, state, dry_run=dry_run, yes_delete=args.yes_delete)
    return 0


def cmd_prune(args) -> int:
    """Apply destination retention rules without syncing first."""

    config = load_config(args.config)
    dry_run = _resolve_dry_run(args, config)
    with FileLock(config.lock_file):
        prune(config, load_state(config.state_file), dry_run=dry_run, yes_delete=args.yes_delete)
    return 0


def cmd_create_manual(args) -> int:
    """Request a source Timeshift on-demand/manual snapshot with tag O."""

    config = load_config(args.config)
    create_remote_manual_snapshot(
        SSHRunner(config.ssh),
        sudo=config.source.sudo,
        timeshift_command=config.source.timeshift_command,
        comment=args.comment,
    )
    print("Requested remote Timeshift on-demand snapshot.")
    return 0


def cmd_show_state(args) -> int:
    """Display local state.json in either table or JSON form."""

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
    for name in sorted(snapshots):
        item = snapshots[name]
        print(f"{name:<22} {''.join(item.get('tags', [])) or '-':<8} {','.join(sorted(item.get('subvolumes', {}).keys())) or '-'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Create the argparse parser and all subcommands."""

    parser = argparse.ArgumentParser(
        prog="ts-btrfs",
        description="Pull Timeshift Btrfs snapshots over SSH using only source sudo btrfs/timeshift.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # init-config writes an editable starting config.
    p = sub.add_parser("init-config", help="write an example TOML config")
    p.add_argument("--path", default="./ts-btrfs.toml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init_config)

    # test-ssh checks both SSH and the minimal source sudo commands.
    p = sub.add_parser("test-ssh", help="test SSH and source sudo permissions")
    p.add_argument("--config", "-c", required=True)
    p.set_defaults(func=cmd_test_ssh)

    # list-source is the discovery/debug command.
    p = sub.add_parser("list-source", help="list source Timeshift snapshots")
    p.add_argument("--config", "-c", required=True)
    p.add_argument("--fast", action="store_true", help="skip btrfs metadata reads")
    p.set_defaults(func=cmd_list_source)

    # sync does the real backup. It defaults to config.default_dry_run unless
    # --dry-run or --run overrides it.
    p = sub.add_parser("sync", help="pull missing snapshots")
    p.add_argument("--config", "-c", required=True)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--run", action="store_true")
    p.add_argument("--limit", type=int, help="transfer at most this many subvolumes")
    p.add_argument("--snapshot", help="sync only this snapshot name")
    p.add_argument("--resend", action="store_true", help="try even if state says already synced")
    p.add_argument("--prune", action="store_true", help="run retention after sync")
    p.add_argument("--yes-delete", action="store_true", help="required for real prune deletes")
    p.set_defaults(func=cmd_sync)

    # prune deletes old destination snapshots according to retention rules.
    p = sub.add_parser("prune", help="apply destination retention rules")
    p.add_argument("--config", "-c", required=True)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--run", action="store_true")
    p.add_argument("--yes-delete", action="store_true")
    p.set_defaults(func=cmd_prune)

    # create-manual asks source Timeshift to make an on-demand snapshot.
    p = sub.add_parser("create-manual", help="create source Timeshift tag O snapshot")
    p.add_argument("--config", "-c", required=True)
    p.add_argument("--comment", required=True)
    p.set_defaults(func=cmd_create_manual)

    # show-state is useful for debugging incremental parent selection.
    p = sub.add_parser("show-state", help="show local sync state")
    p.add_argument("--config", "-c", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show_state)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Program entry point used by console_scripts and python -m."""

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
