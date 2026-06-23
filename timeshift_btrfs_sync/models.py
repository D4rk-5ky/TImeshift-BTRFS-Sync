"""Shared dataclasses used by discovery, sync, and state handling."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SubvolumeMeta:
    """Metadata for one Btrfs subvolume inside one Timeshift snapshot."""

    # Configured/visible name, normally "@" or "@home".
    name: str

    # Full path to the subvolume on the machine where it was discovered.
    path: str

    # Fields read from `btrfs subvolume show`.
    uuid: str | None = None
    parent_uuid: str | None = None
    received_uuid: str | None = None

    # Result from `btrfs property get -ts <path> ro`.
    readonly: bool | None = None

    # Actual path used for btrfs send. This may be a read-only cache snapshot.
    send_path: str | None = None


@dataclass(slots=True)
class SnapshotMeta:
    """Metadata for one Timeshift snapshot."""

    # Timeshift snapshot name, normally `YYYY-MM-DD_HH-MM-SS`.
    name: str

    # Full source-side snapshot directory path.
    path: str

    # Timeshift retention tags: H/D/W/M/B/O. Y is optional extension support.
    tags: list[str] = field(default_factory=list)

    # Optional parsed comment from `timeshift --list`.
    comment: str | None = None

    # Optional creation timestamp. For normal Timeshift names, the name is used.
    created: str | None = None

    # Mapping from subvolume name to its metadata.
    subvolumes: dict[str, SubvolumeMeta] = field(default_factory=dict)

    def sort_key(self) -> str:
        """Return the key used to process snapshots oldest-to-newest."""

        # Timeshift's timestamp names sort correctly as strings.
        return self.name
