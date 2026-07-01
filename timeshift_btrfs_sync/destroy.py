"""Destructive leftover cleanup for removing a ts-btrfs setup."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import shlex
import subprocess

from . import btrfs
from . import payload_stats
from . import log as runlog
from . import state as state_mod
from .commands import quote_join, run_local, sudo_prefix
from .config import AppConfig
from .source import SourceRunner

PROTECTED_PATHS = {
    "/",
    "/home",
    "/mnt",
    "/media",
    "/var",
    "/run",
    "/tmp",
    "/usr",
    "/etc",
    "/root",
    "/boot",
}


@dataclass(slots=True)
class DestroyResult:
    """Result summary for one destructive cleanup root."""

    label: str
    path: str
    location: str
    exists: bool = False
    root_is_subvolume: bool = False
    subvolumes: list[str] = field(default_factory=list)
    deleted_subvolumes: int = 0
    removed_tree: bool = False
    removed_stale_dirs: int = 0
    removed_ordinary_files: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Return True when the target is gone or dry-run found no blocking errors."""

        return not self.errors


def _safe_cleanup_path(path: str | Path, label: str) -> str:
    """Return a normalized absolute path or raise for dangerous cleanup roots."""

    text = os.path.normpath(str(path).strip())
    if not text or text == "." or not text.startswith("/"):
        raise RuntimeError(f"Refusing unsafe {label} path; it must be absolute: {path!r}")
    if ".." in Path(text).parts:
        raise RuntimeError(f"Refusing unsafe {label} path containing '..': {text}")
    if text.rstrip("/") in PROTECTED_PATHS:
        raise RuntimeError(f"Refusing to destroy protected broad path for {label}: {text}")
    if len([part for part in Path(text).parts if part not in {"/", ""}]) < 2:
        raise RuntimeError(f"Refusing suspiciously broad {label} path: {text}")
    return text.rstrip("/")


def _listed_path_to_absolute(root_path: str, listed_path: str) -> str | None:
    """Convert a Btrfs-listed path back to an absolute path below root_path."""

    listed = os.path.normpath(listed_path.strip())
    if listed.startswith("/"):
        return listed if _is_under(listed, root_path) else None

    root_parts = [part for part in Path(root_path).parts if part not in {"/", ""}]
    listed_parts = [part for part in Path(listed).parts if part not in {"/", ""}]
    for index in range(len(root_parts)):
        suffix = root_parts[index:]
        if listed_parts[: len(suffix)] == suffix:
            absolute = "/" + "/".join(root_parts[:index] + listed_parts)
            return absolute if _is_under(absolute, root_path) else None
    return None


def _is_under(path: str, root: str) -> bool:
    """Return True when path is root or below root."""

    path_norm = os.path.normpath(path).rstrip("/")
    root_norm = os.path.normpath(root).rstrip("/")
    return path_norm == root_norm or path_norm.startswith(root_norm + "/")


def _sort_deepest_first(paths: list[str]) -> list[str]:
    """Return unique paths deepest first for safe Btrfs subvolume deletion."""

    return sorted(set(paths), key=lambda item: (item.count("/"), item), reverse=True)


def _collect_recursive_subvolumes(root_path: str, child_loader) -> list[str] | None:
    """List all descendant Btrfs subvolumes by walking one level at a time."""

    seen: set[str] = set()
    pending = [root_path]
    while pending:
        current = pending.pop(0)
        children = child_loader(current)
        if children is None:
            return None
        for child in children:
            if _is_under(child, root_path) and child not in seen:
                seen.add(child)
                pending.append(child)
    return _sort_deepest_first(list(seen))


def _run_quiet(cmd: list[str], *, env: dict[str, str] | None = None):
    """Run a probe/delete command quietly but record it in active run logs."""

    try:
        result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=env)
    except FileNotFoundError as exc:
        result = subprocess.CompletedProcess(cmd, 127, "", str(exc))
    logger = runlog.get_logger()
    if logger:
        logger.completed(cmd, result.returncode, result.stdout, result.stderr)
    return result


def _run_source_quiet(source: SourceRunner, source_command: str):
    """Run one source command quietly for structured destroy output."""

    return _run_quiet(source.command(source_command), env=source.environment())


