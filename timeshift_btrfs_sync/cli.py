"""Command-line interface for timeshift-btrfs-sync."""

from __future__ import annotations

from pathlib import Path
from importlib.resources import files
import argparse
import json
import sys
from . import __version__, btrfs, timeshift
from .commands import CommandError
from .config import ConfigError, load_config
from .lock import FileLock
from .log import active_logger, create_run_logger
from .mail import send_status as send_mail_status
from .mqtt import publish_status
from .notify import build_notification_payload
from .retention import prune
from .destroy import destroy_leftovers
from .source import SourceRunner
from .state import load_state, refresh_state_metadata_and_report
from .sync import confirm_source_identity_before_manual_snapshot, list_source_snapshots, print_snapshot_table, source_snapshot_index, sync_once
from .preflight import check_required_sync_paths
from .timeshift import create_source_manual_snapshot


CLI_FORMATTER = argparse.RawTextHelpFormatter


def new_subparser(sub, name: str, help_text: str, description: str, func):
    parser = sub.add_parser(name, help=help_text, description=description, formatter_class=CLI_FORMATTER)
    parser.set_defaults(func=func)
    return parser


def add_config_arg(parser) -> None: parser.add_argument("--config", "-c", required=True, help="path to config.toml")

def add_run_mode_args(parser, *, dry_run_help: str, run_help: str) -> None:
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help=dry_run_help)
    mode.add_argument("--run", action="store_true", help=run_help)


def add_yes_delete_arg(parser, help_text: str) -> None:
    parser.add_argument("--yes-delete", action="store_true", help=help_text)


def _failure_exit_code(exc: BaseException) -> int:
    """Return a stable CLI exit code for failure notifications.

    CommandError carries the real return code from the failed external command,
    such as btrfs send/receive or ssh. Other Python-side safety errors use 1.
    Keeping this helper defined avoids masking the real error with a secondary
    NameError during MQTT/mail failure handling.
    """

    if isinstance(exc, CommandError):
        try:
            return int(exc.returncode)
        except Exception:
            return 1
    return 1

def _stderr_tail_for_exception(exc: BaseException, logger) -> str:
    """Return the best available recent stderr text for failure notifications."""

    if isinstance(exc, CommandError) and exc.stderr:
        return exc.stderr[-4000:]
    if logger:
        return logger.last_stderr_tail()
    return ""


def _send_notifications(
    config,
    command_name: str,
    *,
    success: bool,
    exit_code: int,
    error: str = "",
    stderr_tail: str = "",
    attachment_paths: list[Path] | None = None,
) -> None:
    """Send optional MQTT/email status without changing the command exit code."""

    payload = build_notification_payload(
        job_name=config.name,
        command=command_name,
        state="success" if success else "failure",
        success=success,
        exit_code=exit_code,
        stderr_tail=stderr_tail,
        error=error,
        version=__version__,
    )

    mqtt_config = getattr(config, "mqtt", None)
    if mqtt_config and mqtt_config.enabled:
        if (success and mqtt_config.notify_on_success) or (not success and mqtt_config.notify_on_failure):
            try:
                publish_status(mqtt_config, payload)
            except Exception as mqtt_exc:
                print(f"WARNING: MQTT notification failed: {mqtt_exc}", file=sys.stderr)

    mail_config = getattr(config, "mail", None)
    if mail_config and mail_config.enabled:
        if (success and mail_config.notify_on_success) or (not success and mail_config.notify_on_failure):
            try:
                send_mail_status(mail_config, payload, attachments=attachment_paths)
            except Exception as mail_exc:
                print(f"WARNING: mail notification failed: {mail_exc}", file=sys.stderr)


