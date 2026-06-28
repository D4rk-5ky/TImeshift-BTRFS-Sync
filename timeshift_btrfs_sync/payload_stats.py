"""Normalized source/destination payload statistics for Btrfs snapshot trees.

Raw Btrfs subvolume counts can differ even when source and destination contain
matching backup payloads. The source side can contain helper/container
subvolumes such as ``send-cache/<snapshot-date>`` while the destination stores
only the received ``@``/``@home`` payload subvolumes below its snapshot tree.

Since v0.1.2, read-only Timeshift originals may also be used directly as send
sources. Those protected Timeshift-owned paths are not under ``source.cache_root``
and are not destroy-leftovers targets, so this module can merge direct-send
payload entries from state.json with the app-owned source-cache payload before
comparing source and destination payloads.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import os

from . import state as state_mod


PayloadKey = tuple[str, str]


@dataclass(slots=True)
class PayloadTreeStats:
    """Normalized payload/container counts for one source or destination tree."""

    root: str
    kind: str
    subvolume_names: tuple[str, ...]
    raw_total: int = 0
    root_subvolume: bool = False
    timestamp_parent_subvolumes: int = 0
    payload: set[PayloadKey] = field(default_factory=set)
    payload_by_subvolume: Counter[str] = field(default_factory=Counter)
    direct_payload: set[PayloadKey] = field(default_factory=set)
    direct_payload_by_subvolume: Counter[str] = field(default_factory=Counter)
    cache_payload: set[PayloadKey] = field(default_factory=set)
    ignored_subvolumes: int = 0

    @property
    def total_payload(self) -> int:
        """Return the number of real cached/received payload subvolumes."""

        return len(self.payload)

    @property
    def total_cache_payload(self) -> int:
        """Return how many source payloads came from app-owned source cache."""

        return len(self.cache_payload)

    @property
    def total_direct_payload(self) -> int:
        """Return how many source payloads came from protected Timeshift originals."""

        return len(self.direct_payload)


def normalize_path(path: str | Path) -> str:
    """Normalize paths so source/destination comparisons ignore trailing slashes."""

    return os.path.normpath(str(path)).rstrip("/") or "/"


def _relative_parts(root: str | Path, path: str | Path) -> tuple[str, ...] | None:
    """Return path parts relative to root, or None if path is outside root."""

    root_text = normalize_path(root)
    path_text = normalize_path(path)
    if path_text == root_text:
        return ()
    prefix = root_text + "/"
    if not path_text.startswith(prefix):
        return None
    relative = path_text[len(prefix) :]
    return tuple(part for part in relative.split("/") if part)


def _recount_payload(stats: PayloadTreeStats) -> None:
    """Rebuild per-subvolume counters from the normalized payload set."""

    stats.payload_by_subvolume.clear()
    for _snapshot, subvol in stats.payload:
        stats.payload_by_subvolume[subvol] += 1


def _add_payload(stats: PayloadTreeStats, parts: tuple[str, ...]) -> bool:
    """Add a payload entry when relative parts end in a configured subvolume name."""

    if len(parts) < 2 or parts[-1] not in stats.subvolume_names:
        return False
    key = (parts[-2], parts[-1])
    if key not in stats.payload:
        stats.payload.add(key)
        stats.cache_payload.add(key)
        stats.payload_by_subvolume[parts[-1]] += 1
    return True


def source_send_cache_stats(root: str | Path, subvolume_paths: list[str], subvolume_names: list[str]) -> PayloadTreeStats:
    """Classify source send-cache subvolumes into payload and helper counts."""

    stats = PayloadTreeStats(root=normalize_path(root), kind="source", subvolume_names=tuple(subvolume_names))
    normalized_paths = sorted({normalize_path(path) for path in subvolume_paths})
    stats.raw_total = len(normalized_paths)
    for path in normalized_paths:
        parts = _relative_parts(stats.root, path)
        if parts is None:
            stats.ignored_subvolumes += 1
            continue
        if not parts:
            stats.root_subvolume = True
            continue
        if _add_payload(stats, parts):
            continue
        if len(parts) == 1:
            stats.timestamp_parent_subvolumes += 1
        else:
            stats.ignored_subvolumes += 1
    return stats


def destination_payload_stats(root: str | Path, subvolume_paths: list[str], subvolume_names: list[str]) -> PayloadTreeStats:
    """Classify destination target subvolumes into received payload counts."""

    stats = PayloadTreeStats(root=normalize_path(root), kind="destination", subvolume_names=tuple(subvolume_names))
    normalized_paths = sorted({normalize_path(path) for path in subvolume_paths})
    stats.raw_total = len(normalized_paths)
    for path in normalized_paths:
        parts = _relative_parts(stats.root, path)
        if parts is None:
            stats.ignored_subvolumes += 1
            continue
        if not parts:
            stats.root_subvolume = True
            continue
        if _add_payload(stats, parts):
            # Destination payloads are received snapshots, not source-cache
            # payloads. Keep cache_payload source-only by removing the entry.
            stats.cache_payload.discard((parts[-2], parts[-1]))
            continue
        stats.ignored_subvolumes += 1
    return stats


def direct_send_payload_stats(state_doc: dict[str, Any], subvolume_names: list[str], *, cache_root: str | None = None) -> PayloadTreeStats:
    """Return payload entries streamed directly from protected Timeshift originals.

    Direct-send paths are Timeshift-owned and must never be removed by
    destroy-leftovers or prune. They are still valid source-side payload when
    explaining why source and destination snapshots match after v0.1.2 direct
    read-only sends.
    """

    stats = PayloadTreeStats(root="state.json direct Timeshift send paths", kind="source-direct", subvolume_names=tuple(subvolume_names))
    allowed = set(subvolume_names)
    for snapshot_name, item in state_doc.get("snapshots", {}).items():
        if not isinstance(item, dict):
            continue
        subvolumes = item.get("subvolumes", {})
        if not isinstance(subvolumes, dict):
            continue
        for subvol_name, subvol_state in subvolumes.items():
            if subvol_name not in allowed or not isinstance(subvol_state, dict):
                continue
            if subvol_state.get("status") != "ok":
                continue
            if not state_mod.state_send_path_is_protected_timeshift_original(subvol_state, cache_root=cache_root):
                continue
            key = (str(snapshot_name), str(subvol_name))
            stats.payload.add(key)
            stats.direct_payload.add(key)
            stats.direct_payload_by_subvolume[str(subvol_name)] += 1
    _recount_payload(stats)
    return stats


def merge_source_payload_stats(cache: PayloadTreeStats, direct: PayloadTreeStats | None = None) -> PayloadTreeStats:
    """Merge app-cache and protected direct-send payload into one source view."""

    merged = PayloadTreeStats(
        root=cache.root,
        kind="source",
        subvolume_names=cache.subvolume_names,
        raw_total=cache.raw_total,
        root_subvolume=cache.root_subvolume,
        timestamp_parent_subvolumes=cache.timestamp_parent_subvolumes,
        payload=set(cache.payload),
        cache_payload=set(cache.cache_payload or cache.payload),
        ignored_subvolumes=cache.ignored_subvolumes,
    )
    if direct is not None:
        merged.payload.update(direct.payload)
        merged.direct_payload.update(direct.payload)
        merged.direct_payload_by_subvolume.update(direct.direct_payload_by_subvolume)
    _recount_payload(merged)
    return merged


@dataclass(slots=True)
class PayloadMatchStats:
    """Comparison between source send payload and destination received payload."""

    source: PayloadTreeStats
    destination: PayloadTreeStats

    @property
    def source_only(self) -> set[PayloadKey]:
        """Return source payload entries not present on the destination."""

        return self.source.payload - self.destination.payload

    @property
    def destination_only(self) -> set[PayloadKey]:
        """Return destination payload entries not present on the source side."""

        return self.destination.payload - self.source.payload

    @property
    def ok(self) -> bool:
        """Return True when source send payload and destination payload match."""

        return not self.source_only and not self.destination_only


def compare_payloads(source: PayloadTreeStats, destination: PayloadTreeStats) -> PayloadMatchStats:
    """Return normalized source/destination payload comparison stats."""

    return PayloadMatchStats(source=source, destination=destination)


def _format_count_line(label: str, value: int | str, width: int = 17) -> str:
    """Return an aligned summary line."""

    return f"  {label:<{width}} {value}"


def render_payload_match(stats: PayloadMatchStats) -> list[str]:
    """Render the source/destination payload comparison block."""

    names = list(stats.source.subvolume_names or stats.destination.subvolume_names)
    source_title = "Source send payload:" if stats.source.direct_payload else "Source cached payload:"
    lines = [
        "SOURCE / DESTINATION SNAPSHOT MATCH",
        "===================================",
        source_title,
    ]
    for name in names:
        lines.append(_format_count_line(f"{name} snapshots:", stats.source.payload_by_subvolume.get(name, 0)))
    lines.append(_format_count_line("total payload:", stats.source.total_payload))
    if stats.source.direct_payload:
        lines.append(_format_count_line("cache payload:", stats.source.total_cache_payload))
        lines.append(_format_count_line("direct Timeshift payload:", stats.source.total_direct_payload))
    lines.extend(["", "Destination received payload:"])
    for name in names:
        lines.append(_format_count_line(f"{name} snapshots:", stats.destination.payload_by_subvolume.get(name, 0)))
    lines.append(_format_count_line("total payload:", stats.destination.total_payload))
    lines.extend(
        [
            "",
            "Container/helper subvolumes:",
            f"  source send-cache root:          {'yes' if stats.source.root_subvolume else 'no'}",
            f"  source timestamp parent subvols: {stats.source.timestamp_parent_subvolumes}",
            f"  source protected direct sends:   {stats.source.total_direct_payload}",
            f"  destination target root:         {'yes' if stats.destination.root_subvolume else 'no'}",
            "",
            "Raw subvolume totals:",
            f"  source send-cache raw total:     {stats.source.raw_total}",
            f"  destination target raw total:    {stats.destination.raw_total}",
            "",
        ]
    )
    if stats.ok:
        lines.append("Result:")
        lines.append("  OK - source send payload matches destination received payload")
    else:
        lines.append("Result:")
        lines.append("  WARNING - source send payload does not match destination received payload")
        if stats.source_only:
            lines.append("  Source-only payload snapshots:")
            for snapshot, subvol in sorted(stats.source_only):
                lines.append(f"    {snapshot}/{subvol}")
        if stats.destination_only:
            lines.append("  Destination-only payload snapshots:")
            for snapshot, subvol in sorted(stats.destination_only):
                lines.append(f"    {snapshot}/{subvol}")
    return lines
