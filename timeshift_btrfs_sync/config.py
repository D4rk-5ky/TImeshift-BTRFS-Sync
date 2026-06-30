"""TOML configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import posixpath
from typing import Any
import tomllib

from .ssh import SSHConfig, validate_control_path_safety
from .mqtt import MQTTConfig
from .mail import MailConfig

@dataclass(slots=True)
class ManualSnapshotConfig:
    """Optional source-side Timeshift on-demand snapshot creation and cleanup."""

    # When true, `sync` asks source Timeshift to create a tag O snapshot before
    # it reads the source snapshot list. Dry-run only prints what would happen.
    enabled: bool = False

    # Independent prune switch for on-demand snapshots created by this app.
    # Matching is based on the configured marker in the saved Timeshift comment.
    # Even when true, real deletion still requires prune to run and --yes-delete.
    cleanup_enabled: bool = True

    # Source identity checking is not configurable. If the destination already
    # contains snapshots, manual snapshot creation requires a UUID-confirmed
    # source/destination anchor. If the destination is empty, a first full seed
    # is allowed.

    # Comment passed to `timeshift --create --comments`. The comment should
    # contain marker so later retention can recognize snapshots created by this
    # app, even though they are normal Timeshift on-demand snapshots.
    comment: str = "ts-btrfs-sync automatic on-demand snapshot"

    # Marker used when applying the app-created on-demand retention rule to
    # destination state entries. Matching is case-insensitive.
    marker: str = "ts-btrfs-sync"

    # Destination retention for app-created on-demand snapshots. This is separate
    # from retention.ondemand, which applies only to normal/user-created
    # Timeshift O snapshots when retention.cleanup_ondemand = true.
    # Set to 0 to delete all matching app-created snapshots except globally
    # protected/newest snapshots. Disable cleanup_enabled to keep them all.
    retention_count: int = 10

@dataclass(slots=True)
class SourceConfig:
    """Source Timeshift and Btrfs settings."""

    # Timeshift-owned snapshot directory. It may be an ordinary directory on a
    # Btrfs filesystem, but the app must never create, prune, delete, destroy,
    # or clean this path or anything below it.
    snapshot_root: str
    mode: str = "ssh"
    subvolumes: list[str] = field(default_factory=lambda: ["@", "@home"])
    sudo: str = "sudo -n"
    btrfs_command: str = "btrfs"
    timeshift_command: str = "timeshift"
    cache_root: str | None = None
    create_readonly_cache: bool = True

    # Backward-compatible option name. When true, source-side read-only cache
    # snapshots are cleaned only during prune, and only for snapshots that the
    # destination retention plan deletes. Sync itself keeps every cache snapshot
    # it creates so short-lived hourly parents do not erase common UUID ground.
    cleanup_superseded_cache: bool = True

    # Speed option. False means discovery does not run btrfs subvolume show for
    # every snapshot. The app assumes configured subvolume names exist and only
    # runs Btrfs checks for snapshots that are actually going to be sent.
    verify_subvolumes_at_discovery: bool = False

    # Performance/safety balance for the current run only. Parent paths from
    # previous runs are always checked against destination received_uuid. When
    # true, a source send_path that was just successfully sent/received by this
    # process can be reused as the next parent without re-reading metadata.
    verify_incremental_parent_once_per_run: bool = True

    send_compressed_data: bool = False
    send_proto: int | None = None

@dataclass(slots=True)
class DestinationConfig:
    """Local/destination receive settings."""

    target_root: Path
    sudo: str = "sudo -n"
    btrfs_command: str = "btrfs"
    create_target_root: bool = True

    # If a previous transfer was interrupted, btrfs receive can leave a partial
    # destination subvolume that is not recorded in state.json. When this is
    # true, the app deletes that incomplete destination subvolume and retries.
    cleanup_incomplete_receive: bool = True

@dataclass(slots=True)
class StreamConfig:
    """Optional pipeline display/buffering settings.

    mbuffer is the best progress display because it shows throughput, total
    transferred data, elapsed time, and buffer fill. Btrfs itself has verbose
    flags, but those print operation/details, not a clean percentage progress
    bar.
    """

    use_mbuffer: bool = False
    mbuffer_command: str = "mbuffer"
    mbuffer_size: str = "256M"
    mbuffer_rate: str | None = None
    mbuffer_extra_args: list[str] = field(default_factory=list)

    # When true, add -v to both btrfs send and btrfs receive and let their
    # stderr/stdout text pass through to the terminal during the transfer.
    # This is not byte progress; it is Btrfs operation verbosity.
    btrfs_verbose: bool = False

    def command(self) -> list[str] | None:
        """Return mbuffer command argv or None when disabled."""

        if not self.use_mbuffer:
            return None
        cmd = [self.mbuffer_command]
        if self.mbuffer_size:
            cmd += ["-m", self.mbuffer_size]
        if self.mbuffer_rate:
            cmd += ["-R", self.mbuffer_rate]
        cmd += self.mbuffer_extra_args
        return cmd

@dataclass(slots=True)
class RetentionConfig:
    """Destination retention counts by Timeshift tag."""

    hourly: int = 6
    daily: int = 7
    weekly: int = 4
    monthly: int = 6
    boot: int = 5
    ondemand: int = 10
    # Independent prune switch for normal/user-created Timeshift tag O snapshots.
    # False is the safest default: user manual snapshots are kept unless this is
    # explicitly enabled. App-created O snapshots are controlled separately by
    # manual_snapshot.cleanup_enabled.
    cleanup_ondemand: bool = False

    keep_latest: bool = True
    keep_latest_common_parent: bool = True
    protected_snapshots: list[str] = field(default_factory=list)

    def counts_by_tag(self) -> dict[str, int]:
        """Return retention counts keyed by Timeshift tag letters."""

        return {"H": self.hourly, "D": self.daily, "W": self.weekly, "M": self.monthly, "B": self.boot, "O": self.ondemand}

@dataclass(slots=True)
class AppConfig:
    """Complete validated app configuration."""

    name: str
    ssh: SSHConfig
    source: SourceConfig
    destination: DestinationConfig
    stream: StreamConfig
    retention: RetentionConfig
    mqtt: MQTTConfig
    mail: MailConfig
    manual_snapshot: ManualSnapshotConfig
    state_file: Path
    lock_file: Path
    log_dir: Path | None
    default_dry_run: bool = True
    prune_after_sync: bool = False

class ConfigError(ValueError):
    """Raised when the TOML config is invalid."""

def _table(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value

def _optional_str(table: dict[str, Any], key: str) -> str | None:
    return table.get(key) if isinstance(table.get(key), str) and table.get(key) else None

def _positive_int(value: Any, field_name: str, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, int) and value > 0:
        return value
    raise ConfigError(f"{field_name} must be a positive integer")

def _stripped(table: dict[str, Any], key: str, default: str = "") -> str:
    return str(table.get(key, default)).strip()

def _bool(table: dict[str, Any], section: str, key: str, default: bool) -> bool:
    return _as_bool(table.get(key), f"{section}.{key}", default)

def _int(table: dict[str, Any], section: str, key: str, default: int | None) -> int | None:
    return _as_int(table.get(key), f"{section}.{key}", default)

def _as_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field_name} must be a non-empty string")
    return value

def _as_path(value: Any, field_name: str) -> Path:
    return Path(_as_str(value, field_name)).expanduser()

def _as_bool(value: Any, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{field_name} must be true or false")
    return value

def _as_int(value: Any, field_name: str, default: int | None) -> int | None:
    if value is None:
        return default
    if not isinstance(value, int) or value < 0:
        raise ConfigError(f"{field_name} must be a non-negative integer")
    return value

def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{field_name} must be a list of non-empty strings")
    return value


def _normalize_source_path(value: str) -> str:
    """Return a normalized POSIX-style source path without a trailing slash."""

    text = str(value).strip()
    if not text:
        return ""
    return posixpath.normpath(text).rstrip("/") or "/"

def _source_path_is_same_or_under(path: str, root: str) -> bool:
    """Return True when a source path is the root itself or below it."""

    normalized_path = _normalize_source_path(path)
    normalized_root = _normalize_source_path(root)
    return normalized_path == normalized_root or normalized_path.startswith(normalized_root.rstrip("/") + "/")

def load_config(path: str | Path) -> AppConfig:
    """Read and validate TOML config."""

    path = Path(path).expanduser()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    if not isinstance(raw, dict):
        raise ConfigError("Config must be a TOML table")

    name = str(raw.get("name") or "timeshift-btrfs-sync")

    source_raw = _table(raw, "source")
    source_mode = _stripped(source_raw, "mode", "ssh").lower()
    if source_mode not in {"ssh", "local"}:
        raise ConfigError("source.mode must be either 'ssh' or 'local'")

    ssh_raw = _table(raw, "ssh")
    if source_mode == "ssh":
        port = _positive_int(ssh_raw.get("port"), "ssh.port")
        password = _optional_str(ssh_raw, "password")
        password_file = _optional_str(ssh_raw, "password_file")
        if password and password_file:
            raise ConfigError("Use either ssh.password or ssh.password_file, not both")
        if password_file and not Path(password_file).expanduser().is_file():
            raise ConfigError(f"ssh.password_file does not exist or is not a file: {password_file}")
        extra_args = _string_list(ssh_raw.get("extra_args"), "ssh.extra_args")
        if (password or password_file) and any("BatchMode=yes" in arg for arg in extra_args):
            raise ConfigError("ssh.password/password_file cannot be used with BatchMode=yes; remove that SSH option")
        control_master = _bool(ssh_raw, "ssh", "control_master", False)
        control_persist = _optional_str(ssh_raw, "control_persist")
        control_path = _optional_str(ssh_raw, "control_path")
        if control_master:
            try:
                validate_control_path_safety(control_path)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc

        ssh = SSHConfig(
            host=_as_str(ssh_raw.get("host"), "ssh.host"),
            user=_optional_str(ssh_raw, "user"),
            port=port,
            identity_file=_optional_str(ssh_raw, "identity_file"),
            password=password,
            password_file=password_file,
            compression=_bool(ssh_raw, "ssh", "compression", False),
            cipher=_optional_str(ssh_raw, "cipher"),
            control_master=control_master,
            control_persist=control_persist,
            control_path=control_path,
            extra_args=extra_args,
        )
    else:
        # Local source mode never constructs SSH commands. Keep a placeholder so
        # AppConfig remains backward-compatible for callers that inspect config.ssh.
        ssh = SSHConfig(host="")

    snapshot_root = _normalize_source_path(_as_str(source_raw.get("snapshot_root"), "source.snapshot_root"))
    cache_root_raw = source_raw.get("cache_root")
    cache_root = _normalize_source_path(str(cache_root_raw)) if cache_root_raw else None
    if cache_root and _source_path_is_same_or_under(cache_root, snapshot_root):
        raise ConfigError(
            "source.cache_root must be outside source.snapshot_root. "
            "source.snapshot_root is Timeshift-owned and may be an ordinary directory, but "
            "source.cache_root is app-owned send-cache storage. Use a separate path such as "
            "<timeshift-root>/.ts-btrfs-sync/send-cache, not the snapshots directory itself."
        )

    source = SourceConfig(
        snapshot_root=snapshot_root,
        mode=source_mode,
        subvolumes=_string_list(source_raw.get("subvolumes", ["@", "@home"]), "source.subvolumes") or ["@", "@home"],
        sudo=str(source_raw.get("sudo", "sudo -n")),
        btrfs_command=str(source_raw.get("btrfs_command", "btrfs")),
        timeshift_command=str(source_raw.get("timeshift_command", "timeshift")),
        cache_root=cache_root,
        create_readonly_cache=_bool(source_raw, "source", "create_readonly_cache", True),
        cleanup_superseded_cache=_bool(source_raw, "source", "cleanup_superseded_cache", True),
        verify_subvolumes_at_discovery=_bool(source_raw, "source", "verify_subvolumes_at_discovery", False),
        verify_incremental_parent_once_per_run=_bool(source_raw, "source", "verify_incremental_parent_once_per_run", True),
        send_compressed_data=_bool(source_raw, "source", "send_compressed_data", False),
        send_proto=_int(source_raw, "source", "send_proto", None),
    )

    destination_raw = _table(raw, "destination")
    target_root = _as_path(destination_raw.get("target_root"), "destination.target_root")
    destination = DestinationConfig(
        target_root=target_root,
        sudo=str(destination_raw.get("sudo", "sudo -n")),
        btrfs_command=str(destination_raw.get("btrfs_command", "btrfs")),
        create_target_root=_bool(destination_raw, "destination", "create_target_root", True),
        cleanup_incomplete_receive=_bool(destination_raw, "destination", "cleanup_incomplete_receive", True),
    )

    stream_raw = _table(raw, "stream")
    stream = StreamConfig(
        use_mbuffer=_bool(stream_raw, "stream", "use_mbuffer", False),
        mbuffer_command=str(stream_raw.get("mbuffer_command", "mbuffer")),
        mbuffer_size=str(stream_raw.get("mbuffer_size", "256M")),
        mbuffer_rate=(str(stream_raw.get("mbuffer_rate")) if stream_raw.get("mbuffer_rate") else None),
        mbuffer_extra_args=_string_list(stream_raw.get("mbuffer_extra_args"), "stream.mbuffer_extra_args"),
        btrfs_verbose=_bool(stream_raw, "stream", "btrfs_verbose", False),
    )

    retention_raw = _table(raw, "retention")
    retention = RetentionConfig(
        hourly=int(_int(retention_raw, "retention", "hourly", 6)),
        daily=int(_int(retention_raw, "retention", "daily", 7)),
        weekly=int(_int(retention_raw, "retention", "weekly", 4)),
        monthly=int(_int(retention_raw, "retention", "monthly", 6)),
        boot=int(_int(retention_raw, "retention", "boot", 5)),
        ondemand=int(_int(retention_raw, "retention", "ondemand", 10)),
        cleanup_ondemand=_bool(retention_raw, "retention", "cleanup_ondemand", False),
        keep_latest=_bool(retention_raw, "retention", "keep_latest", True),
        keep_latest_common_parent=_bool(retention_raw, "retention", "keep_latest_common_parent", True),
        protected_snapshots=_string_list(retention_raw.get("protected_snapshots"), "retention.protected_snapshots"),
    )

    manual_raw = _table(raw, "manual_snapshot")
    manual_comment = _stripped(manual_raw, "comment", "ts-btrfs-sync automatic on-demand snapshot")
    manual_marker = _stripped(manual_raw, "marker", "ts-btrfs-sync")
    manual_enabled = _bool(manual_raw, "manual_snapshot", "enabled", False)
    if manual_enabled and not manual_comment:
        raise ConfigError("manual_snapshot.comment must be non-empty when manual_snapshot.enabled = true")
    if manual_enabled and not manual_marker:
        raise ConfigError("manual_snapshot.marker must be non-empty when manual_snapshot.enabled = true")
    manual_snapshot = ManualSnapshotConfig(
        enabled=manual_enabled,
        comment=manual_comment or "ts-btrfs-sync automatic on-demand snapshot",
        marker=manual_marker or "ts-btrfs-sync",
        cleanup_enabled=_bool(manual_raw, "manual_snapshot", "cleanup_enabled", True),
        retention_count=int(_int(manual_raw, "manual_snapshot", "retention_count", 10)),
    )

    mqtt_raw = _table(raw, "mqtt")
    mqtt_port = _positive_int(mqtt_raw.get("port"), "mqtt.port", 1883)
    mqtt_qos = mqtt_raw.get("qos", 0)
    if not isinstance(mqtt_qos, int) or mqtt_qos not in {0, 1, 2}:
        raise ConfigError("mqtt.qos must be 0, 1, or 2")
    mqtt_timeout = _positive_int(mqtt_raw.get("timeout"), "mqtt.timeout", 10)
    mqtt_password = _optional_str(mqtt_raw, "password")
    mqtt_password_file = _optional_str(mqtt_raw, "password_file")
    if mqtt_password and mqtt_password_file:
        raise ConfigError("Use either mqtt.password or mqtt.password_file, not both")
    if mqtt_password_file and not Path(mqtt_password_file).expanduser().is_file():
        raise ConfigError(f"mqtt.password_file does not exist or is not a file: {mqtt_password_file}")
    mqtt_enabled = _bool(mqtt_raw, "mqtt", "enabled", False)
    mqtt_host = _stripped(mqtt_raw, "host")
    mqtt_topic = _stripped(mqtt_raw, "topic", "timeshift-btrfs-sync/status")
    if mqtt_enabled and not mqtt_host:
        raise ConfigError("mqtt.host is required when mqtt.enabled = true")
    if mqtt_enabled and not mqtt_topic:
        raise ConfigError("mqtt.topic is required when mqtt.enabled = true")
    mqtt = MQTTConfig(
        enabled=mqtt_enabled,
        host=mqtt_host,
        port=mqtt_port,
        topic=mqtt_topic,
        username=_optional_str(mqtt_raw, "username"),
        password=mqtt_password,
        password_file=str(Path(mqtt_password_file).expanduser()) if mqtt_password_file else None,
        client_id=_optional_str(mqtt_raw, "client_id"),
        qos=mqtt_qos,
        retain=_bool(mqtt_raw, "mqtt", "retain", False),
        timeout=mqtt_timeout,
        notify_on_success=_bool(mqtt_raw, "mqtt", "notify_on_success", True),
        notify_on_failure=_bool(mqtt_raw, "mqtt", "notify_on_failure", True),
    )

    mail_raw = _table(raw, "mail")
    mail_port = _positive_int(mail_raw.get("smtp_port"), "mail.smtp_port", 587)
    mail_timeout = _positive_int(mail_raw.get("timeout"), "mail.timeout", 10)
    mail_max_attachment_bytes = mail_raw.get("max_attachment_bytes", 0)
    if not isinstance(mail_max_attachment_bytes, int) or mail_max_attachment_bytes < 0:
        raise ConfigError("mail.max_attachment_bytes must be a non-negative integer")
    mail_password = _optional_str(mail_raw, "password")
    mail_password_file = _optional_str(mail_raw, "password_file")
    if mail_password and mail_password_file:
        raise ConfigError("Use either mail.password or mail.password_file, not both")
    if mail_password_file and not Path(mail_password_file).expanduser().is_file():
        raise ConfigError(f"mail.password_file does not exist or is not a file: {mail_password_file}")
    mail_enabled = _bool(mail_raw, "mail", "enabled", False)
    mail_smtp_host = _stripped(mail_raw, "smtp_host")
    mail_from_addr = _stripped(mail_raw, "from_addr")
    mail_to_addrs = _string_list(mail_raw.get("to_addrs"), "mail.to_addrs")
    mail_smtp_ssl = _bool(mail_raw, "mail", "smtp_ssl", False)
    mail_starttls = _bool(mail_raw, "mail", "starttls", True)
    if mail_smtp_ssl and mail_starttls:
        raise ConfigError("mail.smtp_ssl and mail.starttls cannot both be true")
    if mail_enabled and not mail_smtp_host:
        raise ConfigError("mail.smtp_host is required when mail.enabled = true")
    if mail_enabled and not mail_from_addr:
        raise ConfigError("mail.from_addr is required when mail.enabled = true")
    if mail_enabled and not mail_to_addrs:
        raise ConfigError("mail.to_addrs must contain at least one address when mail.enabled = true")
    mail = MailConfig(
        enabled=mail_enabled,
        smtp_host=mail_smtp_host,
        smtp_port=mail_port,
        smtp_ssl=mail_smtp_ssl,
        starttls=mail_starttls,
        username=_optional_str(mail_raw, "username"),
        password=mail_password,
        password_file=str(Path(mail_password_file).expanduser()) if mail_password_file else None,
        from_addr=mail_from_addr,
        to_addrs=mail_to_addrs,
        subject_prefix=_stripped(mail_raw, "subject_prefix", "[timeshift-btrfs-sync]"),
        timeout=mail_timeout,
        notify_on_success=_bool(mail_raw, "mail", "notify_on_success", True),
        notify_on_failure=_bool(mail_raw, "mail", "notify_on_failure", True),
        include_json=_bool(mail_raw, "mail", "include_json", True),
        attach_logs=_bool(mail_raw, "mail", "attach_logs", True),
        max_attachment_bytes=mail_max_attachment_bytes,
    )

    state_file = _as_path(raw.get("state_file", str(target_root / ".ts-btrfs-sync" / "state.json")), "state_file")
    lock_file = _as_path(raw.get("lock_file", str(target_root / ".ts-btrfs-sync" / "lock")), "lock_file")

    # File logging is optional. If top-level log_dir is missing or blank, the app
    # only prints to the terminal. If log_dir is set, log.py creates timestamped
    # .log/.err/.btrfs/.mbuffer/.succes files in that directory.
    raw_log_dir = raw.get("log_dir")
    log_dir = Path(str(raw_log_dir)).expanduser() if isinstance(raw_log_dir, str) and raw_log_dir.strip() else None

    return AppConfig(
        name=name,
        ssh=ssh,
        source=source,
        destination=destination,
        stream=stream,
        retention=retention,
        mqtt=mqtt,
        mail=mail,
        manual_snapshot=manual_snapshot,
        state_file=state_file,
        lock_file=lock_file,
        log_dir=log_dir,
        default_dry_run=_as_bool(raw.get("default_dry_run"), "default_dry_run", True),
        prune_after_sync=_as_bool(raw.get("prune_after_sync"), "prune_after_sync", False),
    )
