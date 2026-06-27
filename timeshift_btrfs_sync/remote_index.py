"""Per-run Btrfs subvolume indexes for fewer SSH calls.

The index is intentionally short lived. It is built at the start of a command
or refreshed after a create/receive/delete operation. It never replaces the
UUID safety rules; it only replaces repeated ``btrfs subvolume list/show``
process startups with dictionary lookups whenever the same metadata has already
been read in the current run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import shlex

from . import btrfs
from .commands import run_local, sudo_prefix
from .models import SubvolumeMeta
from .ssh import SSHRunner


@dataclass(slots=True)
class BtrfsIndex:
    """In-memory index of Btrfs subvolumes below one root path."""

    root: str
    location: str
    by_path: dict[str, SubvolumeMeta] = field(default_factory=dict)
    by_uuid: dict[str, SubvolumeMeta] = field(default_factory=dict)
    by_received_uuid: dict[str, SubvolumeMeta] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    root_missing: bool = False

    def add(self, meta: SubvolumeMeta | None) -> None:
        """Add or replace one indexed subvolume."""

        if not meta or not meta.path:
            return
        path = normalize_path(meta.path)
        meta.path = path
        self.by_path[path] = meta
        if meta.uuid:
            self.by_uuid[meta.uuid] = meta
        if meta.received_uuid:
            self.by_received_uuid[meta.received_uuid] = meta

    def discard(self, path: str) -> None:
        """Remove one path and any known UUID lookup entries for it."""

        path = normalize_path(path)
        meta = self.by_path.pop(path, None)
        if not meta:
            return
        if meta.uuid and self.by_uuid.get(meta.uuid) is meta:
            self.by_uuid.pop(meta.uuid, None)
        if meta.received_uuid and self.by_received_uuid.get(meta.received_uuid) is meta:
            self.by_received_uuid.pop(meta.received_uuid, None)

    def contains(self, path: str | Path | None) -> bool:
        """Return True when ``path`` is an indexed subvolume."""

        return bool(path) and normalize_path(path) in self.by_path

    def meta(self, path: str | Path | None) -> SubvolumeMeta | None:
        """Return metadata for ``path`` if it was indexed."""

        return self.by_path.get(normalize_path(path)) if path else None

    def child_paths(self, path: str | Path) -> list[str]:
        """Return indexed descendants below ``path``."""

        root = normalize_path(path)
        return sorted(
            [candidate for candidate in self.by_path if candidate != root and is_under(candidate, root)],
            key=lambda item: (item.count("/"), item),
            reverse=True,
        )

    def is_empty(self, path: str | Path) -> bool | None:
        """Return whether an indexed path has indexed child subvolumes."""

        root = normalize_path(path)
        if root not in self.by_path:
            return None
        return not any(candidate != root and is_under(candidate, root) for candidate in self.by_path)

    def remove_tree(self, path: str | Path) -> None:
        """Remove a deleted path and all indexed descendants."""

        root = normalize_path(path)
        for candidate in list(self.by_path):
            if is_under(candidate, root):
                self.discard(candidate)


def normalize_path(path: str | Path) -> str:
    """Normalize paths so lookups do not depend on trailing slashes."""

    return os.path.normpath(str(path)).rstrip("/") or "/"


def is_under(path: str | Path, root: str | Path) -> bool:
    """Return True when path is root or below root."""

    path_text = normalize_path(path)
    root_text = normalize_path(root)
    return path_text == root_text or path_text.startswith(root_text + "/")


def listed_path_to_absolute(root_path: str | Path, listed_path: str) -> str | None:
    """Convert a Btrfs-listed path to an absolute path under ``root_path``.

    ``btrfs subvolume list`` reports paths relative to the filesystem tree, not
    necessarily the user supplied mount path. The converter accepts full paths,
    paths ending with the configured root suffix, and paths starting at the root
    basename. This mirrors the older suffix-based cache matching but produces a
    concrete absolute path for dictionary lookups.
    """

    root = normalize_path(root_path)
    listed = os.path.normpath(listed_path.strip())
    if not listed or listed == ".":
        return None
    if listed.startswith("/"):
        candidate = normalize_path(listed)
        return candidate if is_under(candidate, root) else None

    root_parts = [part for part in Path(root).parts if part not in {"/", ""}]
    listed_parts = [part for part in Path(listed).parts if part not in {"/", ""}]
    if not listed_parts:
        return None

    # Full configured path without leading slash, for example
    # media/disk/timeshift-btrfs/.ts-btrfs-sync/send-cache/...
    if listed_parts[: len(root_parts)] == root_parts:
        candidate = "/" + "/".join(listed_parts)
        return normalize_path(candidate) if is_under(candidate, root) else None

    # Any suffix of the configured root, for example
    # .ts-btrfs-sync/send-cache/... or send-cache/...
    for index in range(1, len(root_parts)):
        suffix = root_parts[index:]
        if listed_parts[: len(suffix)] == suffix:
            candidate = "/" + "/".join(root_parts[:index] + listed_parts)
            candidate = normalize_path(candidate)
            return candidate if is_under(candidate, root) else None

    return None


def _clean_uuid(value: str | None) -> str | None:
    """Normalize Btrfs UUID fields from list/show output."""

    if value is None:
        return None
    value = value.strip()
    return None if not value or value == "-" else value


def parse_subvolume_list(output: str, root_path: str | Path) -> list[SubvolumeMeta]:
    """Parse ``btrfs subvolume list -u -q -R`` output for one root."""

    metas: list[SubvolumeMeta] = []
    for line in output.splitlines():
        before, sep, raw_path = line.strip().partition(" path ")
        if not sep:
            continue
        abs_path = listed_path_to_absolute(root_path, raw_path)
        if not abs_path:
            continue
        tokens = before.split()
        meta = SubvolumeMeta(name=Path(abs_path).name, path=abs_path)
        for idx, token in enumerate(tokens[:-1]):
            key = token.lower()
            value = _clean_uuid(tokens[idx + 1])
            if key == "uuid":
                meta.uuid = value
            elif key == "parent_uuid":
                meta.parent_uuid = value
            elif key == "received_uuid":
                meta.received_uuid = value
        metas.append(meta)
    return metas


def _index_from_list_output(root_path: str | Path, output: str, *, location: str) -> BtrfsIndex:
    index = BtrfsIndex(root=normalize_path(root_path), location=location)
    for meta in parse_subvolume_list(output, index.root):
        index.add(meta)
    return index


def build_local_btrfs_index(
    root_path: str | Path,
    *,
    sudo: str,
    btrfs_command: str,
    include_root: bool = True,
    required: bool = False,
) -> BtrfsIndex:
    """Build a local destination index with no SSH overhead."""

    root = normalize_path(root_path)
    index = BtrfsIndex(root=root, location="local")
    if not Path(root).exists():
        index.root_missing = True
        if required:
            index.errors.append(f"local index root is missing: {root}")
        return index

    pending = [root]
    seen: set[str] = set()
    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        result = run_local(
            btrfs.local_btrfs_cmd(sudo, btrfs_command, ["subvolume", "list", "-u", "-q", "-R", "-o", current]),
            check=False,
            log_stderr=False,
            mirror_stderr=False,
        )
        if result.returncode != 0:
            if current == root and required:
                index.errors.append(result.stderr.strip() or result.stdout.strip() or f"return code {result.returncode}")
            continue
        metas = parse_subvolume_list(result.stdout, current)
        for meta in metas:
            index.add(meta)
            if is_under(meta.path, root) and meta.path not in seen:
                pending.append(meta.path)

    if include_root:
        root_meta = btrfs.get_subvolume_meta("local", root, Path(root).name, sudo, btrfs_command, required=False)
        index.add(root_meta)
    return index


def _remote_recursive_index_script(root: str, sudo: str, btrfs_command: str) -> str:
    """Return a POSIX shell script that recursively lists subvolumes in one SSH call."""

    root_q = shlex.quote(normalize_path(root))
    sudo_words = " ".join(shlex.quote(part) for part in sudo_prefix(sudo))
    btrfs_q = shlex.quote(btrfs_command)
    return f"""
