"""Command-line interface for timeshift-btrfs-sync."""

from __future__ import annotations

from pathlib import Path
import argparse
import json
import sys
from . import __version__, btrfs, timeshift
from .commands import CommandError
from .config import ConfigError, load_config
from .lock import FileLock
from .log import active_logger, create_run_logger
from .mqtt import build_payload, publish_status
from .retention import prune
from .ssh import SSHRunner
from .state import load_state
from .sync import list_source_snapshots, print_snapshot_table, sync_once
from .timeshift import create_remote_manual_snapshot

EXAMPLE_CONFIG = '''# timeshift-btrfs-sync v0.2.7 config
# Run this config on the BACKUP/DESTINATION machine.
# The SOURCE machine still only needs passwordless sudo for btrfs and timeshift.

# Human-readable job name. This is only for identification.
name = "kubuntu-timeshift"

# Safe default: commands preview changes unless --run is passed.
default_dry_run = true

# If true, `sync` automatically runs destination retention pruning after a successful sync.
# Safety rule: pruning only deletes for real when the command is also run with
# both --run and --yes-delete. Without --yes-delete, the app prints the prune
# plan and refuses to delete. Keep false while testing.
prune_after_sync = false

# Optional split file logging. If blank or omitted, only terminal output is used.
# When set, the directory is created automatically and each run writes:
#   *.log = normal commands and captured command output
#   *.mbuffer = mbuffer progress/summary and transfer command header
#   *.btrfs-out = btrfs send/receive verbose output and send/receive commands
#   *.err = stderr/error output
log_dir = "/Backups/Kubuntu/timeshift-btrfs/.ts-btrfs-sync/logs"


[mqtt]
# Optional MQTT notifications for Home Assistant or another MQTT consumer.
# This feature uses the paho-mqtt Python module. Install optional dependency with:
#   python3 -m pip install -e '.[mqtt]'
#
# If enabled = false, paho-mqtt is not required and nothing is published.
enabled = false

# MQTT broker address and port. For Home Assistant add-on broker this is often
# the HA host/IP and port 1883.
host = "homeassistant.local"
port = 1883

# Topic where JSON status messages are published.
# Home Assistant can use this topic in an MQTT sensor or automation trigger.
topic = "timeshift-btrfs-sync/kubuntu-timeshift/status"

# Optional MQTT authentication. Use either password or password_file, not both.
# username = "mqtt-user"
# password = "mqtt-password"
# password_file = "/root/.config/ts-btrfs-mqtt.password"

# Optional fixed MQTT client id. Blank/omitted creates one from the local hostname.
# client_id = "ts-btrfs-kubuntu-timeshift"

# MQTT publish options.
# qos must be 0, 1, or 2. retain=true lets Home Assistant see the last known status
# immediately after restart, but retain=false avoids stale retained status messages.
qos = 0
retain = false
timeout = 10

# Control whether success and/or failure messages are sent.
notify_on_success = true
notify_on_failure = true

# Optional internal metadata paths. Omit these to use defaults under target_root:
#   <target_root>/.ts-btrfs-sync/state.json
#   <target_root>/.ts-btrfs-sync/lock
# state_file records successfully received snapshots and incremental parent data.
# lock_file prevents two sync/prune jobs from running against the same target.
# state_file = "/Backups/Kubuntu/timeshift-btrfs/.ts-btrfs-sync/state.json"
# lock_file = "/Backups/Kubuntu/timeshift-btrfs/.ts-btrfs-sync/lock"

[ssh]
# Source machine containing the Timeshift snapshots.
host = "source-machine.example.lan"

# Dedicated source SSH user.
user = "ts-btrfs-sync-user"

# Optional SSH port.
# port = 22

# Recommended authentication: SSH private key.
# This adds: ssh -i /root/.ssh/timeshift-btrfs-sync ...
# The key file itself should be protected with chmod 600.
# identity_file = "/root/.ssh/timeshift-btrfs-sync"

# Optional SSH compression.
# true adds: ssh -C
# Useful over slow links; often unnecessary on fast LANs or already-compressed data.
compression = false

# Optional SSH cipher choice.
# This adds: ssh -c <cipher>
# Leave unset/blank to use OpenSSH's default cipher negotiation.
# cipher = "chacha20-poly1305@openssh.com"
# cipher = "aes128-gcm@openssh.com"

# Optional password authentication through sshpass on the DESTINATION machine.
# Key-based auth is safer. If password/password_file is set, remove BatchMode=yes.
# password = "your-ssh-password"
# password_file = "/root/.ssh/timeshift-btrfs-sync.password"

# Extra SSH options.
# BatchMode=yes makes SSH fail instead of hanging on prompts. Use it for key auth.
# Do NOT use BatchMode=yes together with password/password_file.
extra_args = ["-o", "BatchMode=yes"]

[source]
# Source-side command prefix. sudo -n means sudo must not prompt for a password.
sudo = "sudo -n"

# Source-side commands. Absolute paths are also valid, for example /usr/bin/btrfs.
btrfs_command = "btrfs"
timeshift_command = "timeshift"

# Snapshot discovery command:
#   sudo -n timeshift --list
# The app parses snapshot names from that output, then constructs paths as:
#   snapshot_root/<snapshot-name>/<subvolume>
snapshot_root = "/timeshift-btrfs/snapshots"

# Subvolumes expected inside each Timeshift snapshot.
subvolumes = ["@", "@home"]

# Speed option. Default false is fast: discovery does NOT run btrfs show/property
# for every snapshot/subvolume. It trusts the configured naming layout and only
# checks Btrfs metadata when a subvolume is actually going to be sent.
# Set true if you want list-source/sync discovery to verify every subvolume up front.
verify_subvolumes_at_discovery = false

# Safety option. Keep true. Before using an existing destination snapshot as an
# incremental parent, the app compares current source Btrfs UUID metadata with
# destination `received_uuid` metadata. This prevents accidentally mixing
# snapshots from another OS/source into the same backup target.
verify_incremental_parent = true

# Performance/safety balance. When true, the first incremental parent for each
# subvolume name (@, @home) is verified during a run. Later incrementals in the
# same run trust the chain that this process just created, avoiding repeated
# remote/local UUID checks for every single incremental send.
verify_incremental_parent_once_per_run = true

# Dangerous escape hatch. Keep false. If true, the app may continue when it
# cannot prove that the selected incremental parent matches the current source.
allow_incremental_without_parent_match = false

# Source-side read-only cache for writable Timeshift snapshots.
# Create only the top-level cache_root manually once on the source.
# Per-snapshot cache parents are created with btrfs, not mkdir:
#   sudo -n btrfs subvolume create <cache_root>/<snapshot-name>
# Read-only send snapshots are then created with:
#   sudo -n btrfs subvolume snapshot -r <original> <cache_root>/<snapshot>/<subvolume>
cache_root = "/timeshift-btrfs/.ts-btrfs-sync/send-cache"
create_readonly_cache = true

# Source cache cleanup. Keep true. After a successful incremental send, the
# previous source cache snapshot is no longer needed and is deleted with:
#   sudo -n btrfs subvolume delete <old-cache-subvolume>
# The newest/current cache snapshot is kept because it is needed as the parent
# for the next incremental send, including the next program run.
cleanup_superseded_cache = true

# Optional Btrfs send compressed-data mode.
# true adds: btrfs send --compressed-data
# This preserves already-compressed source extents when supported. It is not the
# same thing as choosing destination compression.
send_compressed_data = false

# Optional Btrfs send protocol version.
# Example: send_proto = 2 adds: btrfs send --proto 2
# send_proto = 2

[destination]
# Local Btrfs backup root on the BACKUP/DESTINATION machine.
#
# The app creates two folders inside this target_root:
#   snapshots/       = received Btrfs backup snapshots
#   .ts-btrfs-sync/ = app state.json, lock file, logs
#
# If you completely reset/start over with a new full sync, clean both folders.
# Do not delete only .ts-btrfs-sync/state.json while leaving old snapshots/.
# Received @ and @home entries are Btrfs subvolumes, so delete those with
# `btrfs subvolume delete` before removing the ordinary folders.
target_root = "/Backups/Kubuntu/timeshift-btrfs"

# Local sudo prefix for btrfs receive/delete/property commands.
sudo = "sudo -n"
btrfs_command = "btrfs"

# Whether the app may create target_root and internal metadata directories.
create_target_root = true

# Interrupted receive cleanup. Keep true. If a previous transfer was cancelled
# and left a partial destination subvolume that is not in state.json, the app
# deletes that incomplete Btrfs subvolume and retries the receive. It only
# deletes paths that are Btrfs subvolumes or empty directories; non-empty normal
# directories still require manual inspection.
cleanup_incomplete_receive = true

# Destination Btrfs compression property.
# Accepted: zstd, lzo, zlib, none, or blank.
# zstd:3 is accepted but normalized to zstd because `btrfs property set` does
# not set compression levels. Use mount options for exact levels.
compression = "zstd"

# Before receive, set compression on the destination snapshot parent directory.
set_compression_before_receive = true

# After receive, optionally try to set compression on the received subvolume.
# Default false because received Btrfs snapshots are normally read-only, and
# setting properties on a read-only subvolume fails. Leave false unless you
# intentionally make received snapshots writable before setting properties.
set_compression_after_receive = false

[stream]
# Optional mbuffer between SSH send and local receive.
# false pipeline:
#   ssh source 'btrfs send ...' | btrfs receive ...
# true pipeline:
#   ssh source 'btrfs send ...' | mbuffer -m 256M | btrfs receive ...
use_mbuffer = false

# mbuffer command and memory buffer size.
mbuffer_command = "mbuffer"
mbuffer_size = "256M"

# Optional mbuffer rate limit. Example: "100M" adds `mbuffer -R 100M`.
# mbuffer_rate = "100M"

# Optional extra mbuffer arguments as a TOML string list.
mbuffer_extra_args = []

# Optional Btrfs verbose output.
# This adds -v to both:
#   btrfs send -v ...
#   btrfs receive -v ...
# Btrfs verbose mode prints operation/details, not a clean percent progress bar.
# mbuffer is still the useful throughput/total progress display.
btrfs_verbose = false

[retention]
# Destination retention counts by Timeshift tag.
# H=hourly, D=daily, W=weekly, M=monthly, B=boot, O=on-demand/manual.
#
# These rules are used by:
#   ts-btrfs prune --config ./config.toml --dry-run
#   ts-btrfs prune --config ./config.toml --run --yes-delete
#   ts-btrfs sync  --config ./config.toml --run --prune --yes-delete
#
# Important: `prune_after_sync = true` or `--prune` only enables the prune step.
# Real deletion still requires `--run --yes-delete`.
hourly = 6
daily = 7
weekly = 4
monthly = 6
boot = 5
ondemand = 10

# Optional extension. Yearly is not native Timeshift behavior.
yearly = 0

# Extra safety: always keep newest snapshot and latest likely incremental parent.
keep_latest = true
keep_latest_common_parent = true

# Names listed here are never pruned.
protected_snapshots = []
'''


