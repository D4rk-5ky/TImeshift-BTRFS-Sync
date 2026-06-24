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
from .mail import build_payload as build_mail_payload, send_status as send_mail_status
from .retention import prune
from .ssh import SSHRunner
from .state import load_state
from .sync import list_source_snapshots, print_snapshot_table, sync_once, verify_source_identity_for_manual_snapshot
from .timeshift import create_remote_manual_snapshot

EXAMPLE_CONFIG = '# timeshift-btrfs-sync v0.2.19 config\n# Run this config on the BACKUP/DESTINATION machine.\n# The SOURCE machine still only needs passwordless sudo for btrfs and timeshift.\n\n# Human-readable job name. This is only for identification.\nname = "kubuntu-timeshift"\n\n# Safe default: commands preview changes unless --run is passed.\ndefault_dry_run = true\n\n# If true, `sync` automatically runs destination retention pruning after a successful sync.\n# Safety rule: pruning only deletes for real when the command is also run with\n# both --run and --yes-delete. Without --yes-delete, the app prints the prune\n# plan and refuses to delete. Keep false while testing.\nprune_after_sync = false\n\n# Optional split file logging. If blank or omitted, only terminal output is used.\n# When set, the directory is created automatically and each run writes:\n#   *.log = normal commands and captured command output\n#   *.mbuffer = mbuffer progress/summary and transfer command header\n#   *.btrfs-out = btrfs send/receive verbose output and send/receive commands\n#   *.err = stderr/error output\nlog_dir = "/Backups/Kubuntu/timeshift-btrfs/.ts-btrfs-sync/logs"\n\n\n# Optional internal metadata paths. Omit these to use defaults under target_root:\n#   <target_root>/.ts-btrfs-sync/state.json\n#   <target_root>/.ts-btrfs-sync/lock\n# state_file records successfully received snapshots and incremental parent data.\n# lock_file prevents two sync/prune jobs from running against the same target.\n# state_file = "/Backups/Kubuntu/timeshift-btrfs/.ts-btrfs-sync/state.json"\n# lock_file = "/Backups/Kubuntu/timeshift-btrfs/.ts-btrfs-sync/lock"\n\n[mqtt]\n# Optional MQTT notifications for Home Assistant or another MQTT consumer.\n# This feature uses the paho-mqtt Python module. Install optional dependency with:\n#   python3 -m pip install -e \'.[mqtt]\'\n#\n# If enabled = false, paho-mqtt is not required and nothing is published.\nenabled = false\n\n# MQTT broker address and port. For Home Assistant add-on broker this is often\n# the HA host/IP and port 1883.\nhost = "homeassistant.local"\nport = 1883\n\n# Topic where JSON status messages are published.\n# Home Assistant can use this topic in an MQTT sensor or automation trigger.\ntopic = "timeshift-btrfs-sync/kubuntu-timeshift/status"\n\n# Optional MQTT authentication. Use either password or password_file, not both.\n# username = "mqtt-user"\n# password = "mqtt-password"\n# password_file = "/root/.config/ts-btrfs-mqtt.password"\n\n# Optional fixed MQTT client id. Blank/omitted creates one from the local hostname.\n# client_id = "ts-btrfs-kubuntu-timeshift"\n\n# MQTT publish options.\n# qos must be 0, 1, or 2. retain=true lets Home Assistant see the last known status\n# immediately after restart, but retain=false avoids stale retained status messages.\nqos = 0\nretain = false\ntimeout = 10\n\n# Control whether success and/or failure messages are sent.\nnotify_on_success = true\nnotify_on_failure = true\n\n\n[mail]\n# Optional email notifications using Python standard library smtplib/email.\n# No extra Python dependency is required. If enabled = false, no mail is sent.\nenabled = false\n\n# SMTP server settings. Typical STARTTLS setup uses port 587 with starttls=true.\n# Typical implicit SSL setup uses port 465 with smtp_ssl=true and starttls=false.\nsmtp_host = "smtp.example.com"\nsmtp_port = 587\nsmtp_ssl = false\nstarttls = true\ntimeout = 10\n\n# Optional SMTP authentication. Use either password or password_file, not both.\n# username = "smtp-user@example.com"\n# password = "smtp-password"\n# password_file = "/root/.config/ts-btrfs-mail.password"\n\n# Sender and recipients. to_addrs must contain at least one address when enabled=true.\nfrom_addr = "timeshift-btrfs-sync@example.com"\nto_addrs = ["admin@example.com"]\n\n# Mail content options.\nsubject_prefix = "[timeshift-btrfs-sync]"\ninclude_json = true\n\n# Control whether success and/or failure messages are sent.\nnotify_on_success = true\nnotify_on_failure = true\n\n\n[ssh]\n# Source machine containing the Timeshift snapshots.\nhost = "source-machine.example.lan"\n\n# Dedicated source SSH user.\nuser = "ts-btrfs-sync-user"\n\n# Optional SSH port.\n# port = 22\n\n# Recommended authentication: SSH private key.\n# This adds: ssh -i /root/.ssh/timeshift-btrfs-sync ...\n# The key file itself should be protected with chmod 600.\n# identity_file = "/root/.ssh/timeshift-btrfs-sync"\n\n# Optional SSH compression.\n# true adds: ssh -C\n# Useful over slow links; often unnecessary on fast LANs or already-compressed data.\ncompression = false\n\n# Optional SSH cipher choice.\n# This adds: ssh -c <cipher>\n# Leave unset/blank to use OpenSSH\'s default cipher negotiation.\n# cipher = "chacha20-poly1305@openssh.com"\n# cipher = "aes128-gcm@openssh.com"\n\n# Optional password authentication through sshpass on the DESTINATION machine.\n# Key-based auth is safer. If password/password_file is set, remove BatchMode=yes.\n# password = "your-ssh-password"\n# password_file = "/root/.ssh/timeshift-btrfs-sync.password"\n\n# Extra SSH options.\n# BatchMode=yes makes SSH fail instead of hanging on prompts. Use it for key auth.\n# Do NOT use BatchMode=yes together with password/password_file.\nextra_args = ["-o", "BatchMode=yes"]\n\n\n[manual_snapshot]\n# Optional source-side Timeshift on-demand snapshot creation before a normal sync.\n# When enabled = true, `sync` first reads sudo timeshift --list, verifies the\n# configured source if require_verified_source is true, then creates the snapshot:\n#   sudo -n timeshift --create --scripted --comments <comment>\n#\n# The command intentionally omits explicit --tags O. Timeshift defaults to\n# on-demand/tag O when no tag is supplied, and some Timeshift versions reject\n# explicit --tags O even though the help text lists O as valid.\n# Dry-run only prints what would happen. If --snapshot is used, automatic\n# creation is skipped because that command is a targeted sync.\nenabled = false\n\n# Independent cleanup for app-created on-demand snapshots.\n# This only affects tag O snapshots whose saved Timeshift comment contains marker.\n# It does not affect normal/user-created Timeshift on-demand snapshots.\n# Real deletion still requires prune to run with --run --yes-delete.\ncleanup_enabled = true\n\n# Safety guard for creating source-side on-demand snapshots. Keep true.\n# Before creating a manual snapshot, the app first runs timeshift --list and\n# requires the configured source to match an already received state.json entry\n# by Btrfs UUID. This prevents creating stale snapshots on the wrong mounted OS.\n# First-ever sync with no state should normally run with manual_snapshot.enabled\n# = false first, or this can be explicitly set false if you accept the risk.\nrequire_verified_source = true\n\n# Comment passed to Timeshift. Keep the marker text inside the comment so the\n# destination prune logic can recognize snapshots created by this app.\ncomment = "ts-btrfs-sync automatic on-demand snapshot"\nmarker = "ts-btrfs-sync"\n\n# Destination retention for app-created on-demand snapshots recognized by marker.\n# Default 10. This is independent from [retention].ondemand.\n# Set 0 to delete all matching app-created snapshots except globally protected\n# snapshots/newest common parent. Set cleanup_enabled = false to keep them all.\nretention_count = 10\n\n[source]\n# Source-side command prefix. sudo -n means sudo must not prompt for a password.\nsudo = "sudo -n"\n\n# Source-side commands. Absolute paths are also valid, for example /usr/bin/btrfs.\nbtrfs_command = "btrfs"\ntimeshift_command = "timeshift"\n\n# Snapshot discovery command:\n#   sudo -n timeshift --list\n# The app parses snapshot names from that output, then constructs paths as:\n#   snapshot_root/<snapshot-name>/<subvolume>\nsnapshot_root = "/timeshift-btrfs/snapshots"\n\n# Subvolumes expected inside each Timeshift snapshot.\nsubvolumes = ["@", "@home"]\n\n# Speed option. Default false is fast: discovery does NOT run btrfs show/property\n# for every snapshot/subvolume. It trusts the configured naming layout and only\n# checks Btrfs metadata when a subvolume is actually going to be sent.\n# Set true if you want list-source/sync discovery to verify every subvolume up front.\nverify_subvolumes_at_discovery = false\n\n# Safety option. Keep true. Before using an existing destination snapshot as an\n# incremental parent, the app compares current source Btrfs UUID metadata with\n# destination `received_uuid` metadata. This prevents accidentally mixing\n# snapshots from another OS/source into the same backup target.\nverify_incremental_parent = true\n\n# Performance/safety balance. When true, the first incremental parent for each\n# subvolume name (@, @home) is verified during a run. Later incrementals in the\n# same run trust the chain that this process just created, avoiding repeated\n# remote/local UUID checks for every single incremental send.\nverify_incremental_parent_once_per_run = true\n\n# Dangerous escape hatch. Keep false. If true, the app may continue when it\n# cannot prove that the selected incremental parent matches the current source.\nallow_incremental_without_parent_match = false\n\n# Source-side read-only cache for writable Timeshift snapshots.\n# Create only the top-level cache_root manually once on the source.\n# Per-snapshot cache parents are created with btrfs, not mkdir:\n#   sudo -n btrfs subvolume create <cache_root>/<snapshot-name>\n# Read-only send snapshots are then created with:\n#   sudo -n btrfs subvolume snapshot -r <original> <cache_root>/<snapshot>/<subvolume>\ncache_root = "/timeshift-btrfs/.ts-btrfs-sync/send-cache"\ncreate_readonly_cache = true\n\n# Source cache cleanup. Keep true. After a successful incremental send, the\n# previous source cache snapshot is no longer needed and is deleted with:\n#   sudo -n btrfs subvolume delete <old-cache-subvolume>\n# The newest/current cache snapshot is kept because it is needed as the parent\n# for the next incremental send, including the next program run.\ncleanup_superseded_cache = true\n\n# Optional Btrfs send compressed-data mode.\n# true adds: btrfs send --compressed-data\n# This preserves already-compressed source extents when supported. It is not the\n# same thing as choosing destination compression.\nsend_compressed_data = false\n\n# Optional Btrfs send protocol version.\n# Example: send_proto = 2 adds: btrfs send --proto 2\n# send_proto = 2\n\n[destination]\n# Local Btrfs backup root on the BACKUP/DESTINATION machine.\n#\n# The app creates two folders inside this target_root:\n#   snapshots/       = received Btrfs backup snapshots\n#   .ts-btrfs-sync/ = app state.json, lock file, logs\n#\n# If you completely reset/start over with a new full sync, clean both folders.\n# Do not delete only .ts-btrfs-sync/state.json while leaving old snapshots/.\n# Received @ and @home entries are Btrfs subvolumes, so delete those with\n# `btrfs subvolume delete` before removing the ordinary folders.\ntarget_root = "/Backups/Kubuntu/timeshift-btrfs"\n\n# Local sudo prefix for btrfs receive/delete/property commands.\nsudo = "sudo -n"\nbtrfs_command = "btrfs"\n\n# Whether the app may create target_root and internal metadata directories.\ncreate_target_root = true\n\n# Interrupted receive cleanup. Keep true. If a previous transfer was cancelled\n# and left a partial destination subvolume that is not in state.json, the app\n# deletes that incomplete Btrfs subvolume and retries the receive. It only\n# deletes paths that are Btrfs subvolumes or empty directories; non-empty normal\n# directories still require manual inspection.\ncleanup_incomplete_receive = true\n\n# Destination Btrfs compression property.\n# Accepted: zstd, lzo, zlib, none, or blank.\n# zstd:3 is accepted but normalized to zstd because `btrfs property set` does\n# not set compression levels. Use mount options for exact levels.\ncompression = "zstd"\n\n# Before receive, set compression on the destination snapshot parent directory.\nset_compression_before_receive = true\n\n# After receive, optionally try to set compression on the received subvolume.\n# Default false because received Btrfs snapshots are normally read-only, and\n# setting properties on a read-only subvolume fails. Leave false unless you\n# intentionally make received snapshots writable before setting properties.\nset_compression_after_receive = false\n\n[stream]\n# Optional mbuffer between SSH send and local receive.\n# false pipeline:\n#   ssh source \'btrfs send ...\' | btrfs receive ...\n# true pipeline:\n#   ssh source \'btrfs send ...\' | mbuffer -m 256M | btrfs receive ...\nuse_mbuffer = false\n\n# mbuffer command and memory buffer size.\nmbuffer_command = "mbuffer"\nmbuffer_size = "256M"\n\n# Optional mbuffer rate limit. Example: "100M" adds `mbuffer -R 100M`.\n# mbuffer_rate = "100M"\n\n# Optional extra mbuffer arguments as a TOML string list.\nmbuffer_extra_args = []\n\n# Optional Btrfs verbose output.\n# This adds -v to both:\n#   btrfs send -v ...\n#   btrfs receive -v ...\n# Btrfs verbose mode prints operation/details, not a clean percent progress bar.\n# mbuffer is still the useful throughput/total progress display.\nbtrfs_verbose = false\n\n[retention]\n# Destination retention counts by Timeshift tag.\n# H=hourly, D=daily, W=weekly, M=monthly, B=boot, O=on-demand/manual.\n#\n# These rules are used by:\n#   ts-btrfs prune --config ./config.toml --dry-run\n#   ts-btrfs prune --config ./config.toml --run --yes-delete\n#   ts-btrfs sync  --config ./config.toml --run --prune --yes-delete\n#\n# Important: `prune_after_sync = true` or `--prune` only enables the prune step.\n# Real deletion still requires `--run --yes-delete`.\nhourly = 6\ndaily = 7\nweekly = 4\nmonthly = 6\nboot = 5\n# Retention count for normal/user-created Timeshift on-demand snapshots.\n# This is ignored unless cleanup_ondemand = true.\nondemand = 10\n\n# Cleanup switch for normal/user-created Timeshift tag O snapshots.\n# Default false means your normal manual on-demand snapshots are never pruned by\n# this app unless you explicitly allow it. App-created on-demand cleanup is\n# controlled independently by [manual_snapshot].cleanup_enabled.\ncleanup_ondemand = false\n\n# Optional extension. Yearly is not native Timeshift behavior.\nyearly = 0\n\n# Extra safety: always keep newest snapshot and latest likely incremental parent.\nkeep_latest = true\nkeep_latest_common_parent = true\n\n# Names listed here are never pruned.\nprotected_snapshots = []\n'

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