def _mail_attachment_paths(logger) -> list[Path] | None:
    """Return current run log paths for optional email attachment."""

    if not logger:
        return None
    try:
        return logger.attachment_paths()
    except Exception:
        return None

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
            logger.info("Logging is active before command work begins")
        try:
            result = int(callback() or 0)
            _send_notifications(config, command_name, success=(result == 0), exit_code=result, attachment_paths=_mail_attachment_paths(logger))
            return result
        except KeyboardInterrupt as exc:
            if logger:
                logger.err("ERROR: Interrupted by user")
            stderr_tail = _stderr_tail_for_exception(exc, logger)
            _send_notifications(
                config,
                command_name,
                success=False,
                exit_code=130,
                error="Interrupted by user",
                stderr_tail=stderr_tail,
                attachment_paths=_mail_attachment_paths(logger),
            )
            raise
        except Exception as exc:
            if logger:
                logger.err(f"ERROR: {exc}")
            stderr_tail = _stderr_tail_for_exception(exc, logger)
            exit_code = _failure_exit_code(exc)
            _send_notifications(
                config,
                command_name,
                success=False,
                exit_code=exit_code,
                error=str(exc),
                stderr_tail=stderr_tail,
                attachment_paths=_mail_attachment_paths(logger),
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
    path.write_text(files("timeshift_btrfs_sync").joinpath("data/config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Wrote example config: {path}")
    return 0


def cmd_test_ssh(args) -> int:
    config = load_config(args.config)

    def _run() -> int:
        source = SourceRunner.from_config(config)
        if source.uses_ssh:
            source.test()
        else:
            print("Source mode: local; SSH test skipped.")
        source.run(timeshift.timeshift_cmd(config.source.sudo, config.source.timeshift_command, ["--list"]))
        source.run(btrfs.remote_btrfs_cmd(config.source.sudo, config.source.btrfs_command, ["--version"]))
        print("Source command endpoint works. Source sudo for timeshift/btrfs works.")
        return 0

    return _with_logging(config, "test-source", _run)



def _refresh_state_metadata_from_timeshift(config, state: dict, *, dry_run: bool) -> list[str]:
    """Refresh mutable state metadata from one fast Timeshift list read."""

    source = SourceRunner.from_config(config)
    print("Refreshing state metadata from source Timeshift --list...")
    source_by_name = source_snapshot_index(list_source_snapshots(config, source, include_btrfs_info=False))
    return refresh_state_metadata_and_report(state, source_by_name.values(), config.state_file, dry_run=dry_run)

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
                SourceRunner.from_config(config),
                include_btrfs_info=args.verify_btrfs,
            )
        )
        return 0

    return _with_logging(config, "list-source", _run)


def cmd_sync(args) -> int:
    config = load_config(args.config)

    def _run() -> int:
        dry_run = _resolve_dry_run(args, config)
        print(f"Run mode: {'dry-run' if dry_run else 'real run'}")
        if dry_run:
            print("Strict dry-run: no destination preparation, no lock file, no receive, and no prune deletion will be performed.")
            state = load_state(config.state_file, config.destination.target_root)
            sync_once(config, state, dry_run=True, limit=args.limit, only_snapshot=args.snapshot, only_missing=not args.resend)
            if args.prune or config.prune_after_sync:
                prune(config, state, dry_run=True, yes_delete=False)
        else:
            print(f"Acquiring lock: {config.lock_file}")
            with FileLock(config.lock_file):
                state = load_state(config.state_file, config.destination.target_root)
                sync_once(config, state, dry_run=False, limit=args.limit, only_snapshot=args.snapshot, only_missing=not args.resend)
                if args.prune or config.prune_after_sync:
                    prune(config, state, dry_run=False, yes_delete=args.yes_delete)
        return 0

    return _with_logging(config, "sync", _run)


def cmd_prune(args) -> int:
    config = load_config(args.config)

    def _run() -> int:
        dry_run = _resolve_dry_run(args, config)
        print(f"Run mode: {'dry-run' if dry_run else 'real run'}")
        if dry_run:
            print("Strict dry-run: no lock file and no destination deletion will be performed.")
            state = load_state(config.state_file, config.destination.target_root)
            _refresh_state_metadata_from_timeshift(config, state, dry_run=True)
            prune(config, state, dry_run=True, yes_delete=False)
        else:
            print(f"Acquiring lock: {config.lock_file}")
            with FileLock(config.lock_file):
                state = load_state(config.state_file, config.destination.target_root)
                _refresh_state_metadata_from_timeshift(config, state, dry_run=False)
                prune(config, state, dry_run=False, yes_delete=args.yes_delete)
        return 0

    return _with_logging(config, "prune", _run)