def _failure_exit_code(exc: BaseException) -> int:
    """Map common failures to the same exit codes main() returns."""

    if isinstance(exc, CommandError):
        return exc.returncode
    if isinstance(exc, BlockingIOError):
        return 3
    if isinstance(exc, KeyboardInterrupt):
        return 130
    return 1


def _stderr_tail_for_exception(exc: BaseException, logger) -> str:
    """Return the best available recent stderr text for failure notifications."""

    if isinstance(exc, CommandError) and exc.stderr:
        return exc.stderr[-4000:]
    if logger:
        return logger.last_stderr_tail()
    return ""


def _publish_mqtt_status(config, command_name: str, *, success: bool, exit_code: int, error: str = "", stderr_tail: str = "") -> None:
    """Publish optional MQTT status without changing the command exit code."""

    mqtt_config = getattr(config, "mqtt", None)
    if not mqtt_config or not mqtt_config.enabled:
        return
    if success and not mqtt_config.notify_on_success:
        return
    if not success and not mqtt_config.notify_on_failure:
        return

    payload = build_payload(
        job_name=config.name,
        command=command_name,
        state="success" if success else "failure",
        success=success,
        exit_code=exit_code,
        stderr_tail=stderr_tail,
        error=error,
        version=__version__,
    )
    try:
        publish_status(mqtt_config, payload)
    except Exception as mqtt_exc:
        print(f"WARNING: MQTT notification failed: {mqtt_exc}", file=sys.stderr)


