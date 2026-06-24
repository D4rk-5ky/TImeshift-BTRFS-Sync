"""TOML configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import tomllib

from .ssh import SSHConfig


@dataclass(slots=True)
class SourceConfig:
    """Remote/source Timeshift and Btrfs settings."""

    snapshot_root: str
    subvolumes: list[str] = field(default_factory=lambda: ["@", "@home"])
    sudo: str = "sudo -n"
    btrfs_command: str = "btrfs"
    timeshift_command: str = "timeshift"
    cache_root: str | None = None
    create_readonly_cache: bool = True

    # Cleanup option. When true, temporary source-side cache snapshots are
    # deleted after they are superseded by a newer successful send. The newest
    # cache snapshot per subvolume is kept because it is needed as the parent
    # for the next incremental send, including the next program run.
    cleanup_superseded_cache: bool = True

    # Speed option. False means discovery does not run btrfs property/show for
    # every snapshot. The app assumes configured subvolume names exist and only
    # runs Btrfs checks for snapshots that are actually going to be sent.
    verify_subvolumes_at_discovery: bool = False

    # Safety option. When true, an incremental parent is verified by comparing
    # source Btrfs UUID metadata with local destination received_uuid metadata
    # before using `btrfs send -p`. This prevents accidentally using snapshots
    # from another OS/source as parents while keeping discovery fast.
    verify_incremental_parent: bool = True

    # Performance/safety balance. When true, the app verifies the first
    # incremental parent for each subvolume name during a run, then trusts the
    # Btrfs incremental chain for later snapshots in the same run. This avoids
    # repeated source/destination UUID metadata checks for every incremental
    # send while still preventing the initial "wrong OS/source" mistake.
    verify_incremental_parent_once_per_run: bool = True

    # Safety option. When false, the app refuses incremental send if existing
    # destination snapshots cannot be proven to match the current source.
    allow_incremental_without_parent_match: bool = False

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

    compression: str | None = None
    set_compression_before_receive: bool = True
    set_compression_after_receive: bool = True


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
    yearly: int = 0
    keep_latest: bool = True
    keep_latest_common_parent: bool = True
    protected_snapshots: list[str] = field(default_factory=list)

    def counts_by_tag(self) -> dict[str, int]:
        """Return retention counts keyed by Timeshift tag letters."""

        return {"H": self.hourly, "D": self.daily, "W": self.weekly, "M": self.monthly, "B": self.boot, "O": self.ondemand, "Y": self.yearly}


@dataclass(slots=True)
class AppConfig:
    """Complete validated app configuration."""

    name: str
    ssh: SSHConfig
    source: SourceConfig
    destination: DestinationConfig
    stream: StreamConfig
    retention: RetentionConfig
    state_file: Path
    lock_file: Path
    log_dir: Path | None
    default_dry_run: bool = True
    prune_after_sync: bool = False


class ConfigError(ValueError):
    """Raised when the TOML config is invalid."""


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


def _compression_value(value: Any) -> str | None:
    """Normalize destination compression property value.

    btrfs property supports algorithm names, not levels. zstd:3 is accepted in
    config for convenience but normalized to zstd.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("destination.compression must be a string")
    normalized = value.strip().lower()
    if not normalized or normalized in {"off", "false"}:
        return None
    if ":" in normalized:
        normalized = normalized.split(":", 1)[0]
    aliases = {"no": "none", "disabled": "none"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"zstd", "lzo", "zlib", "none"}:
        raise ConfigError("destination.compression must be zstd, lzo, zlib, none, or blank")
    return normalized


