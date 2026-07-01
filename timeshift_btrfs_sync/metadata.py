"""Timeshift snapshot metadata-file copy helpers.

Btrfs send/receive transfers subvolumes such as ``@`` and ``@home`` only.  A
Timeshift snapshot date folder also contains ordinary metadata files, most
importantly ``info.json``.  These helpers copy those ordinary files beside the
received subvolumes so a restored destination keeps the Timeshift snapshot
layout, and they remove those files before prune/destroy removes the date
folder.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import base64
import json
import os
import shlex

from .commands import quote_join
from .models import SnapshotMeta
from .source import SourceRunner
from .timeshift import normalize_tags


@dataclass(slots=True)
class MetadataFile:
    """One ordinary metadata file from a Timeshift snapshot date folder."""

    name: str
    data: bytes


@dataclass(slots=True)
class MetadataStageResult:
    """Result from staging source Timeshift metadata into the send cache."""

    copied: list[str]
    unchanged: list[str]
    skipped: bool = False


@dataclass(slots=True)
class MetadataCopyResult:
    """Result from copying ordinary Timeshift snapshot metadata files."""

    copied: list[str]
    unchanged: list[str]
    missing: list[str]
    staged: list[str] | None = None

    @property
    def changed(self) -> bool:
        """Return True when any destination metadata file was written."""

        return bool(self.copied)


def _safe_metadata_name(name: str) -> str | None:
    """Return a safe top-level metadata filename or None.

    Snapshot metadata files are copied only as direct children of the Timeshift
    date folder.  Path separators, traversal names, and embedded NULs are
    rejected so source output can never escape the destination snapshot folder.
    """

    if not name or name in {".", ".."} or "/" in name or "\x00" in name or "\t" in name or "\n" in name or "\r" in name:
        return None
    return name


def source_snapshot_metadata_files(source: SourceRunner, snapshot_dir: str) -> list[MetadataFile]:
    """Read ordinary top-level files from one source Timeshift snapshot folder.

    The command intentionally does not use sudo.  The project keeps source-side
    sudoers narrow: only ``btrfs`` and ``timeshift`` should need passwordless
    sudo.  Timeshift ``info.json`` is normally readable; if ordinary metadata
    files are not readable, the app reports that as a source permission problem
    instead of asking for broad sudo ``cat``/``tar`` access.
    """

    dir_q = shlex.quote(str(snapshot_dir))
    script = f"""
dir={dir_q}
[ -d "$dir" ] || exit 0
for file in "$dir"/* "$dir"/.[!.]* "$dir"/..?*; do
    [ -e "$file" ] || continue
    [ -f "$file" ] || continue
    name=${{file##*/}}
    case "$name" in
        .|..) continue ;;
    esac
    printf 'TSBTRFS_META_FILE\\t%s\\t' "$name"
    if base64 -- "$file" 2>/dev/null | tr -d '\\n'; then
        printf '\\n'
    else
        printf '\\nTSBTRFS_META_ERROR\\t%s\\tbase64 failed or file is not readable\\n' "$name"
    fi