def _with_logging(config, command_name: str, callback):
    """Run a command with optional logging and MQTT notification.

    If config.log_dir is None, this is just a direct callback call plus optional
    MQTT. If log_dir is set, log.py creates timestamped run files. Exceptions
    are copied to .err and can also be sent as MQTT failure JSON before being
    re-raised to main().
    """

    logger = create_run_logger(config.log_dir, config.name)
    with active_logger(logger):
        if logger:
            logger.info(f"CLI COMMAND: {command_name}")
        try:
            result = int(callback() or 0)
            _publish_mqtt_status(config, command_name, success=(result == 0), exit_code=result)
            return result
        except KeyboardInterrupt as exc:
            if logger:
                logger.err("ERROR: Interrupted by user")
            _publish_mqtt_status(
                config,
                command_name,
                success=False,
                exit_code=130,
                error="Interrupted by user",
                stderr_tail=_stderr_tail_for_exception(exc, logger),
            )
            raise
        except Exception as exc:
            if logger:
                logger.err(f"ERROR: {exc}")
            _publish_mqtt_status(
                config,
                command_name,
                success=False,
                exit_code=_failure_exit_code(exc),
                error=str(exc),
                stderr_tail=_stderr_tail_for_exception(exc, logger),
            )
            raise


def _resolve_dry_run(args, config) -> bool:
    if getattr(args, "dry_run", False):
        return True
    if getattr(args, "run", False):
        return False
    return config.default_dry_run