def _path_exists_status(result) -> tuple[bool | None, str]:
    """Return True/False for test -e, or None when the check itself failed."""

    if result.returncode == 0:
        return True, ""
    if result.returncode == 1:
        return False, ""
    return None, result.stderr.strip() or result.stdout.strip() or f"return code {result.returncode}"


def _local_exists(path: str, sudo: str) -> tuple[bool | None, str]:
    """Return local path existence status, using sudo if configured."""

    return _path_exists_status(_run_quiet(sudo_prefix(sudo) + ["test", "-e", path]))


def _source_exists(source: SourceRunner, path: str, sudo: str) -> tuple[bool | None, str]:
    """Return source shell path existence status without sudo.

    This is a fallback probe only.  The source sudoers model intentionally
    stays narrow, so the app cannot rely on sudo ``test``.  Destructive
    source-cache cleanup first asks ``sudo btrfs`` whether the path is a real
    Btrfs object, then uses this shell check only to distinguish ordinary
    user-visible directories from missing paths.
    """

    result = _run_source_quiet(source, "test -e " + shlex.quote(path))
    return _path_exists_status(result)


def _source_destroy_path_status(
    source: SourceRunner,
    path: str,
    sudo: str,
    btrfs_command: str,
) -> tuple[bool | None, bool, str]:
    """Return source path existence using Btrfs first, then shell fallback.

    ``destroy-leftovers --delete-source`` is only allowed to use source sudo for
    ``btrfs``.  Earlier builds checked the remote source cache root with plain
    ``test -e`` first.  On SSH sources that could report "missing" even when
    ``sudo btrfs subvolume show`` could see the app-owned cache subvolume, so
    the command skipped deletion and printed a false success.

    Return ``(exists, is_subvolume, detail)`` where ``exists`` is ``None`` only
    when neither the Btrfs probes nor the shell fallback can determine the
    status.
    """

    meta = _source_subvolume_meta(source, path, sudo, btrfs_command)
    if meta is not None:
        return True, True, "exists as Btrfs subvolume"

    df_result = _run_source_quiet(
        source,
        btrfs.remote_btrfs_cmd(sudo, btrfs_command, ["filesystem", "df", path]),
    )
    if df_result.returncode == 0:
        return True, False, "exists on Btrfs filesystem"

    shell_exists, shell_error = _source_exists(source, path, sudo)
    if shell_exists is True:
        return True, False, "exists according to source shell"
    if shell_exists is False:
        return False, False, ""

    btrfs_error = df_result.stderr.strip() or df_result.stdout.strip() or f"return code {df_result.returncode}"
    if shell_error:
        return None, False, f"btrfs access failed: {btrfs_error}; shell check failed: {shell_error}"
    return None, False, f"btrfs access failed: {btrfs_error}"


def _local_subvolume_meta(path: str, sudo: str, btrfs_command: str):
    """Return local Btrfs subvolume metadata, or None for ordinary/missing paths."""

    result = _run_quiet(btrfs.local_btrfs_cmd(sudo, btrfs_command, ["subvolume", "show", path]))
    return btrfs.parse_subvolume_show(result.stdout, Path(path).name, path) if result.returncode == 0 else None


def _source_subvolume_meta(source: SourceRunner, path: str, sudo: str, btrfs_command: str):
    """Return source Btrfs subvolume metadata, or None for ordinary/missing paths."""

    result = _run_source_quiet(source, btrfs.remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "show", path]))
    return btrfs.parse_subvolume_show(result.stdout, Path(path).name, path) if result.returncode == 0 else None


def _local_child_subvolumes(path: str, sudo: str, btrfs_command: str) -> list[str] | None:
    """Return absolute local child subvolume paths below path."""

    result = _run_quiet(btrfs.local_btrfs_cmd(sudo, btrfs_command, ["subvolume", "list", "-o", path]))
    if result.returncode != 0:
        return None
    converted = [_listed_path_to_absolute(path, item) for item in btrfs._subvolume_list_paths(result.stdout)]
    return [item for item in converted if item]


def _source_child_subvolumes(source: SourceRunner, path: str, sudo: str, btrfs_command: str) -> list[str] | None:
    """Return absolute source child subvolume paths below path."""

    result = _run_source_quiet(source, btrfs.remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "list", "-o", path]))
    if result.returncode != 0:
        return None
    converted = [_listed_path_to_absolute(path, item) for item in btrfs._subvolume_list_paths(result.stdout)]
    return [item for item in converted if item]


