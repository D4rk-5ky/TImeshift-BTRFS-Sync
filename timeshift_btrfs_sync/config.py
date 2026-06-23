"""TOML configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import tomllib

from .ssh import SSHConfig


@dataclass(slots=True)
class SourceConfig:
    """Remote/source settings.

    v0.3.0 deliberately requires no source-side helper/script. The only source
    commands that need passwordless sudo are `btrfs` and `timeshift`.
    """

    snapshot_root: str
    subvolumes: list[str] = field(default_factory=lambda: ["@", "@home"])
    sudo: str = "sudo -n"
    btrfs_command: str = "btrfs"
    timeshift_command: str = "timeshift"

    # Optional source-side read-only cache root. It must be pre-created manually
    # by the admin. The app will not run source-side mkdir.
    cache_root: str | None = None
    create_readonly_cache: bool = True


@dataclass(slots=True)
class DestinationConfig:
    """Local/destination settings."""

    target_root: Path
    sudo: str = "sudo -n"
    create_target_root: bool = True


@dataclass(slots=True)
class RetentionConfig:
    """Retention counts by Timeshift tag."""

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
        return {
            "H": self.hourly,
            "D": self.daily,
            "W": self.weekly,
            "M": self.monthly,
            "B": self.boot,
            "O": self.ondemand,
            "Y": self.yearly,
        }


@dataclass(slots=True)
class AppConfig:
    """Complete validated config."""

    name: str
    ssh: SSHConfig
    source: SourceConfig
    destination: DestinationConfig
    retention: RetentionConfig
    state_file: Path
    lock_file: Path
    log_dir: Path
    default_dry_run: bool = True
    prune_after_sync: bool = False


class ConfigError(ValueError):
    """Raised when the config is invalid."""


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


def _as_int(value: Any, field_name: str, default: int) -> int:
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


def load_config(path: str | Path) -> AppConfig:
    """Load and validate a TOML config file."""

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
    ssh = SSHConfig(
        host=_as_str(ssh_raw.get("host"), "ssh.host"),
        user=ssh_raw.get("user") if isinstance(ssh_raw.get("user"), str) and ssh_raw.get("user") else None,
        port=port,
        identity_file=ssh_raw.get("identity_file") if isinstance(ssh_raw.get("identity_file"), str) and ssh_raw.get("identity_file") else None,
        extra_args=_string_list(ssh_raw.get("extra_args"), "ssh.extra_args"),
    )

    source_raw = raw.get("source", {})
    if not isinstance(source_raw, dict):
        raise ConfigError("[source] must be a TOML table")
    snapshot_root = _as_str(source_raw.get("snapshot_root"), "source.snapshot_root").rstrip("/")
    source = SourceConfig(
        snapshot_root=snapshot_root,
        subvolumes=_string_list(source_raw.get("subvolumes", ["@", "@home"]), "source.subvolumes") or ["@", "@home"],
        sudo=str(source_raw.get("sudo", "sudo -n")),
        btrfs_command=str(source_raw.get("btrfs_command", "btrfs")),
        timeshift_command=str(source_raw.get("timeshift_command", "timeshift")),
        cache_root=(str(source_raw.get("cache_root")) if source_raw.get("cache_root") else None),
        create_readonly_cache=_as_bool(source_raw.get("create_readonly_cache"), "source.create_readonly_cache", True),
    )

    destination_raw = raw.get("destination", {})
    if not isinstance(destination_raw, dict):
        raise ConfigError("[destination] must be a TOML table")
    target_root = _as_path(destination_raw.get("target_root"), "destination.target_root")
    destination = DestinationConfig(
        target_root=target_root,
        sudo=str(destination_raw.get("sudo", "sudo -n")),
        create_target_root=_as_bool(destination_raw.get("create_target_root"), "destination.create_target_root", True),
    )

    retention_raw = raw.get("retention", {})
    if not isinstance(retention_raw, dict):
        raise ConfigError("[retention] must be a TOML table")
    retention = RetentionConfig(
        hourly=_as_int(retention_raw.get("hourly"), "retention.hourly", 6),
        daily=_as_int(retention_raw.get("daily"), "retention.daily", 7),
        weekly=_as_int(retention_raw.get("weekly"), "retention.weekly", 4),
        monthly=_as_int(retention_raw.get("monthly"), "retention.monthly", 6),
        boot=_as_int(retention_raw.get("boot"), "retention.boot", 5),
        ondemand=_as_int(retention_raw.get("ondemand"), "retention.ondemand", 10),
        yearly=_as_int(retention_raw.get("yearly"), "retention.yearly", 0),
        keep_latest=_as_bool(retention_raw.get("keep_latest"), "retention.keep_latest", True),
        keep_latest_common_parent=_as_bool(retention_raw.get("keep_latest_common_parent"), "retention.keep_latest_common_parent", True),
        protected_snapshots=_string_list(retention_raw.get("protected_snapshots"), "retention.protected_snapshots"),
    )

    state_file = _as_path(raw.get("state_file", str(target_root / ".ts-btrfs-sync" / "state.json")), "state_file")
    lock_file = _as_path(raw.get("lock_file", str(target_root / ".ts-btrfs-sync" / "lock")), "lock_file")
    log_dir = _as_path(raw.get("log_dir", str(target_root / ".ts-btrfs-sync" / "logs")), "log_dir")

    return AppConfig(
        name=name,
        ssh=ssh,
        source=source,
        destination=destination,
        retention=retention,
        state_file=state_file,
        lock_file=lock_file,
        log_dir=log_dir,
        default_dry_run=_as_bool(raw.get("default_dry_run"), "default_dry_run", True),
        prune_after_sync=_as_bool(raw.get("prune_after_sync"), "prune_after_sync", False),
    )