def cmd_create_manual(args) -> int:
    config = load_config(args.config)

    def _run() -> int:
        source = SourceRunner.from_config(config)
        if source.uses_ssh:
            source.test()
        else:
            print("Source mode: local; SSH setup/test skipped.")
        check_required_sync_paths(config, source, dry_run=False)
        confirm_source_identity_before_manual_snapshot(
            config,
            source,
            load_state(config.state_file, config.destination.target_root),
            load_source_index=lambda: source_snapshot_index(
                list_source_snapshots(config, source, include_btrfs_info=config.source.verify_subvolumes_at_discovery)
            ),
        )
        print()
        create_source_manual_snapshot(source, sudo=config.source.sudo, timeshift_command=config.source.timeshift_command, comment=args.comment)
        print("Requested source Timeshift on-demand snapshot.")
        return 0

    return _with_logging(config, "create-manual", _run)




def cmd_destroy_leftovers(args) -> int:
    """Destroy configured leftovers when this app setup is being retired."""

    config = load_config(args.config)
    dry_run = not args.run
    destroy_leftovers(
        config,
        delete_source=args.delete_source or args.delete_both,
        delete_destination=args.delete_destination or args.delete_both,
        dry_run=dry_run,
        danger_confirmed=args.i_understand_this_destroys_data,
    )
    return 0

def cmd_show_state(args) -> int:
    config = load_config(args.config)

    def _run() -> int:
        state = load_state(config.state_file, config.destination.target_root)
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


TOP_LEVEL_HELP = """
Available commands:
  init-config    Write a starter TOML config from the packaged template.
  test-source    Test source command endpoint and required source sudo commands.
  test-ssh       Backward-compatible alias for test-source.
  list-source    List source Timeshift snapshots.
  sync           Pull missing snapshots and optionally prune.
  prune          Apply destination retention rules only.
  create-manual      Create a source Timeshift on-demand snapshot.
  show-state         Show local state.json.
  destroy-leftovers  Permanently delete app-created source send-cache and/or destination leftovers.

Command-specific flags are shown by asking the command for help, for example:
  ts-btrfs sync --help
  ts-btrfs prune --help
  ts-btrfs init-config --help

Config options are documented in README.md and the packaged config.example.toml template.
Typical first test:
  ts-btrfs sync --config ./config.toml --dry-run
"""