def _source_cache_layout_subvolumes_batched(
    source: SourceRunner,
    path: str,
    sudo: str,
    btrfs_command: str,
    *,
    protected_snapshot_root: str | None = None,
) -> tuple[list[str] | None, list[str]]:
    """Discover app-owned source cache subvolumes by the cache directory layout.

    ``btrfs subvolume list`` output is relative to the Btrfs filesystem root.
    On SSH sources with bind mounts, unusual mount roots, or different printed
    paths, converting that output back to absolute paths can fail and produce an
    empty deletion plan. The source send-cache layout is simpler and
    app-owned: ``cache_root/<snapshot>/<subvolume>`` plus the per-snapshot
    container subvolume. This fallback runs in one source shell and asks Btrfs
    directly whether each candidate directory is a subvolume.

    It uses only the configured source shell user plus sudo for ``btrfs``. It
    does not use sudo ``find``, ``rm``, ``mkdir``, ``chown``, or ``chmod``.
    """

    protected = (protected_snapshot_root or "").rstrip("/")
    sudo_words = " ".join(shlex.quote(part) for part in sudo_prefix(sudo))
    btrfs_q = shlex.quote(btrfs_command)
    script = f"""
root={shlex.quote(path)}
protected={shlex.quote(protected)}
sudo_words={shlex.quote(sudo_words)}
btrfs_cmd={btrfs_q}
run_btrfs() {{
    if [ -n "$sudo_words" ]; then
        # shellcheck disable=SC2086
        $sudo_words "$btrfs_cmd" "$@"
    else
        "$btrfs_cmd" "$@"
    fi
}}
is_protected() {{
    p=$1
    [ -n "$protected" ] || return 1
    [ "$p" = "$protected" ] && return 0
    case "$p" in
        "$protected"/*) return 0 ;;
        *) return 1 ;;
    esac
}}
is_subvol() {{
    run_btrfs subvolume show "$1" >/dev/null 2>&1
}}
emit_subvol() {{
    candidate=$1
    [ -d "$candidate" ] || return 0
    if is_protected "$candidate"; then
        echo "TSBTRFS_PROTECTED	$candidate"
        return 0
    fi
    if is_subvol "$candidate"; then
        echo "TSBTRFS_SUBVOL	$candidate"
    fi
}}
if [ ! -e "$root" ]; then
    echo "TSBTRFS_MISSING	$root"
    exit 0
fi
if is_protected "$root"; then
    echo "TSBTRFS_PROTECTED	$root"
    exit 0
fi
for snap in "$root"/* "$root"/.[!.]* "$root"/..?*; do
    [ -e "$snap" ] || continue
    [ -d "$snap" ] || continue
    # Payload/cache child subvolumes such as @ and @home must be deleted
    # before the per-snapshot cache container subvolume.
    for child in "$snap"/* "$snap"/.[!.]* "$snap"/..?*; do
        [ -e "$child" ] || continue
        [ -d "$child" ] || continue
        emit_subvol "$child"
    done
    emit_subvol "$snap"
done
emit_subvol "$root"
""".strip()
    result = _run_source_quiet(source, "sh -c " + shlex.quote(script))
    if result.returncode != 0:
        return None, [result.stderr.strip() or result.stdout.strip() or f"return code {result.returncode}"]
    paths: list[str] = []
    errors: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("TSBTRFS_SUBVOL\t"):
            paths.append(line.split("\t", 1)[1])
        elif line.startswith("TSBTRFS_PROTECTED\t"):
            errors.append(
                "refusing to delete Timeshift-owned source.snapshot_root path discovered in source cache layout: "
                + line.split("\t", 1)[1]
            )
    return _sort_deepest_first(paths), errors


def _local_remove_timeshift_metadata_files_in_destination_tree(path: str) -> int:
    """Remove copied Timeshift metadata files from destination date folders.

    This is deliberately narrow: it removes only ordinary files that are direct
    children of ``snapshots/<date>/`` folders, such as ``info.json``. It does
    not recurse into payload subvolumes like ``@`` or ``@home``.
    """

    root = Path(path)
    candidates: list[Path] = []
    if root.name == "snapshots":
        candidates.append(root)
    candidate = root / "snapshots"
    if candidate.exists():
        candidates.append(candidate)

    removed = 0
    seen: set[Path] = set()
    for snapshots_root in candidates:
        if snapshots_root in seen or not snapshots_root.exists() or not snapshots_root.is_dir():
            continue
        seen.add(snapshots_root)
        try:
            date_dirs = [entry for entry in snapshots_root.iterdir() if entry.is_dir()]
        except OSError:
            continue
        for date_dir in date_dirs:
            try:
                children = list(date_dir.iterdir())
            except OSError:
                continue
            for child in children:
                try:
                    if child.is_file() or child.is_symlink():
                        child.unlink()
                        removed += 1
                except OSError:
                    pass
    return removed