def load_config(path: str | Path) -> AppConfig:
    """Read and validate TOML config."""

    path = Path(path).expanduser()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    if not isinstance(raw, dict):
        raise ConfigError("Config must be a TOML table")

    name = str(raw.get("name") or "timeshift-btrfs-sync")

    ssh_raw = raw.get("ssh", {})
    if not isinstance(ssh_raw, dict):
        raise ConfigError("[ssh] must be a TOML table")
    port = ssh_raw.get("port")
    if port is not None and (not isinstance(port, int) or port <= 0):
        raise ConfigError("ssh.port must be a positive integer")
    password = ssh_raw.get("password") if isinstance(ssh_raw.get("password"), str) and ssh_raw.get("password") else None
    password_file = ssh_raw.get("password_file") if isinstance(ssh_raw.get("password_file"), str) and ssh_raw.get("password_file") else None
    if password and password_file:
        raise ConfigError("Use either ssh.password or ssh.password_file, not both")
    if password_file and not Path(password_file).expanduser().is_file():
        raise ConfigError(f"ssh.password_file does not exist or is not a file: {password_file}")
    extra_args = _string_list(ssh_raw.get("extra_args"), "ssh.extra_args")
    if (password or password_file) and any("BatchMode=yes" in arg for arg in extra_args):
        raise ConfigError("ssh.password/password_file cannot be used with BatchMode=yes; remove that SSH option")
    ssh = SSHConfig(
        host=_as_str(ssh_raw.get("host"), "ssh.host"),
        user=ssh_raw.get("user") if isinstance(ssh_raw.get("user"), str) and ssh_raw.get("user") else None,
        port=port,
        identity_file=ssh_raw.get("identity_file") if isinstance(ssh_raw.get("identity_file"), str) and ssh_raw.get("identity_file") else None,
        password=password,
        password_file=password_file,
        compression=_as_bool(ssh_raw.get("compression"), "ssh.compression", False),
        cipher=ssh_raw.get("cipher") if isinstance(ssh_raw.get("cipher"), str) and ssh_raw.get("cipher") else None,
        extra_args=extra_args,
    )

    source_raw = raw.get("source", {})
    if not isinstance(source_raw, dict):
        raise ConfigError("[source] must be a TOML table")
    source = SourceConfig(
        snapshot_root=_as_str(source_raw.get("snapshot_root"), "source.snapshot_root").rstrip("/"),
        subvolumes=_string_list(source_raw.get("subvolumes", ["@", "@home"]), "source.subvolumes") or ["@", "@home"],
        sudo=str(source_raw.get("sudo", "sudo -n")),
        btrfs_command=str(source_raw.get("btrfs_command", "btrfs")),
        timeshift_command=str(source_raw.get("timeshift_command", "timeshift")),
        cache_root=(str(source_raw.get("cache_root")) if source_raw.get("cache_root") else None),
        create_readonly_cache=_as_bool(source_raw.get("create_readonly_cache"), "source.create_readonly_cache", True),
        cleanup_superseded_cache=_as_bool(source_raw.get("cleanup_superseded_cache"), "source.cleanup_superseded_cache", True),
        verify_subvolumes_at_discovery=_as_bool(source_raw.get("verify_subvolumes_at_discovery"), "source.verify_subvolumes_at_discovery", False),
        verify_incremental_parent=_as_bool(source_raw.get("verify_incremental_parent"), "source.verify_incremental_parent", True),
        verify_incremental_parent_once_per_run=_as_bool(source_raw.get("verify_incremental_parent_once_per_run"), "source.verify_incremental_parent_once_per_run", True),
        allow_incremental_without_parent_match=_as_bool(source_raw.get("allow_incremental_without_parent_match"), "source.allow_incremental_without_parent_match", False),
        send_compressed_data=_as_bool(source_raw.get("send_compressed_data"), "source.send_compressed_data", False),
        send_proto=_as_int(source_raw.get("send_proto"), "source.send_proto", None),
    )

    destination_raw = raw.get("destination", {})
    if not isinstance(destination_raw, dict):
        raise ConfigError("[destination] must be a TOML table")
    target_root = _as_path(destination_raw.get("target_root"), "destination.target_root")
    destination = DestinationConfig(
        target_root=target_root,
        sudo=str(destination_raw.get("sudo", "sudo -n")),
        btrfs_command=str(destination_raw.get("btrfs_command", "btrfs")),
        create_target_root=_as_bool(destination_raw.get("create_target_root"), "destination.create_target_root", True),
        cleanup_incomplete_receive=_as_bool(destination_raw.get("cleanup_incomplete_receive"), "destination.cleanup_incomplete_receive", True),
        compression=_compression_value(destination_raw.get("compression")),
        set_compression_before_receive=_as_bool(destination_raw.get("set_compression_before_receive"), "destination.set_compression_before_receive", True),
        set_compression_after_receive=_as_bool(destination_raw.get("set_compression_after_receive"), "destination.set_compression_after_receive", True),
    )

    stream_raw = raw.get("stream", {})
    if not isinstance(stream_raw, dict):
        raise ConfigError("[stream] must be a TOML table")
    stream = StreamConfig(
        use_mbuffer=_as_bool(stream_raw.get("use_mbuffer"), "stream.use_mbuffer", False),
        mbuffer_command=str(stream_raw.get("mbuffer_command", "mbuffer")),
        mbuffer_size=str(stream_raw.get("mbuffer_size", "256M")),
        mbuffer_rate=(str(stream_raw.get("mbuffer_rate")) if stream_raw.get("mbuffer_rate") else None),
        mbuffer_extra_args=_string_list(stream_raw.get("mbuffer_extra_args"), "stream.mbuffer_extra_args"),
        btrfs_verbose=_as_bool(stream_raw.get("btrfs_verbose"), "stream.btrfs_verbose", False),
    )

    retention_raw = raw.get("retention", {})
    if not isinstance(retention_raw, dict):
        raise ConfigError("[retention] must be a TOML table")
    retention = RetentionConfig(
        hourly=int(_as_int(retention_raw.get("hourly"), "retention.hourly", 6)),
        daily=int(_as_int(retention_raw.get("daily"), "retention.daily", 7)),
        weekly=int(_as_int(retention_raw.get("weekly"), "retention.weekly", 4)),
        monthly=int(_as_int(retention_raw.get("monthly"), "retention.monthly", 6)),
        boot=int(_as_int(retention_raw.get("boot"), "retention.boot", 5)),
        ondemand=int(_as_int(retention_raw.get("ondemand"), "retention.ondemand", 10)),
        yearly=int(_as_int(retention_raw.get("yearly"), "retention.yearly", 0)),
        keep_latest=_as_bool(retention_raw.get("keep_latest"), "retention.keep_latest", True),
        keep_latest_common_parent=_as_bool(retention_raw.get("keep_latest_common_parent"), "retention.keep_latest_common_parent", True),
        protected_snapshots=_string_list(retention_raw.get("protected_snapshots"), "retention.protected_snapshots"),
    )

    state_file = _as_path(raw.get("state_file", str(target_root / ".ts-btrfs-sync" / "state.json")), "state_file")
    lock_file = _as_path(raw.get("lock_file", str(target_root / ".ts-btrfs-sync" / "lock")), "lock_file")

    # File logging is optional. If top-level log_dir is missing or blank, the app
    # only prints to the terminal. If log_dir is set, log.py creates timestamped
    # .log/.mbuffer/.btrfs-out/.err files in that directory.
    raw_log_dir = raw.get("log_dir")
    log_dir = Path(str(raw_log_dir)).expanduser() if isinstance(raw_log_dir, str) and raw_log_dir.strip() else None

    return AppConfig(
        name=name,
        ssh=ssh,
        source=source,
        destination=destination,
        stream=stream,
        retention=retention,
        state_file=state_file,
        lock_file=lock_file,
        log_dir=log_dir,
        default_dry_run=_as_bool(raw.get("default_dry_run"), "default_dry_run", True),
        prune_after_sync=_as_bool(raw.get("prune_after_sync"), "prune_after_sync", False),
    )