def build_parser() -> argparse.ArgumentParser:
    """Create the argparse parser and command-specific flag help."""

    parser = argparse.ArgumentParser(
        prog="ts-btrfs",
        description="Pull or locally copy Timeshift Btrfs snapshots.",
        epilog=TOP_LEVEL_HELP,
        formatter_class=CLI_FORMATTER,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        title="commands",
        metavar="COMMAND",
        description="Run 'ts-btrfs COMMAND --help' to see that command's flags.",
    )

    p = new_subparser(sub, "init-config", "write an example TOML config", "Write a complete commented TOML config template.", cmd_init_config)
    p.add_argument("--path", default="./ts-btrfs.toml", help="where to write the example config; default: ./ts-btrfs.toml")
    p.add_argument("--force", action="store_true", help="overwrite the destination config file if it already exists")

    p = new_subparser(sub, "test-source", "test source endpoint and source sudo permissions", "Verify source mode works and source sudo can run timeshift --list and btrfs --version. In local mode, SSH is skipped.", cmd_test_ssh)
    add_config_arg(p)

    p = new_subparser(sub, "test-ssh", "alias for test-source", "Backward-compatible alias for test-source. In local mode, SSH is skipped.", cmd_test_ssh)
    add_config_arg(p)

    p = new_subparser(
        sub,
        "list-source",
        "list source Timeshift snapshots",
        (
            "List Timeshift snapshots found on the source.\n"
            "Default is fast mode: parse timeshift --list and construct expected paths.\n"
            "Use --verify-btrfs to run slower btrfs checks for every listed subvolume."
        ),
        cmd_list_source,
    )
    add_config_arg(p)
    p.add_argument(
        "--verify-btrfs",
        action="store_true",
        help="slow: verify every configured source subvolume with btrfs during listing",
    )

    p = new_subparser(
        sub,
        "sync",
        "pull missing snapshots",
        (
            "Pull missing Timeshift snapshot subvolumes from source to destination.\n"
            "Without --run or --dry-run, the config option default_dry_run decides.\n"
            "Real prune deletion still requires --yes-delete."
        ),
        cmd_sync,
    )
    add_config_arg(p)
    add_run_mode_args(p, dry_run_help="strict preview: no destination preparation, lock, receive, state write, manual snapshot, or delete", run_help="perform real send/receive work; required for actual changes")
    p.add_argument("--limit", type=int, help="transfer at most this many subvolumes; useful for first live test")
    p.add_argument("--snapshot", help="sync only this Timeshift snapshot name, for example 2026-06-23_07-10-24")
    p.add_argument("--resend", action="store_true", help="attempt transfer even if state.json says the subvolume was already synced")
    p.add_argument("--prune", action="store_true", help="run destination retention pruning after sync; real delete also needs --run --yes-delete")
    add_yes_delete_arg(p, "allow real pruning deletes when used with --run and --prune or prune_after_sync=true")

    p = new_subparser(
        sub,
        "prune",
        "apply destination retention rules",
        (
            "Apply retention rules to destination snapshots only.\n"
            "Use --dry-run first. Real deletion requires both --run and --yes-delete."
        ),
        cmd_prune,
    )
    add_config_arg(p)
    add_run_mode_args(p, dry_run_help="show what would be deleted; do not create a lock file, save state, or delete anything", run_help="perform real pruning if --yes-delete is also present")
    add_yes_delete_arg(p, "explicit safety confirmation required before real prune deletes")

    p = new_subparser(
        sub,
        "create-manual",
        "create source Timeshift tag O snapshot",
        (
            "Ask source Timeshift to create an on-demand/manual snapshot with tag O.\n"
            "Before creating it, verify source.snapshot_root, source.cache_root policy, "
            "and destination.target_root. If the destination already contains snapshots, the "
            "source must also match state.json by UUID first. If the destination is empty, "
            "first full seed creation is allowed."
        ),
        cmd_create_manual,
    )
    add_config_arg(p)
    p.add_argument("--comment", required=True, help="comment passed to timeshift --create --comments")

    p = new_subparser(
        sub,
        "destroy-leftovers",
        "destroy app-created leftovers",
        (
            "Permanently delete app-created source send-cache and/or destination leftover trees.\n"
            "This ignores state.json and retention rules. Dry-run is the default.\n"
            "Real deletion requires --run, --i-understand-this-destroys-data, and two typed confirmations."
        ),
        cmd_destroy_leftovers,
    )
    add_config_arg(p)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--delete-source", action="store_true", help="destroy source.cache_root only; never destroys source.snapshot_root")
    target.add_argument("--delete-destination", action="store_true", help="destroy destination.target_root")
    target.add_argument("--delete-both", action="store_true", help="destroy source.cache_root and destination.target_root; never destroys source.snapshot_root")
    add_run_mode_args(
        p,
        dry_run_help="show the destructive cleanup plan; do not delete anything",
        run_help="perform real destructive cleanup; also requires --i-understand-this-destroys-data and typed confirmations",
    )
    p.add_argument(
        "--i-understand-this-destroys-data",
        action="store_true",
        help="required with --run; confirms you understand this recursively destroys configured paths",
    )

    p = new_subparser(sub, "show-state", "show local sync state", "Show state.json, which records completed transfers and incremental parent metadata.", cmd_show_state)
    add_config_arg(p)
    p.add_argument("--json", action="store_true", help="print raw state.json instead of a short table")
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
