"""TOML configuration loading and validation.

The CLI reads one config file and turns it into typed dataclasses. The rest of
the app then uses those dataclasses instead of looking up raw TOML values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import tomllib


# Map friendly retention names from the config file to Timeshift-style tags.
# Timeshift uses H/D/W/M/B/O. Y is our optional extension for yearly retention.
TAG_NAME_TO_LETTER = {
    "hourly": "H",
    "daily": "D",
    "weekly": "W",
    "monthly": "M",
    "boot": "B",
    "ondemand": "O",
    "on_demand": "O",
    "manual": "O",
    "yearly": "Y",  # extension, not native Timeshift
}


@dataclass(slots=True)
class SSHConfig:
    """Settings for connecting from the backup machine to the source machine."""

    host: str
    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    extra_args: list[str] = field(default_factory=list)

    @property
    def target(self) -> str:
        """Return the SSH target in user@host format when a user is configured."""

        return f"{self.user}@{self.host}" if self.user else self.host

    def base_command(self) -> list[str]:
        """Build the common `ssh ... target` command prefix.

        The remote command itself is appended later by SSHRunner.
        """

        cmd = ["ssh"]
        if self.port:
            cmd += ["-p", str(self.port)]
        if self.identity_file:
            cmd += ["-i", self.identity_file]
        cmd += self.extra_args
        cmd.append(self.target)
        return cmd


@dataclass(slots=True)
class SourceConfig:
    """Paths and commands used on the remote/source machine."""

    # Folder containing Timeshift snapshot directories.
    snapshot_root: str

    # Optional app-managed location for read-only send-cache snapshots.
    cache_root: str | None = None

    # Subvolumes to look for inside every Timeshift snapshot.
    subvolumes: list[str] = field(default_factory=lambda: ["@", "@home"])

    # Sudo command used remotely. Empty string disables sudo.
    sudo: str = "sudo"

    # Timeshift executable name/path on the source.
    timeshift_command: str = "timeshift"


@dataclass(slots=True)
class DestinationConfig:
    """Paths and commands used locally on the backup/destination machine."""

    # Local Btrfs backup root where received snapshots are stored.
    target_root: Path

    # Sudo command used locally. Empty string disables sudo.
    sudo: str = "sudo"

    # Whether to create target_root if it does not already exist.
    create_target_root: bool = True


@dataclass(slots=True)
class RetentionConfig:
    """How many snapshots of each Timeshift tag to keep on the destination."""

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
    """Complete validated config object used by sync, prune, and CLI commands."""

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
    """Raised when the TOML config is missing fields or has invalid types."""


# The helper functions below keep validation readable in load_config(). They
# also produce better error messages than letting dataclasses fail later.
def _string_list(value: Any, field_name: str) -> list[str]:
    """Validate a TOML value as `list[str]` and return an empty list for None."""

    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{field_name} must be a list of strings")
    return value


def _as_path(value: Any, field_name: str) -> Path:
    """Validate a TOML string and expand it into a pathlib.Path."""

    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field_name} must be a non-empty string")
    return Path(value).expanduser()


def _as_str(value: Any, field_name: str) -> str:
    """Validate a required TOML string."""

    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field_name} must be a non-empty string")
    return value


def _as_int(value: Any, field_name: str, default: int) -> int:
    """Validate a non-negative integer, using a default when the value is absent."""

    if value is None:
        return default
    if not isinstance(value, int) or value < 0:
        raise ConfigError(f"{field_name} must be a non-negative integer")
    return value


def _as_bool(value: Any, field_name: str, default: bool) -> bool:
    """Validate a boolean, using a default when the value is absent."""

    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{field_name} must be true or false")
    return value


def load_config(path: str | Path) -> AppConfig:
    """Read, validate, and normalize a TOML config file.

    The returned AppConfig has all default paths filled in, including the state,
    lock, and log paths under `<target_root>/.ts-btrfs-sync/` unless overridden.
    """

    path = Path(path).expanduser()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    if not isinstance(raw, dict):
        raise ConfigError("Config must be a TOML table")

    # Top-level app name used mostly for human identification.
    name = str(raw.get("name") or "timeshift-btrfs-sync")

    # --- [ssh] section -----------------------------------------------------
    ssh_raw = raw.get("ssh", {})
    if not isinstance(ssh_raw, dict):
        raise ConfigError("[ssh] must be a TOML table")
    host = _as_str(ssh_raw.get("host"), "ssh.host")
    port = ssh_raw.get("port")
    if port is not None and (not isinstance(port, int) or port <= 0):
        raise ConfigError("ssh.port must be a positive integer")
    ssh = SSHConfig(
        host=host,
        user=ssh_raw.get("user") if isinstance(ssh_raw.get("user"), str) and ssh_raw.get("user") else None,
        port=port,
        identity_file=ssh_raw.get("identity_file") if isinstance(ssh_raw.get("identity_file"), str) and ssh_raw.get("identity_file") else None,
        extra_args=_string_list(ssh_raw.get("extra_args"), "ssh.extra_args"),
    )

    # --- [source] section --------------------------------------------------
    source_raw = raw.get("source", {})
    if not isinstance(source_raw, dict):
        raise ConfigError("[source] must be a TOML table")
    source_root = _as_str(source_raw.get("snapshot_root"), "source.snapshot_root")
    source = SourceConfig(
        # Remove a trailing slash so later path joins are consistent.
        snapshot_root=source_root.rstrip("/"),
        cache_root=(source_raw.get("cache_root") or None),
        subvolumes=_string_list(source_raw.get("subvolumes", ["@", "@home"]), "source.subvolumes") or ["@", "@home"],
        sudo=str(source_raw.get("sudo", "sudo")),
        timeshift_command=str(source_raw.get("timeshift_command", "timeshift")),
    )

    # --- [destination] section --------------------------------------------
    destination_raw = raw.get("destination", {})
    if not isinstance(destination_raw, dict):
        raise ConfigError("[destination] must be a TOML table")
    target_root = _as_path(destination_raw.get("target_root"), "destination.target_root")
    destination = DestinationConfig(
        target_root=target_root,
        sudo=str(destination_raw.get("sudo", "sudo")),
        create_target_root=_as_bool(destination_raw.get("create_target_root"), "destination.create_target_root", True),
    )

    # --- [retention] section ----------------------------------------------
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
        keep_latest_common_parent=_as_bool(
            retention_raw.get("keep_latest_common_parent"),
            "retention.keep_latest_common_parent",
            True,
        ),
        protected_snapshots=_string_list(retention_raw.get("protected_snapshots"), "retention.protected_snapshots"),
    )

    # Internal metadata paths. These default to a hidden app folder in the
    # destination root so each backup target can keep its own independent state.
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