def cmd_init_config(args) -> int:
    path = Path(args.path).expanduser()
    if path.exists() and not args.force:
        print(f"Refusing to overwrite existing file: {path}", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    print(f"Wrote example config: {path}")
    return 0


def cmd_test_ssh(args) -> int:
    config = load_config(args.config)

    def _run() -> int:
        ssh = SSHRunner(config.ssh)
        ssh.test()
        ssh.run(timeshift.timeshift_cmd(config.source.sudo, config.source.timeshift_command, ["--list"]))
        ssh.run(btrfs.remote_btrfs_cmd(config.source.sudo, config.source.btrfs_command, ["--version"]))
        print("SSH works. Source sudo for timeshift/btrfs works.")
        return 0

    return _with_logging(config, "test-ssh", _run)


def cmd_list_source(args) -> int:
    """List snapshots on the source machine.

    Default is fast listing: parse Timeshift names/tags and construct expected
    subvolume paths without probing every subvolume with Btrfs. Use
    --verify-btrfs when you explicitly want the slower full verification.
    """

    config = load_config(args.config)

    def _run() -> int:
        print_snapshot_table(
            list_source_snapshots(
                config,
                SSHRunner(config.ssh),
                include_btrfs_info=args.verify_btrfs,
            )
        )
        return 0

    return _with_logging(config, "list-source", _run)


def cmd_sync(args) -> int:
    config = load_config(args.config)

    def _run() -> int:
        dry_run = _resolve_dry_run(args, config)
        with FileLock(config.lock_file):
            state = load_state(config.state_file)
            sync_once(config, state, dry_run=dry_run, limit=args.limit, only_snapshot=args.snapshot, only_missing=not args.resend)
            if args.prune or config.prune_after_sync:
                prune(config, state, dry_run=dry_run, yes_delete=args.yes_delete)
        return 0

    return _with_logging(config, "sync", _run)


def cmd_prune(args) -> int:
    config = load_config(args.config)

    def _run() -> int:
        dry_run = _resolve_dry_run(args, config)
        with FileLock(config.lock_file):
            prune(config, load_state(config.state_file), dry_run=dry_run, yes_delete=args.yes_delete)
        return 0

    return _with_logging(config, "prune", _run)


def cmd_create_manual(args) -> int:
    config = load_config(args.config)

    def _run() -> int:
        create_remote_manual_snapshot(SSHRunner(config.ssh), sudo=config.source.sudo, timeshift_command=config.source.timeshift_command, comment=args.comment)
        print("Requested remote Timeshift on-demand snapshot.")
        return 0

    return _with_logging(config, "create-manual", _run)


def cmd_show_state(args) -> int:
    config = load_config(args.config)

    def _run() -> int:
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

    return _with_logging(config, "show-state", _run)


def build_parser() -> argparse.ArgumentParser:
    """Create the argparse parser and describe every CLI flag.

    Config options are documented in README.md and config.example.toml. This
    help output focuses on command-line flags available through:
      python3 -m timeshift_btrfs_sync --help
      python3 -m timeshift_btrfs_sync <command> --help
    """

    parser = argparse.ArgumentParser(
        prog="ts-btrfs",
        description="Pull Timeshift Btrfs snapshots over SSH.",
        epilog=(
            "Config options are documented in README.md and config.example.toml.\n"
            "Typical first test: ts-btrfs sync --config ./config.toml --dry-run"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "init-config",
        help="write an example TOML config",
        description="Write a complete commented TOML config template.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--path", default="./ts-btrfs.toml", help="where to write the example config; default: ./ts-btrfs.toml")
    p.add_argument("--force", action="store_true", help="overwrite the destination config file if it already exists")
    p.set_defaults(func=cmd_init_config)

    p = sub.add_parser(
        "test-ssh",
        help="test SSH and source sudo permissions",
        description="Verify SSH works and source sudo can run timeshift --list and btrfs --version.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--config", "-c", required=True, help="path to config.toml")
    p.set_defaults(func=cmd_test_ssh)

    p = sub.add_parser(
        "list-source",
        help="list source Timeshift snapshots",
        description=(
            "List Timeshift snapshots found on the source.\n"
            "Default is fast mode: parse timeshift --list and construct expected paths.\n"
            "Use --verify-btrfs to run slower btrfs checks for every listed subvolume."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--config", "-c", required=True, help="path to config.toml")
    p.add_argument(
        "--verify-btrfs",
        action="store_true",
        help="slow: verify every configured source subvolume with btrfs during listing",
    )
    p.set_defaults(func=cmd_list_source)

    p = sub.add_parser(
        "sync",
        help="pull missing snapshots",
        description=(
            "Pull missing Timeshift snapshot subvolumes from source to destination.\n"
            "Without --run or --dry-run, the config option default_dry_run decides.\n"
            "Real prune deletion still requires --yes-delete."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--config", "-c", required=True, help="path to config.toml")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview planned sends/prunes; do not receive or delete anything")
    mode.add_argument("--run", action="store_true", help="perform real send/receive work; required for actual changes")
    p.add_argument("--limit", type=int, help="transfer at most this many subvolumes; useful for first live test")
    p.add_argument("--snapshot", help="sync only this Timeshift snapshot name, for example 2026-06-23_07-10-24")
    p.add_argument("--resend", action="store_true", help="attempt transfer even if state.json says the subvolume was already synced")
    p.add_argument("--prune", action="store_true", help="run destination retention pruning after sync; real delete also needs --run --yes-delete")
    p.add_argument("--yes-delete", action="store_true", help="allow real pruning deletes when used with --run and --prune or prune_after_sync=true")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser(
        "prune",
        help="apply destination retention rules",
        description=(
            "Apply retention rules to destination snapshots only.\n"
            "Use --dry-run first. Real deletion requires both --run and --yes-delete."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--config", "-c", required=True, help="path to config.toml")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show what would be deleted; do not delete anything")
    mode.add_argument("--run", action="store_true", help="perform real pruning if --yes-delete is also present")
    p.add_argument("--yes-delete", action="store_true", help="explicit safety confirmation required before real prune deletes")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser(
        "create-manual",
        help="create source Timeshift tag O snapshot",
        description="Ask source Timeshift to create an on-demand/manual snapshot with tag O.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--config", "-c", required=True, help="path to config.toml")
    p.add_argument("--comment", required=True, help="comment passed to timeshift --create --comments")
    p.set_defaults(func=cmd_create_manual)

    p = sub.add_parser(
        "show-state",
        help="show local sync state",
        description="Show state.json, which records completed transfers and incremental parent metadata.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--config", "-c", required=True, help="path to config.toml")
    p.add_argument("--json", action="store_true", help="print raw state.json instead of a short table")
    p.set_defaults(func=cmd_show_state)
    return parser

def main(argv: list[str] | None = None) -> int:
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