def _local_remove_empty_child_dirs(path: str, sudo: str) -> int:
    """Remove empty ordinary directories below a local path, deepest first.

    Deleting a nested Btrfs subvolume can leave an ordinary directory entry
    behind at the former mountpoint. Parent subvolume deletion may then fail
    with ``Directory not empty`` even though all child subvolumes are gone. Walk
    ordinary directories deepest-first and remove only empty directories before
    deleting the parent subvolume. Non-empty directories are left untouched and
    reported by the later Btrfs delete error.
    """

    removed = 0
    root = Path(path)
    try:
        directories = [entry for entry in root.rglob("*") if entry.is_dir()]
    except OSError:
        return 0
    for entry in sorted(directories, key=lambda item: (len(item.parts), str(item)), reverse=True):
        try:
            entry.rmdir()
            removed += 1
        except OSError:
            pass
    return removed


def _local_remove_stale_path(path: str, sudo: str) -> bool:
    """Remove an ordinary empty directory left behind after local subvolume delete."""

    exists, _ = _local_exists(path, sudo)
    if not exists:
        return False
    try:
        Path(path).rmdir()
        return True
    except OSError:
        result = _run_quiet(sudo_prefix(sudo) + ["rmdir", "--", path])
        return result.returncode == 0


def _confirm_or_raise(prompt: str, expected: str) -> None:
    """Require an exact typed confirmation."""

    answer = input(prompt).strip()
    if answer != expected:
        raise RuntimeError("Confirmation did not match; destructive cleanup aborted")


def _delete_local_tree(path: str, sudo: str, btrfs_command: str, *, dry_run: bool, label: str) -> DestroyResult:
    """Delete one local tree after deleting nested Btrfs subvolumes deepest-first.

    Destination cleanup intentionally deletes child payload subvolumes first,
    then performs a second ordinary-file/empty-directory cleanup pass before
    deleting ``destination.target_root`` itself. Copied Timeshift metadata such
    as ``info.json`` is an ordinary file beside received ``@``/``@home``
    subvolumes, so it must be removed before the date folder or target root can
    become empty.
    """

    result = DestroyResult(label=label, path=path, location="destination")
    print(f"  checking destination path existence: {path}", flush=True)
    exists, exists_error = _local_exists(path, sudo)
    if exists is None:
        result.errors.append(f"could not check local path existence: {exists_error}")
        return result
    result.exists = exists
    if not result.exists:
        return result

    print(f"  discovering destination Btrfs subvolumes below: {path}", flush=True)
    meta = _local_subvolume_meta(path, sudo, btrfs_command)
    result.root_is_subvolume = meta is not None
    children = _collect_recursive_subvolumes(path, lambda current: _local_child_subvolumes(current, sudo, btrfs_command))
    if children is None:
        if result.root_is_subvolume:
            result.errors.append("could not recursively list local child subvolumes")
            return result
        children = []

    child_subvolumes = [item for item in _sort_deepest_first(children) if os.path.normpath(item) != os.path.normpath(path)]
    root_subvolume = path if result.root_is_subvolume else None
    result.subvolumes = child_subvolumes + ([root_subvolume] if root_subvolume else [])
    print(f"  discovered destination subvolumes: {len(result.subvolumes)}", flush=True)
    if dry_run:
        return result

    # First pass: remove copied Timeshift metadata before deleting payload
    # subvolumes. This catches existing info.json files early, but a second
    # pass is still needed after child subvolumes are gone because Btrfs can
    # leave ordinary empty mountpoint directories behind.
    result.removed_ordinary_files += _local_remove_timeshift_metadata_files_in_destination_tree(path)

    print(f"  deleting destination child subvolumes deepest-first: {len(child_subvolumes)}", flush=True)
    for subvol in child_subvolumes:
        result.removed_stale_dirs += _local_remove_empty_child_dirs(subvol, sudo)
        try:
            btrfs.delete_local_subvolume(Path(subvol), sudo, btrfs_command)
            result.deleted_subvolumes += 1
            if _local_remove_stale_path(subvol, sudo):
                result.removed_stale_dirs += 1
        except Exception as exc:
            result.errors.append(f"failed deleting local subvolume {subvol}: {exc}")

    # Second pass requested by the cleanup safety model:
    # after all child subvolumes are deleted, remove any copied Timeshift
    # metadata files and stale empty directories again before deleting the
    # configured destination.target_root subvolume.
    print("  cleaning destination metadata and empty directories before target root delete", flush=True)
    result.removed_ordinary_files += _local_remove_timeshift_metadata_files_in_destination_tree(path)
    result.removed_stale_dirs += _local_remove_empty_child_dirs(path, sudo)

    if root_subvolume:
        print("  deleting destination target_root subvolume", flush=True)
        try:
            btrfs.delete_local_subvolume(Path(root_subvolume), sudo, btrfs_command)
            result.deleted_subvolumes += 1
            if _local_remove_stale_path(root_subvolume, sudo):
                result.removed_stale_dirs += 1
        except Exception as exc:
            result.errors.append(f"failed deleting local subvolume {root_subvolume}: {exc}")

    if not result.root_is_subvolume and Path(path).exists():
        rm = _run_quiet(sudo_prefix(sudo) + ["rm", "-rf", "--", path])
        if rm.returncode == 0:
            result.removed_tree = True
        else:
            result.errors.append(f"failed removing local directory tree {path}: {rm.stderr.strip() or rm.stdout.strip()}")
    return result



