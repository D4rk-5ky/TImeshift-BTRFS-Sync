"""Data models shared across the sync code.

These dataclasses are deliberately small. They describe what was discovered on
the source machine before the information is written into the persistent JSON
state file.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SubvolumeMeta:
    """Metadata for one Btrfs subvolume inside one Timeshift snapshot."""

    # Human/configured name, usually "@" or "@home".
    name: str

    # Full path to the subvolume on the machine where it was discovered.
    path: str

    # Btrfs UUID fields from `btrfs subvolume show`.
    uuid: str | None = None
    parent_uuid: str | None = None
    received_uuid: str | None = None

    # Result of `btrfs property get -ts <path> ro`.
    # None means it could not be read.
    readonly: bool | None = None

    # Path actually used for btrfs send. This can differ from `path` when the
    # app had to create a read-only send-cache snapshot.
    send_path: str | None = None


@dataclass(slots=True)
class SnapshotMeta:
    """Metadata for one Timeshift snapshot directory."""

    # Timeshift folder name, normally a timestamp such as 2026-06-22_19-00-01.
    name: str

    # Full path to the Timeshift snapshot directory on the source.
    path: str

    # Timeshift tags such as H, D, W, M, B, O. Y is optional extension support.
    tags: list[str] = field(default_factory=list)

    # Optional human text from Timeshift's info.json.
    comment: str | None = None

    # Optional creation timestamp from Timeshift's info.json, if present.
    created: str | None = None

    # Subvolume name -> SubvolumeMeta.
    subvolumes: dict[str, SubvolumeMeta] = field(default_factory=dict)

    def sort_key(self) -> str:
        """Return a stable sort key for oldest-to-newest processing.

        Timeshift's default long timestamp names sort lexicographically in the
        same order as time. Keeping this as a method lets us improve sorting
        later without changing the sync loop.
        """

        return self.name
