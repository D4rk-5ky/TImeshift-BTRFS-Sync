"""Shared dataclasses used by sync and state handling."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SubvolumeMeta:
    """Metadata for one Btrfs subvolume inside one Timeshift snapshot."""

    name: str
    path: str
    uuid: str | None = None
    parent_uuid: str | None = None
    received_uuid: str | None = None
    readonly: bool | None = None
    send_path: str | None = None


@dataclass(slots=True)
class SnapshotMeta:
    """Metadata for one Timeshift snapshot."""

    name: str
    path: str
    tags: list[str] = field(default_factory=list)
    comment: str | None = None
    created: str | None = None
    subvolumes: dict[str, SubvolumeMeta] = field(default_factory=dict)

    def sort_key(self) -> str:
        # Timeshift's normal timestamp names sort oldest->newest as strings.
        return self.name