def _source_delete_subvolumes_batched(
    source: SourceRunner,
    paths: list[str],
    sudo: str,
    btrfs_command: str,
    *,
    protected_snapshot_root: str | None = None,
) -> tuple[int, int, list[str]]:
    """Delete many source subvolumes in one source command.

    Refuse the entire batch if any path is source.snapshot_root or below it.
    This keeps Timeshift-owned snapshots protected even if a bad config or stale
    state accidentally passes them to destroy-leftovers.
    """

    if not paths:
        return 0, 0, []
    protected = [path for path in paths if btrfs.path_is_same_or_under(path, protected_snapshot_root)]
    if protected:
        return 0, 0, [
            "refusing to delete Timeshift-owned source.snapshot_root path(s): "
            + ", ".join(protected)
            + f"; protected root: {protected_snapshot_root}"
        ]
    sudo_words = " ".join(shlex.quote(part) for part in sudo_prefix(sudo))
    btrfs_q = shlex.quote(btrfs_command)
    path_lines = "\n".join(paths)
    script = f"""
sudo_words={shlex.quote(sudo_words)}
btrfs_cmd={btrfs_q}
run_btrfs() {{
    if [ -n "$sudo_words" ]; then
        # shellcheck disable=SC2086
        $sudo_words "$btrfs_cmd" "$@"
    else
        "$btrfs_cmd" "$@"
    fi
}}
remove_empty_child_dirs() {{
    base=$1
    removed=0
    # Remove ordinary empty directory entries left behind by deleted nested
    # subvolumes. This uses only the source user's normal rmdir permission;
    # no sudo mkdir/rm/find/chown/chmod permission is needed.
    while :; do
        changed=0
        for child in "$base"/* "$base"/.[!.]* "$base"/..?*; do
            [ -e "$child" ] || continue
            [ -d "$child" ] || continue
            if rmdir -- "$child" 2>/dev/null; then
                removed=$((removed + 1))
                changed=1
            fi
        done
        [ "$changed" -eq 1 ] || break
    done
    printf '%s' "$removed"
}}
while IFS= read -r subvol; do
    [ -n "$subvol" ] || continue
    stale_count=$(remove_empty_child_dirs "$subvol")
    output=$(run_btrfs subvolume delete "$subvol" 2>&1)
    status=$?
    if [ "$status" -eq 0 ]; then
        echo "TSBTRFS_DELETED	$subvol	$stale_count"
        if [ -e "$subvol" ]; then
            if rmdir -- "$subvol" >/dev/null 2>&1; then
                echo "TSBTRFS_STALE_REMOVED	$subvol"
            else
                echo "TSBTRFS_STALE_LEFT	$subvol"
            fi
        fi
    else
        safe_output=$(printf '%s' "$output" | tr '\n' ' ')
        echo "TSBTRFS_DELETE_ERROR	$subvol	$safe_output"
    fi
done <<'TSBTRFS_PATHS'
{path_lines}
TSBTRFS_PATHS
""".strip()
    result = _run_source_quiet(source, "sh -c " + shlex.quote(script))
    deleted = 0
    stale_removed = 0
    errors: list[str] = []
    if result.returncode != 0:
        errors.append(result.stderr.strip() or result.stdout.strip() or f"return code {result.returncode}")
        return deleted, stale_removed, errors
    for line in result.stdout.splitlines():
        if line.startswith("TSBTRFS_DELETED\t"):
            _tag, subvol, stale = line.split("\t", 2)
            deleted += 1
            try:
                stale_removed += int(stale)
            except ValueError:
                pass
        elif line.startswith("TSBTRFS_STALE_REMOVED\t"):
            stale_removed += 1
        elif line.startswith("TSBTRFS_STALE_LEFT\t"):
            _tag, subvol = line.split("\t", 1)
            errors.append(
                f"ordinary directory remained after source subvolume delete {subvol}; "
                "source user could not remove it without sudo"
            )
        elif line.startswith("TSBTRFS_DELETE_ERROR\t"):
            _tag, subvol, detail = line.split("\t", 2)
            errors.append(f"failed deleting source subvolume {subvol}: {detail}")
    return deleted, stale_removed, errors