def _send_mail_status(config, command_name: str, *, success: bool, exit_code: int, error: str = "", stderr_tail: str = "") -> None:
    """Send optional email status without changing the command exit code."""

    mail_config = getattr(config, "mail", None)
    if not mail_config or not mail_config.enabled:
        return
    if success and not mail_config.notify_on_success:
        return
    if not success and not mail_config.notify_on_failure:
        return

    payload = build_mail_payload(
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
        send_mail_status(mail_config, payload)
    except Exception as mail_exc:
        print(f"WARNING: mail notification failed: {mail_exc}", file=sys.stderr)


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
            _send_mail_status(config, command_name, success=(result == 0), exit_code=result)
            return result
        except KeyboardInterrupt as exc:
            if logger:
                logger.err("ERROR: Interrupted by user")
            stderr_tail = _stderr_tail_for_exception(exc, logger)
            _publish_mqtt_status(
                config,
                command_name,
                success=False,
                exit_code=130,
                error="Interrupted by user",
                stderr_tail=stderr_tail,
            )
            _send_mail_status(
                config,
                command_name,
                success=False,
                exit_code=130,
                error="Interrupted by user",
                stderr_tail=stderr_tail,
            )
            raise
        except Exception as exc:
            if logger:
                logger.err(f"ERROR: {exc}")
            stderr_tail = _stderr_tail_for_exception(exc, logger)
            exit_code = _failure_exit_code(exc)
            _publish_mqtt_status(
                config,
                command_name,
                success=False,
                exit_code=exit_code,
                error=str(exc),
                stderr_tail=stderr_tail,
            )
            _send_mail_status(
                config,
                command_name,
                success=False,
                exit_code=exit_code,
                error=str(exc),
                stderr_tail=stderr_tail,
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
        ssh = SSHRunner(config.ssh)
        ssh.test()
        if config.manual_snapshot.require_verified_source:
            print("MANUAL SNAPSHOT SOURCE IDENTITY CHECK")
            print("  require_verified_source: true")
            snapshots = [
                snap
                for snap in list_source_snapshots(
                    config,
                    ssh,
                    include_btrfs_info=config.source.verify_subvolumes_at_discovery,
                )
                if snap.subvolumes
            ]
            source_by_name = {snap.name: snap for snap in snapshots}
            confirmed_name, reason = verify_source_identity_for_manual_snapshot(
                config,
                ssh,
                load_state(config.state_file),
                source_by_name,
            )
            print(f"  confirmed source anchor: {confirmed_name}")
            print(f"  reason: {reason}")
            print()
        else:
            print("MANUAL SNAPSHOT SOURCE IDENTITY CHECK")
            print("  require_verified_source: false")
            print("  WARNING: creating a manual Timeshift snapshot without UUID-confirming the source first")
            print()
        create_remote_manual_snapshot(ssh, sudo=config.source.sudo, timeshift_command=config.source.timeshift_command, comment=args.comment)
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
        description=(
            "Ask source Timeshift to create an on-demand/manual snapshot with tag O.\n"
            "By default, manual_snapshot.require_verified_source also applies here: the source must match state.json by UUID first."
        ),
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