done
""".strip()
    result = source.run("sh -c " + shlex.quote(script), mirror_stderr=False)
    files: list[MetadataFile] = []
    errors: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("TSBTRFS_META_FILE\t"):
            try:
                _tag, name, encoded = line.split("\t", 2)
            except ValueError:
                errors.append(f"malformed metadata line: {line!r}")
                continue
            safe_name = _safe_metadata_name(name)
            if safe_name is None:
                errors.append(f"unsafe metadata filename from source: {name!r}")
                continue
            try:
                data = base64.b64decode(encoded.encode("ascii"), validate=True)
            except Exception as exc:
                errors.append(f"could not decode source metadata file {name!r}: {exc}")
                continue
            files.append(MetadataFile(name=safe_name, data=data))
        elif line.startswith("TSBTRFS_META_ERROR\t"):
            errors.append(line.replace("TSBTRFS_META_ERROR\t", "", 1))
        elif line.strip():
            errors.append(f"unexpected metadata output: {line!r}")
    if errors:
        raise RuntimeError("Could not read source Timeshift metadata files: " + "; ".join(errors))
    return files


def stage_snapshot_metadata_files_to_cache(
    source: SourceRunner,
    *,
    source_snapshot_dir: str,
    cache_snapshot_dir: str,
    dry_run: bool,
) -> MetadataStageResult:
    """Stage Timeshift ordinary metadata files into the source send-cache folder.

    SSH syncs may stream Btrfs data from ``source.cache_root`` instead of the
    live Timeshift snapshot date folder.  Keep the ordinary Timeshift metadata
    beside that cached send copy too, so the later destination metadata copy can
    use the app-owned cache path and so interrupted/recovery runs can still
    find ``info.json`` with the cached read-only subvolumes.

    The command intentionally uses only the source user, not sudo. Source sudo
    stays limited to ``btrfs`` and ``timeshift``. Therefore the per-snapshot
    cache parent must be writable by the source user if metadata staging is
    required. If a sudo-created cache parent is root-owned, this function raises
    a clear permission error instead of silently reporting ``info.json`` as
    missing.
    """

    if dry_run:
        return MetadataStageResult(copied=[], unchanged=[], skipped=True)

    src_q = shlex.quote(str(source_snapshot_dir))
    dst_q = shlex.quote(str(cache_snapshot_dir))
    script = f"""
src={src_q}
dst={dst_q}
[ -d "$src" ] || {{ printf 'TSBTRFS_META_STAGE_ERROR\\tsource snapshot metadata directory is missing: %s\\n' "$src"; exit 0; }}
[ -r "$src" ] || {{ printf 'TSBTRFS_META_STAGE_ERROR\\tsource snapshot metadata directory is not readable by the source user: %s\\n' "$src"; exit 0; }}
[ -d "$dst" ] || {{ printf 'TSBTRFS_META_STAGE_ERROR\\tcache metadata directory is missing: %s\\n' "$dst"; exit 0; }}
[ -w "$dst" ] || {{ printf 'TSBTRFS_META_STAGE_ERROR\\tcache metadata directory is not writable by the source user: %s\\n' "$dst"; exit 0; }}
for file in "$src"/* "$src"/.[!.]* "$src"/..?*; do
    [ -e "$file" ] || continue
    [ -f "$file" ] || continue
    name=${{file##*/}}
    case "$name" in
        .|..|.ts-btrfs-meta-*) continue ;;
    esac
    target="$dst/$name"
    if [ -f "$target" ] && cmp -s -- "$file" "$target"; then
        printf 'TSBTRFS_META_STAGE_UNCHANGED\\t%s\\n' "$name"
        continue
    fi
    tmp="$dst/.ts-btrfs-meta-$name.$$"
    if cp -p -- "$file" "$tmp" 2>/dev/null && mv -f -- "$tmp" "$target" 2>/dev/null; then
        printf 'TSBTRFS_META_STAGE_COPIED\\t%s\\n' "$name"
    else
        rm -f -- "$tmp" 2>/dev/null || true
        printf 'TSBTRFS_META_STAGE_ERROR\\tfailed copying metadata file to cache: %s\\n' "$name"
    fi