def _delete_source_tree(
    source: SourceRunner,
    path: str,
    sudo: str,
    btrfs_command: str,
    *,
    dry_run: bool,
    label: str,
    protected_snapshot_root: str | None = None,
) -> DestroyResult:
    """Delete one source tree after deleting nested Btrfs subvolumes deepest-first.

    source.snapshot_root is Timeshift-owned and must never be removed by this
    app. Only the app-owned source.cache_root may be targeted here.
    """

    result = DestroyResult(label=label, path=path, location="source")
    if btrfs.path_is_same_or_under(path, protected_snapshot_root):
        result.errors.append(
            f"refusing to destroy Timeshift-owned source.snapshot_root path {path}; "
            f"protected root: {protected_snapshot_root}"
        )
        return result

    # Source-side sudoers should only need passwordless timeshift/btrfs.  Check
    # existence with sudo btrfs first, because source.cache_root is expected to
    # be an app-owned Btrfs subvolume and a plain remote ``test -e`` can be a
    # false negative on SSH sources.  The shell check is only a fallback for
    # ordinary user-visible leftovers.
    print(f"  checking source path existence with Btrfs first: {path}", flush=True)
    exists, is_subvolume, exists_detail = _source_destroy_path_status(source, path, sudo, btrfs_command)
    if exists is None:
        result.errors.append(f"could not check source path existence: {exists_detail}")
        return result
    result.exists = exists
    result.root_is_subvolume = is_subvolume
    if not result.exists:
        return result
    print(f"  source path status: {exists_detail}", flush=True)

    print(f"  discovering source Btrfs subvolumes below: {path}", flush=True)
    children = _collect_recursive_subvolumes(
        path,
        lambda current: _source_child_subvolumes(source, current, sudo, btrfs_command),
    )
    if children is None:
        if result.root_is_subvolume:
            result.errors.append("could not recursively list source child subvolumes with btrfs subvolume list -o")
            return result
        children = []

    layout_children, layout_errors = _source_cache_layout_subvolumes_batched(
        source,
        path,
        sudo,
        btrfs_command,
        protected_snapshot_root=protected_snapshot_root,
    )
    result.errors.extend(layout_errors)
    if layout_children is None:
        layout_children = []

    # Merge the generic Btrfs index with the cache-layout fallback. The fallback
    # is important when SSH/Btrfs prints paths that cannot be converted back to
    # absolute paths by the destination-side parser; an empty plan must not be
    # considered a successful source cleanup while cache snapshots remain.
    result.subvolumes = _sort_deepest_first(children + layout_children + ([path] if result.root_is_subvolume else []))
    print(f"  discovered source subvolumes: {len(result.subvolumes)}", flush=True)
    if dry_run:
        return result

    print(f"  deleting source subvolumes deepest-first in one source command: {len(result.subvolumes)}", flush=True)
    deleted, stale_removed, errors = _source_delete_subvolumes_batched(
        source,
        result.subvolumes,
        sudo,
        btrfs_command,
        protected_snapshot_root=protected_snapshot_root,
    )
    result.deleted_subvolumes = deleted
    result.removed_stale_dirs += stale_removed
    result.errors.extend(errors)

    exists_after, _is_subvolume_after, after_detail = _source_destroy_path_status(source, path, sudo, btrfs_command)
    if not result.root_is_subvolume and exists_after:
        if btrfs.path_is_same_or_under(path, protected_snapshot_root):
            result.errors.append(
                f"refusing to remove Timeshift-owned source directory tree {path}; "
                f"protected root: {protected_snapshot_root}"
            )
            return result
        rm = _run_source_quiet(source, quote_join(["rm", "-rf", "--", path]))
        if rm.returncode == 0:
            result.removed_tree = True
        else:
            result.errors.append(
                f"failed removing source directory tree {path} without sudo: "
                f"{rm.stderr.strip() or rm.stdout.strip()}"
            )

    # Final verification is mandatory. A source-cache cleanup that discovers no
    # subvolumes or cannot delete them must not report success while the
    # app-owned cache root still exists.  Verify with Btrfs first so SSH source
    # cleanup cannot falsely report success from a plain shell ``test -e`` miss.
    exists_final, _is_subvolume_final, final_detail = _source_destroy_path_status(source, path, sudo, btrfs_command)
    if exists_final:
        result.errors.append(
            f"source cleanup incomplete; path still exists after destroy attempt: {path} ({final_detail}). "
            "The delete plan may not have discovered the source cache subvolumes, "
            "or the source user could not remove stale ordinary directories/files."
        )
    elif exists_final is None:
        result.errors.append(f"could not verify source path was removed after cleanup: {final_detail}")
    return result


