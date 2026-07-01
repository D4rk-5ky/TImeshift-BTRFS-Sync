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
class MetadataCopyResult:
    """Result from copying ordinary Timeshift snapshot metadata files."""

    copied: list[str]
    unchanged: list[str]
    missing: list[str]
    source_dir: str | None = None
    used_existing_destination: bool = False

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
    sudo.  Timeshift ``info.json`` is normally readable by direct pathname even
    on folders that the normal source user cannot directory-list.  Read that
    exact file first, then copy any other ordinary top-level metadata files only
    when the directory itself is listable.  If an existing metadata file cannot
    be read, report that as a source permission problem instead of asking for
    broad sudo ``cat``/``tar`` access.
    """

    dir_q = shlex.quote(str(snapshot_dir))
    script = f"""
dir={dir_q}

emit_metadata_file() {{
    name=$1
    file=$2
    [ -f "$file" ] || return 0
    printf 'TSBTRFS_META_FILE\t%s\t' "$name"
    if base64 "$file" 2>/dev/null | tr -d '\n'; then
        printf '\n'
    else
        printf '\nTSBTRFS_META_ERROR\t%s\tbase64 failed or file is not readable\n' "$name"
    fi
}}

# Always try Timeshift's required metadata file by exact pathname first.
# This does not require read permission on the snapshot directory itself; it
# only requires search permission on the path and read permission on info.json.
emit_metadata_file info.json "$dir/info.json"

# Other ordinary side files are optional.  Copy them only when the date folder
# can be listed by the normal source user, so a root-owned/non-listable
# Timeshift folder does not make a readable info.json look missing.
[ -d "$dir" ] || exit 0
[ -r "$dir" ] || exit 0
for file in "$dir"/* "$dir"/.[!.]* "$dir"/..?*; do
    [ -e "$file" ] || continue
    [ -f "$file" ] || continue
    name=${{file##*/}}
    case "$name" in
        .|..|info.json) continue ;;
    esac
    emit_metadata_file "$name" "$file"
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


def _existing_destination_metadata_names(destination_snapshot_dir: Path) -> list[str]:
    """Return safe top-level ordinary metadata names already on destination."""

    if not destination_snapshot_dir.exists() or not destination_snapshot_dir.is_dir():
        return []
    names: list[str] = []
    for child in destination_snapshot_dir.iterdir():
        try:
            if not child.is_file():
                continue
        except OSError:
            continue
        safe_name = _safe_metadata_name(child.name)
        if safe_name is not None:
            names.append(safe_name)
    return sorted(set(names))


def _metadata_source_candidates(
    *,
    snapshot: SnapshotMeta,
    source_snapshot_dir: str | None = None,
    source_snapshot_dirs: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Build a de-duplicated list of source Timeshift metadata folders."""

    candidates: list[str] = []
    if source_snapshot_dirs:
        candidates.extend(str(item) for item in source_snapshot_dirs if item)
    if source_snapshot_dir:
        candidates.append(str(source_snapshot_dir))
    candidates.append(snapshot.path)

    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        value = str(candidate).rstrip("/")
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _read_first_available_source_metadata(
    source: SourceRunner,
    candidates: list[str],
) -> tuple[list[MetadataFile], str | None]:
    """Read metadata from the best candidate source folder that has files.

    Prefer a candidate containing ``info.json``. If none has that file, keep the
    first candidate that has any ordinary metadata so the caller can still copy
    useful Timeshift side files and warn that ``info.json`` is missing.
    """

    first_files: list[MetadataFile] = []
    first_dir: str | None = None
    for candidate in candidates:
        files = source_snapshot_metadata_files(source, candidate)
        if not files:
            continue
        if any(item.name == "info.json" for item in files):
            return files, candidate
        if first_dir is None:
            first_files = files
            first_dir = candidate
    return first_files, first_dir


def copy_snapshot_metadata_files(
    source: SourceRunner,
    *,
    snapshot: SnapshotMeta,
    destination_snapshot_dir: Path,
    dry_run: bool,
    source_snapshot_dir: str | None = None,
    source_snapshot_dirs: list[str] | tuple[str, ...] | None = None,
) -> MetadataCopyResult:
    """Copy ordinary Timeshift metadata files beside received subvolumes.

    Btrfs send/receive transfers only subvolumes such as ``@`` and ``@home``.
    Ordinary Timeshift files are read directly from the real source snapshot date
    folder with the normal source user and are written directly to the matching
    destination date folder.  The source send-cache is deliberately not used for
    metadata because Btrfs cache parent subvolumes are commonly created as root
    and are not reliably writable by the normal source user.

    This is intentionally independent of ``state.json``.  A destination may
    already contain ``@``/``@home`` while ``info.json`` is missing because a
    previous version only sent Btrfs subvolumes.  Each sync run can therefore
    repair metadata files even when state says the subvolumes are already synced.
    If the original source Timeshift folder is unavailable but destination
    metadata already exists, the existing destination files are kept and reported
    as the fallback instead of requiring metadata to exist in the source cache.
    """

    candidates = _metadata_source_candidates(
        snapshot=snapshot,
        source_snapshot_dir=source_snapshot_dir,
        source_snapshot_dirs=source_snapshot_dirs,
    )
    files, source_dir_used = _read_first_available_source_metadata(source, candidates)
    copied: list[str] = []
    unchanged: list[str] = []
    present_names = {item.name for item in files}

    if not files:
        existing_destination = _existing_destination_metadata_names(destination_snapshot_dir)
        missing = [] if "info.json" in existing_destination else ["info.json"]
        return MetadataCopyResult(
            copied=[],
            unchanged=existing_destination,
            missing=missing,
            source_dir=None,
            used_existing_destination=bool(existing_destination),
        )

    missing = ["info.json"] if "info.json" not in present_names else []

    if dry_run:
        return MetadataCopyResult(
            copied=sorted(present_names),
            unchanged=[],
            missing=missing,
            source_dir=source_dir_used,
        )

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
    return MetadataCopyResult(
        copied=sorted(copied),
        unchanged=sorted(unchanged),
        missing=missing,
        source_dir=source_dir_used,
    )

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