done
""".strip()
    result = source.run("sh -c " + shlex.quote(script), mirror_stderr=False)
    copied: list[str] = []
    unchanged: list[str] = []
    errors: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("TSBTRFS_META_STAGE_COPIED\t"):
            name = line.split("\t", 1)[1]
            safe_name = _safe_metadata_name(name)
            if safe_name is not None:
                copied.append(safe_name)
        elif line.startswith("TSBTRFS_META_STAGE_UNCHANGED\t"):
            name = line.split("\t", 1)[1]
            safe_name = _safe_metadata_name(name)
            if safe_name is not None:
                unchanged.append(safe_name)
        elif line.startswith("TSBTRFS_META_STAGE_ERROR\t"):
            errors.append(line.split("\t", 1)[1])
        elif line.strip():
            errors.append(f"unexpected metadata staging output: {line!r}")
    if errors:
        raise RuntimeError("Could not stage source Timeshift metadata files into send-cache: " + "; ".join(errors))
    return MetadataStageResult(copied=sorted(copied), unchanged=sorted(unchanged))


def copy_snapshot_metadata_files(
    source: SourceRunner,
    *,
    snapshot: SnapshotMeta,
    destination_snapshot_dir: Path,
    dry_run: bool,
    source_snapshot_dir: str | None = None,
    staged: MetadataStageResult | None = None,
) -> MetadataCopyResult:
    """Copy ordinary Timeshift metadata files beside received subvolumes.

    This is intentionally independent of ``state.json``.  A destination may
    already contain ``@``/``@home`` while ``info.json`` is missing because a
    previous version only sent Btrfs subvolumes.  Each sync run can therefore
    repair metadata files even when state says the subvolumes are already synced.
    """

    files = source_snapshot_metadata_files(source, source_snapshot_dir or snapshot.path)
    copied: list[str] = []
    unchanged: list[str] = []
    present_names = {item.name for item in files}
    missing = ["info.json"] if "info.json" not in present_names else []

    if dry_run:
        return MetadataCopyResult(copied=sorted(present_names), unchanged=[], missing=missing, staged=(staged.copied if staged else None))

    destination_snapshot_dir.mkdir(parents=True, exist_ok=True)
    for item in files:
        dest = destination_snapshot_dir / item.name
        if dest.exists() and dest.is_dir():
            raise RuntimeError(f"Destination metadata path is a directory, refusing to overwrite: {dest}")
        old = dest.read_bytes() if dest.exists() and dest.is_file() else None
        if old == item.data:
            unchanged.append(item.name)
            continue
        tmp = destination_snapshot_dir / f".{item.name}.tmp-{os.getpid()}"
        tmp.write_bytes(item.data)
        tmp.replace(dest)
        copied.append(item.name)
    return MetadataCopyResult(copied=sorted(copied), unchanged=sorted(unchanged), missing=missing, staged=(staged.copied if staged else None))


def remove_destination_metadata_files(snapshot_dir: Path) -> int:
    """Remove top-level ordinary metadata files from a destination date folder.

    Prune deletes Btrfs subvolumes such as ``@`` first.  It must then remove
    ordinary files such as ``info.json`` before calling ``rmdir`` on the date
    folder, otherwise the folder remains non-empty and state cannot be safely
    removed.
    """

    if not snapshot_dir.exists() or not snapshot_dir.is_dir():
        return 0
    removed = 0
    for child in list(snapshot_dir.iterdir()):
        try:
            if child.is_file() or child.is_symlink():
                child.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def remove_empty_directories(snapshot_dir: Path) -> int:
    """Remove empty ordinary directories inside and including a date folder."""

    if not snapshot_dir.exists() or not snapshot_dir.is_dir():
        return 0
    removed = 0
    try:
        dirs = [entry for entry in snapshot_dir.rglob("*") if entry.is_dir()]
    except OSError:
        dirs = []
    for entry in sorted(dirs, key=lambda item: (len(item.parts), str(item)), reverse=True):
        try:
            entry.rmdir()
            removed += 1
        except OSError:
            pass
    try:
        snapshot_dir.rmdir()
        removed += 1
    except OSError:
        pass
    return removed


def parse_info_json_text(text: str) -> tuple[list[str], str | None, str | None]:
    """Extract Timeshift-like tags/comment/created fields from info.json text.

    Timeshift has changed JSON field names over time.  This parser is tolerant
    and uses whichever common fields are available.  It is only for metadata
    restoration/state enrichment; Btrfs UUIDs remain the authority for chain
    safety.
    """

    try:
        data = json.loads(text)
    except Exception:
        return [], None, None
    if not isinstance(data, dict):
        return [], None, None

    raw_tags = data.get("tags") or data.get("tag") or data.get("type") or data.get("snapshot_type")
    if isinstance(raw_tags, list):
        tags = normalize_tags("".join(str(item) for item in raw_tags))
    else:
        tags = normalize_tags(str(raw_tags or ""))

    comment = None
    for key in ("comments", "comment", "description"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            comment = value.strip()
            break

    created = None
    for key in ("created", "date", "snapshot_date", "time"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            created = value.strip()
            break
    return tags, comment, created


def destination_info_metadata(snapshot_dir: Path) -> tuple[list[str], str | None, str | None]:
    """Read destination info.json metadata when available."""

    path = snapshot_dir / "info.json"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [], None, None
    return parse_info_json_text(text)