def _mode_text(delete_source: bool, delete_destination: bool) -> str:
    """Return uppercase confirmation text for the selected destroy mode."""

    if delete_source and delete_destination:
        return "DELETE BOTH"
    if delete_source:
        return "DELETE SOURCE"
    return "DELETE DESTINATION"


def _print_target(label: str, path: str) -> None:
    """Print one destroy target path."""

    print(f"{label}:")
    print(f"  {path}")


def _print_result(result: DestroyResult, *, dry_run: bool) -> None:
    """Print one target cleanup result."""

    action = "would delete" if dry_run else "deleted"
    print(f"{result.label}:")
    print(f"  path:       {result.path}")
    if not result.exists:
        print("  result:     already missing")
        return
    print(f"  subvolumes: {len(result.subvolumes)}")
    if dry_run:
        for path in result.subvolumes:
            print(f"    would delete subvolume: {path}")
        print(f"  result:     {action} ordinary files/directories after subvolumes")
        return
    print(f"  deleted subvolumes: {result.deleted_subvolumes}")
    print(f"  removed ordinary files: {result.removed_ordinary_files}")
    print(f"  removed stale directories: {result.removed_stale_dirs}")
    print(f"  removed ordinary tree: {'yes' if result.removed_tree or result.root_is_subvolume else 'no'}")
    if result.errors:
        print("  result:     incomplete")
        for error in result.errors:
            print(f"    error: {error}")
    else:
        print("  result:     complete")


def _result_by_label(results: list[DestroyResult], label: str) -> DestroyResult | None:
    """Return a result by printed target label."""

    for result in results:
        if result.label == label and result.exists:
            return result
    return None


def _load_payload_state(config: AppConfig) -> dict | None:
    """Load state for payload explanation only, or None when unavailable.

    destroy-leftovers deliberately does not use state.json to decide what to
    delete. This read is only for explaining normalized source/destination
    payload statistics, especially when v0.1.2 direct read-only Timeshift sends
    mean valid source payload may live outside source.cache_root.
    """

    try:
        return state_mod.load_state(config.state_file, config.destination.target_root)
    except Exception:
        return None