root={root_q}
root_no_slash=${{root#/}}
root_base=${{root##*/}}
root_parent=${{root%/*}}
sudo_words={shlex.quote(sudo_words)}
btrfs_cmd={btrfs_q}

emit_abs() {{
    p=$1
    case "$p" in
        /*) printf '%s\n' "$p"; return 0 ;;
    esac
    case "$p" in
        "$root_no_slash"|"$root_no_slash"/*) printf '/%s\n' "$p"; return 0 ;;
        "$root_base") printf '%s\n' "$root"; return 0 ;;
        "$root_base"/*) printf '%s/%s\n' "$root_parent" "${{p#"$root_base"/}}"; return 0 ;;
    esac

    # Last-resort suffix match. Walk the configured root from left to right and
    # accept any listed path beginning with a suffix of the root.
    suffix=$root_no_slash
    prefix=''
    while [ -n "$suffix" ]; do
        case "$p" in
            "$suffix"|"$suffix"/*)
                printf '/%s%s\n' "$prefix" "$p"
                return 0
                ;;
        esac
        first=${{suffix%%/*}}
        if [ "$first" = "$suffix" ]; then
            break
        fi
        prefix="$prefix$first/"
        suffix=${{suffix#*/}}
    done
    return 1
}}

run_btrfs() {{
    if [ -n "$sudo_words" ]; then
        # shellcheck disable=SC2086
        $sudo_words "$btrfs_cmd" "$@"
    else
        "$btrfs_cmd" "$@"
    fi
}}

printf 'TSBTRFS_ROOT\t%s\n' "$root"
printf 'TSBTRFS_ROOT_SHOW_BEGIN\n'
run_btrfs subvolume show "$root" 2>&1
printf 'TSBTRFS_ROOT_SHOW_END\n'

queue_file=$(mktemp)
seen_file=$(mktemp)
trap 'rm -f "$queue_file" "$seen_file"' EXIT
printf '%s\n' "$root" > "$queue_file"

while IFS= read -r current; do
    grep -Fx -- "$current" "$seen_file" >/dev/null 2>&1 && continue
    printf '%s\n' "$current" >> "$seen_file"
    printf 'TSBTRFS_LIST_BEGIN\t%s\n' "$current"
    list_output=$(run_btrfs subvolume list -u -q -R -o "$current" 2>&1)
    list_status=$?
    printf 'TSBTRFS_LIST_STATUS\t%s\t%s\n' "$current" "$list_status"
    printf '%s\n' "$list_output"
    printf 'TSBTRFS_LIST_END\t%s\n' "$current"
    [ "$list_status" -eq 0 ] || continue
    printf '%s\n' "$list_output" | while IFS= read -r line; do
        case "$line" in
            *' path '*) p=${{line##* path }} ;;
            *) continue ;;
        esac
        abs=$(emit_abs "$p") || continue
        case "$abs" in
            "$root"|"$root"/*)
                grep -Fx -- "$abs" "$seen_file" >/dev/null 2>&1 || printf '%s\n' "$abs" >> "$queue_file"
                ;;
        esac
    done
    # POSIX sh reads newly appended lines from the same file in this loop on the
    # target systems this app supports, allowing one SSH command to walk nested
    # cache parents such as send-cache/<date>/@ and @home.
done < "$queue_file"
""".strip()


def build_remote_btrfs_index(
    ssh: SSHRunner,
    root_path: str | Path | None,
    *,
    sudo: str,
    btrfs_command: str,
    include_root: bool = True,
    required: bool = False,
) -> BtrfsIndex:
    """Build a remote source index using one SSH command.

    The remote command may run several ``btrfs`` probes on the source host, but
    all of them happen inside one SSH session. This avoids repeated encrypted-key
    authentication while still using only the configured restricted sudo+btrfs
    permissions.
    """

    if not root_path:
        return BtrfsIndex(root="", location="remote")
    root = normalize_path(root_path)
    script = _remote_recursive_index_script(root, sudo, btrfs_command)
    result = ssh.run("sh -c " + shlex.quote(script), check=False, log_stderr=False, mirror_stderr=False)
    index = BtrfsIndex(root=root, location="remote")
    if result.returncode != 0:
        text = result.stderr.strip() or result.stdout.strip() or f"return code {result.returncode}"
        if "No such file or directory" in text or "can't access" in text or "cannot access" in text:
            index.root_missing = True
        if required:
            index.errors.append(text)
        return index

    root_show: list[str] = []
    in_root_show = False
    current_list_root: str | None = None
    current_list_lines: list[str] = []

    def flush_list() -> None:
        nonlocal current_list_root, current_list_lines
        if current_list_root is not None:
            for meta in parse_subvolume_list("\n".join(current_list_lines), current_list_root):
                index.add(meta)
        current_list_root = None
        current_list_lines = []

    for line in result.stdout.splitlines():
        if line == "TSBTRFS_ROOT_SHOW_BEGIN":
            in_root_show = True
            continue
        if line == "TSBTRFS_ROOT_SHOW_END":
            in_root_show = False
            continue
        if in_root_show:
            root_show.append(line)
            continue
        if line.startswith("TSBTRFS_LIST_BEGIN\t"):
            flush_list()
            current_list_root = normalize_path(line.split("\t", 1)[1])
            continue
        if line.startswith("TSBTRFS_LIST_STATUS\t"):
            parts = line.split("\t")
            if len(parts) >= 3 and normalize_path(parts[1]) == root and parts[2] != "0":
                index.root_missing = True
                if required:
                    index.errors.append(f"remote index root is missing or not listable: {root}")
            continue
        if line.startswith("TSBTRFS_LIST_END\t"):
            flush_list()
            continue
        if current_list_root is not None:
            current_list_lines.append(line)
    flush_list()

    if include_root and root_show:
        root_meta = btrfs.parse_subvolume_show("\n".join(root_show), Path(root).name, root)
        if root_meta.uuid or root_meta.parent_uuid or root_meta.received_uuid or root_meta.readonly is not None:
            index.add(root_meta)
    return index


def refresh_remote_path(
    index: BtrfsIndex | None,
    ssh: SSHRunner,
    path: str | Path,
    *,
    name: str | None = None,
    sudo: str,
    btrfs_command: str,
) -> SubvolumeMeta | None:
    """Refresh one remote path in an existing index after create/delete-sensitive work."""

    meta = btrfs.get_subvolume_meta("remote", path, name or Path(path).name, sudo, btrfs_command, ssh=ssh, required=False)
    if index is not None:
        if meta:
            index.add(meta)
        else:
            index.discard(str(path))
    return meta


def refresh_local_path(
    index: BtrfsIndex | None,
    path: str | Path,
    *,
    name: str | None = None,
    sudo: str,
    btrfs_command: str,
) -> SubvolumeMeta | None:
    """Refresh one local path in an existing index after receive/delete-sensitive work."""

    meta = btrfs.get_subvolume_meta("local", path, name or Path(path).name, sudo, btrfs_command, required=False)
    if index is not None:
        if meta:
            index.add(meta)
        else:
            index.discard(str(path))
    return meta