def _print_payload_match_if_available(config: AppConfig, results: list[DestroyResult], state_doc: dict | None) -> None:
    """Print normalized source/destination payload counts when both sides were selected."""

    source = _result_by_label(results, "Source send-cache root")
    destination = _result_by_label(results, "Destination target_root")
    if source is None or destination is None:
        return
    cache_stats = payload_stats.source_send_cache_stats(source.path, source.subvolumes, config.source.subvolumes)
    direct_stats = None
    if state_doc is not None:
        direct_stats = payload_stats.direct_send_payload_stats(
            state_doc,
            config.source.subvolumes,
            cache_root=config.source.cache_root,
        )
    source_stats = payload_stats.merge_source_payload_stats(cache_stats, direct_stats)
    destination_stats = payload_stats.destination_payload_stats(destination.path, destination.subvolumes, config.source.subvolumes)
    for line in payload_stats.render_payload_match(payload_stats.compare_payloads(source_stats, destination_stats)):
        print(line)
    print()


def destroy_leftovers(
    config: AppConfig,
    *,
    delete_source: bool,
    delete_destination: bool,
    dry_run: bool,
    danger_confirmed: bool,
    interactive: bool = True,
) -> list[DestroyResult]:
    """Destroy configured source/destination leftovers for retiring this app setup."""

    if not delete_source and not delete_destination:
        raise RuntimeError("Choose exactly one of --delete-source, --delete-destination, or --delete-both")

    targets: list[tuple[str, str, str]] = []
    if delete_source:
        if not config.source.cache_root:
            raise RuntimeError("--delete-source requires source.cache_root; source.snapshot_root is Timeshift-owned and is never destroyed")
        if btrfs.path_is_same_or_under(config.source.cache_root, config.source.snapshot_root):
            raise RuntimeError(
                "Refusing --delete-source because source.cache_root is source.snapshot_root or below it. "
                "source.snapshot_root is Timeshift-owned and must never be deleted, pruned, destroyed, or cleaned by this app."
            )
        targets.append(("Source send-cache root", _safe_cleanup_path(config.source.cache_root, "source.cache_root"), "source"))
    if delete_destination:
        targets.append(("Destination target_root", _safe_cleanup_path(config.destination.target_root, "destination.target_root"), "destination"))

    mode_text = _mode_text(delete_source, delete_destination)
    print("DESTRUCTIVE LEFTOVER CLEANUP")
    print("============================")
    print("This command is for permanently removing a ts-btrfs setup.")
    print("It ignores state.json and retention rules.")
    print("It recursively deletes Btrfs subvolumes and leftover files/directories.")
    print("It only deletes app-created source send-cache paths when --delete-source is used.")
    print("It must never delete, prune, destroy, or clean source.snapshot_root or anything below it.")
    print()
    print(f"Run mode: {'dry-run' if dry_run else 'REAL DELETION'}")
    print(f"Selected mode: {mode_text}")
    print(f"Configured job: {config.name}")
    print()
    for label, path, _ in targets:
        _print_target(label, path)
    print()

    if not dry_run:
        if not danger_confirmed:
            raise RuntimeError("Real destroy-leftovers requires --i-understand-this-destroys-data")
        if interactive:
            _confirm_or_raise(f"Type {mode_text} to continue: ", mode_text)
            _confirm_or_raise(f"Type the configured job name ({config.name}) to continue: ", config.name)

    source = SourceRunner.from_config(config) if delete_source else None
    payload_state = _load_payload_state(config) if delete_source and delete_destination else None

    results: list[DestroyResult] = []
    print("DESTROY PLAN" if dry_run else "DESTROY EXECUTION")
    print("============" if dry_run else "=================")
    for label, path, location in targets:
        print(f"Starting cleanup target: {label}", flush=True)
        print(f"  location: {location}", flush=True)
        print(f"  path:     {path}", flush=True)
        if location == "source":
            assert source is not None
            result = _delete_source_tree(
                source,
                path,
                config.source.sudo,
                config.source.btrfs_command,
                dry_run=dry_run,
                label=label,
                protected_snapshot_root=config.source.snapshot_root,
            )
        else:
            result = _delete_local_tree(
                path,
                config.destination.sudo,
                config.destination.btrfs_command,
                dry_run=dry_run,
                label=label,
            )
        results.append(result)
        _print_result(result, dry_run=dry_run)
        print()

    _print_payload_match_if_available(config, results, payload_state)

    failures = [result for result in results if result.errors]
    print("DESTROY SUMMARY")
    print("===============")
    print(f"  targets:    {len(results)}")
    print(f"  complete:   {len(results) - len(failures)}")
    print(f"  incomplete: {len(failures)}")
    if failures:
        raise RuntimeError("destroy-leftovers finished with incomplete target cleanup; inspect errors above and rerun after fixing them")
    return results
