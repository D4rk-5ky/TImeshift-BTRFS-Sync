"""Btrfs command builders and parsers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from .commands import quote_join, run_local, sudo_prefix
from .models import SubvolumeMeta
from .ssh import SSHRunner
from .source import SourceRunner

if TYPE_CHECKING:
    from .remote_index import BtrfsIndex

UUID_KEYS = {"UUID": "uuid", "Parent UUID": "parent_uuid", "Received UUID": "received_uuid"}


def _clean_uuid(value: str) -> str | None:
    """Normalize Btrfs UUID fields."""

    value = value.strip()
    return None if not value or value == "-" else value


def parse_subvolume_show(output: str, name: str, path: str) -> SubvolumeMeta:
    """Parse UUIDs and read-only state from `btrfs subvolume show`."""

    meta = SubvolumeMeta(name=name, path=path)
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        attr = UUID_KEYS.get(key)
        if attr:
            setattr(meta, attr, _clean_uuid(value))
            continue

        if key.lower() == "flags":
            lower_value = value.lower()
            if "readonly" in lower_value or "read-only" in lower_value:
                meta.readonly = True
            elif lower_value in {"-", "none", ""}:
                meta.readonly = False
    return meta


def remote_btrfs_cmd(sudo: str, btrfs_command: str, args: list[str]) -> str:
    """Build a quoted source-side shell command that invokes sudo+btrfs only."""

    return quote_join(sudo_prefix(sudo) + [btrfs_command] + args)


def local_btrfs_cmd(sudo: str, btrfs_command: str, args: list[str]) -> list[str]:
    """Build a local btrfs argv list."""

    return sudo_prefix(sudo) + [btrfs_command] + args


def get_subvolume_meta(
    location: str,
    path: str | Path,
    name: str,
    sudo: str,
    btrfs_command: str = "btrfs",
    *,
    ssh: SSHRunner | None = None,
    required: bool = True,
) -> SubvolumeMeta | None:
    """Read and parse `btrfs subvolume show` metadata locally or over SSH."""

    path_text = str(path)
    args = ["subvolume", "show", path_text]
    if location == "local":
        result = run_local(local_btrfs_cmd(sudo, btrfs_command, args), check=False)
    elif location == "remote" and ssh:
        result = ssh.run(remote_btrfs_cmd(sudo, btrfs_command, args), check=False)
    else:
        raise ValueError("location must be 'local' or 'remote' with ssh")

    if result.returncode == 0:
        return parse_subvolume_show(result.stdout, name=name, path=path_text)
    if not required:
        return None
    details = result.stderr.strip() or result.stdout.strip() or f"return code {result.returncode}"
    raise RuntimeError(f"Cannot read {location} Btrfs subvolume metadata for {path_text}: {details}")




def source_get_subvolume_meta(
    source: SourceRunner,
    path: str | Path,
    name: str,
    sudo: str,
    btrfs_command: str = "btrfs",
    *,
    required: bool = True,
) -> SubvolumeMeta | None:
    """Read and parse source-side ``btrfs subvolume show`` metadata.

    In SSH mode the command is wrapped in ssh. In local source mode the same
    sudo+btrfs command is executed locally through ``sh -c``.
    """

    path_text = str(path)
    result = source.run(
        remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "show", path_text]),
        check=False,
    )
    if result.returncode == 0:
        return parse_subvolume_show(result.stdout, name=name, path=path_text)
    if not required:
        return None
    details = result.stderr.strip() or result.stdout.strip() or f"return code {result.returncode}"
    raise RuntimeError(f"Cannot read {source.location} Btrfs subvolume metadata for {path_text}: {details}")


def _validate_cache_snapshot_name(snapshot_name: str) -> str:
    """Reject unsafe cache snapshot names."""

    if not snapshot_name or "/" in snapshot_name or snapshot_name in {".", ".."}:
        raise RuntimeError(f"Unsafe snapshot name for cache path: {snapshot_name!r}")
    return snapshot_name


def _validate_cache_subvolume_name(subvolume_name: str) -> str:
    """Reject unsafe cache subvolume names."""

    if not subvolume_name or "/" in subvolume_name or subvolume_name in {".", ".."}:
        raise RuntimeError(f"Unsafe subvolume name for cache path: {subvolume_name!r}")
    return subvolume_name


def readonly_cache_parent_path(cache_root: str, snapshot_name: str) -> str:
    """Return <cache_root>/<snapshot-name>."""

    return str(Path(cache_root) / _validate_cache_snapshot_name(snapshot_name))


def readonly_cache_path(cache_root: str, snapshot_name: str, subvolume_name: str) -> str:
    """Return Timeshift-like source read-only cache path."""

    return str(Path(readonly_cache_parent_path(cache_root, snapshot_name)) / _validate_cache_subvolume_name(subvolume_name))


def _subvolume_list_paths(output: str) -> list[str]:
    """Extract path fields from `btrfs subvolume list` output."""

    paths: list[str] = []
    for line in output.splitlines():
        _before, sep, after = line.strip().partition(" path ")
        if sep:
            paths.append(after.strip().rstrip("/"))
    return paths


def _cache_path_suffixes(cache_root: str, path: str) -> set[str]:
    """Possible suffixes for matching absolute cache paths to Btrfs-list paths."""

    root = Path(cache_root)
    target = Path(path)
    try:
        rel = str(target.relative_to(root)).strip("/")
    except ValueError:
        rel = target.name

    parts = [part for part in root.parts if part not in {"/", ""}]
    suffixes = {str(target).strip("/"), rel}
    suffixes.update(f"{'/'.join(parts[index:])}/{rel}".strip("/") for index in range(len(parts)))
    return {suffix.rstrip("/") for suffix in suffixes if suffix}


def _listed_cache_path_matches(listed_path: str, cache_root: str, path: str) -> bool:
    listed = listed_path.strip("/").rstrip("/")
    return any(listed == suffix or listed.endswith("/" + suffix) for suffix in _cache_path_suffixes(cache_root, path))


def source_list_child_subvolumes(
    source: SourceRunner,
    *,
    sudo: str,
    btrfs_command: str,
    path: str,
) -> list[str] | None:
    """Return child subvolume paths from source ``btrfs subvolume list -o``."""

    result = source.run(
        remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "list", "-o", path]),
        check=False,
    )
    return None if result.returncode != 0 else _subvolume_list_paths(result.stdout)


def source_cache_existing_paths(
    source: SourceRunner,
    *,
    sudo: str,
    btrfs_command: str,
    cache_root: str | None,
    paths: list[str],
) -> set[str] | None:
    """Return requested cache paths that currently exist as source cache subvolumes."""

    if not cache_root:
        return set()
    candidates = [path for path in paths if path_is_under_cache(path, cache_root)]
    if not candidates:
        return set()
    listed_paths = source_list_child_subvolumes(source, sudo=sudo, btrfs_command=btrfs_command, path=cache_root)
    if listed_paths is None:
        return None
    return {
        path
        for path in candidates
        if any(_listed_cache_path_matches(listed, cache_root, path) for listed in listed_paths)
    }


def source_cache_existing_child_paths(
    source: SourceRunner,
    *,
    sudo: str,
    btrfs_command: str,
    cache_root: str | None,
    parent_path: str,
    paths: list[str],
) -> set[str] | None:
    """Return requested nested cache subvolumes found below one source cache parent."""

    if not cache_root or not path_is_under_cache(parent_path, cache_root):
        return set()
    candidates = [path for path in paths if path_is_under_cache(path, parent_path)]
    if not candidates:
        return set()
    listed_paths = source_list_child_subvolumes(source, sudo=sudo, btrfs_command=btrfs_command, path=parent_path)
    if listed_paths is None:
        return None
    return {
        path
        for path in candidates
        if any(_listed_cache_path_matches(listed, cache_root, path) for listed in listed_paths)
    }


def source_cache_contains(
    source: SourceRunner,
    sudo: str,
    btrfs_command: str,
    cache_root: str | None,
    path: str,
) -> bool:
    """Return True when the configured source cache contains ``path`` as a subvolume."""

    existing = source_cache_existing_paths(
        source, sudo=sudo, btrfs_command=btrfs_command, cache_root=cache_root, paths=[path]
    )
    return bool(existing)


def source_cache_is_empty(
    source: SourceRunner,
    *,
    sudo: str,
    btrfs_command: str,
    cache_root: str | None,
    path: str,
) -> bool | None:
    """Return True/False for source cache child subvolumes, or None when verification fails."""

    if not cache_root or not path_is_under_cache(path, cache_root):
        return None
    listed_paths = source_list_child_subvolumes(source, sudo=sudo, btrfs_command=btrfs_command, path=path)
    return None if listed_paths is None else not listed_paths


def remote_list_child_subvolumes(
    ssh: SSHRunner,
    *,
    sudo: str,
    btrfs_command: str,
    path: str,
) -> list[str] | None:
    """Return child subvolume paths from `btrfs subvolume list -o <path>`."""

    return source_list_child_subvolumes(SourceRunner(mode="ssh", ssh=ssh), sudo=sudo, btrfs_command=btrfs_command, path=path)


def remote_cache_existing_paths(
    ssh: SSHRunner,
    *,
    sudo: str,
    btrfs_command: str,
    cache_root: str | None,
    paths: list[str],
) -> set[str] | None:
    """Return requested cache paths that currently exist as source cache subvolumes."""

    return source_cache_existing_paths(SourceRunner(mode="ssh", ssh=ssh), sudo=sudo, btrfs_command=btrfs_command, cache_root=cache_root, paths=paths)


def remote_cache_existing_child_paths(
    ssh: SSHRunner,
    *,
    sudo: str,
    btrfs_command: str,
    cache_root: str | None,
    parent_path: str,
    paths: list[str],
) -> set[str] | None:
    """Return requested nested cache subvolumes found below one cache parent."""

    return source_cache_existing_child_paths(SourceRunner(mode="ssh", ssh=ssh), sudo=sudo, btrfs_command=btrfs_command, cache_root=cache_root, parent_path=parent_path, paths=paths)


def remote_cache_contains(
    ssh: SSHRunner,
    sudo: str,
    btrfs_command: str,
    cache_root: str | None,
    path: str,
) -> bool:
    """Return True when the configured source cache contains `path` as a subvolume."""

    return source_cache_contains(SourceRunner(mode="ssh", ssh=ssh), sudo, btrfs_command, cache_root, path)


def remote_cache_is_empty(
    ssh: SSHRunner,
    *,
    sudo: str,
    btrfs_command: str,
    cache_root: str | None,
    path: str,
) -> bool | None:
    """Return True/False for cache child subvolumes, or None when verification fails."""

    return source_cache_is_empty(SourceRunner(mode="ssh", ssh=ssh), sudo=sudo, btrfs_command=btrfs_command, cache_root=cache_root, path=path)


def cache_child_display_path(cache_parent: str, listed_path: str) -> str:
    """Return a short display path for a listed cache child subvolume."""

    parent_name = Path(cache_parent).name
    listed_text = listed_path.strip("/").rstrip("/")
    marker = f"/{parent_name}/"
    if marker in "/" + listed_text:
        return ("/" + listed_text).split(marker, 1)[1] or listed_text
    return listed_text


def _source_refresh_cache_path(
    cache_index: "BtrfsIndex | None",
    source: SourceRunner,
    path: str,
    *,
    sudo: str,
    btrfs_command: str,
) -> SubvolumeMeta | None:
    """Refresh one source cache path in the optional per-run Btrfs index."""

    from .remote_index import refresh_source_path

    return refresh_source_path(cache_index, source, path, sudo=sudo, btrfs_command=btrfs_command)


def source_ensure_cache_root(
    source: SourceRunner,
    *,
    sudo: str,
    btrfs_command: str,
    cache_root: str,
    cache_index: "BtrfsIndex | None" = None,
) -> None:
    """Ensure the configured source cache root exists as a Btrfs subvolume.

    The cache root is created lazily only when a writable Timeshift snapshot
    needs a read-only send copy. The parent directory of ``cache_root`` must
    already exist and be inside the intended Btrfs filesystem; this function
    creates exactly the configured cache root subvolume, not arbitrary parent
    directories.
    """

    if cache_index is not None and cache_index.contains(cache_root):
        return

    existing = source_get_subvolume_meta(
        source,
        cache_root,
        Path(cache_root).name,
        sudo,
        btrfs_command,
        required=False,
    )
    if existing:
        if cache_index is not None:
            cache_index.add(existing)
        return

    result = source.run(
        remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "create", cache_root]),
        check=False,
    )
    if result.returncode == 0:
        _source_refresh_cache_path(cache_index, source, cache_root, sudo=sudo, btrfs_command=btrfs_command)
        return

    if "target path already exists" in result.stderr.lower():
        if _source_refresh_cache_path(cache_index, source, cache_root, sudo=sudo, btrfs_command=btrfs_command):
            return
        raise RuntimeError(
            "Source cache root path already exists but is not detected as a Btrfs subvolume:\n"
            f"  {cache_root}\n"
            "The send-cache root must be a Btrfs subvolume. Move or remove the ordinary path, "
            "or choose another source.cache_root."
        )

    detail = result.stderr.strip() or result.stdout.strip() or f"return code {result.returncode}"
    raise RuntimeError(
        "Failed to create source cache root as a Btrfs subvolume:\n"
        f"  {cache_root}\n"
        "The parent directory must already exist on the source and be on the intended Btrfs filesystem.\n"
        + detail
    )


def _reuse_existing_cache_snapshot(
    source: SourceRunner,
    *,
    sudo: str,
    btrfs_command: str,
    cache_root: str,
    cache_path: str,
    subvolume_name: str,
    original_meta: SubvolumeMeta,
    cache_index: "BtrfsIndex | None" = None,
) -> str | None:
    """Return an existing safe read-only cache snapshot, or None when absent.

    The app must not try to recreate ``send-cache/<snapshot>/<subvolume>``
    when it already exists. Recreating a Btrfs snapshot either fails or produces
    a new UUID that cannot be used for previously received incremental parents.
    Existing cache snapshots are reused only after Btrfs metadata proves they are
    real read-only subvolumes and, when parent UUIDs are available, descended
    from the requested original Timeshift subvolume.
    """

    def validate(meta: SubvolumeMeta | None) -> str | None:
        if not meta:
            return None
        if meta.readonly is not True:
            raise RuntimeError(
                "Existing source cache path is a Btrfs subvolume but is not read-only:\n"
                f"  {cache_path}\n"
                "Refusing to use or overwrite it. Inspect the send-cache path manually."
            )
        if original_meta.uuid and meta.parent_uuid and meta.parent_uuid != original_meta.uuid:
            raise RuntimeError(
                "Existing source cache snapshot does not belong to the requested Timeshift snapshot:\n"
                f"  original: {original_meta.path}\n"
                f"  original UUID: {original_meta.uuid}\n"
                f"  cache:    {cache_path}\n"
                f"  cache Parent UUID: {meta.parent_uuid}\n"
                "Refusing to use it as a send source."
            )
        return cache_path

    indexed = cache_index.meta(cache_path) if cache_index is not None else None
    reused = validate(indexed)
    if reused:
        return reused

    # A bulk cache index may be stale or incomplete, especially after a prior
    # interrupted run or after switching between SSH and local mode. Always do a
    # targeted metadata refresh before attempting to create the same cache path.
    refreshed = _source_refresh_cache_path(cache_index, source, cache_path, sudo=sudo, btrfs_command=btrfs_command)
    reused = validate(refreshed)
    if reused:
        return reused

    if cache_index is None and source_cache_contains(source, sudo, btrfs_command, cache_root, cache_path):
        meta = source_get_subvolume_meta(
            source,
            cache_path,
            subvolume_name,
            sudo,
            btrfs_command,
            required=False,
        )
        reused = validate(meta)
        if reused:
            return reused
    return None


def source_ensure_cache_parent(
    source: SourceRunner,
    *,
    sudo: str,
    btrfs_command: str,
    cache_root: str,
    cache_parent: str,
    cache_index: "BtrfsIndex | None" = None,
) -> None:
    """Ensure the cache root and per-snapshot source cache parent are Btrfs subvolumes."""

    source_ensure_cache_root(
        source,
        sudo=sudo,
        btrfs_command=btrfs_command,
        cache_root=cache_root,
        cache_index=cache_index,
    )

    if cache_index is not None and cache_index.contains(cache_parent):
        return
    if cache_index is None and source_cache_contains(source, sudo, btrfs_command, cache_root, cache_parent):
        return

    result = source.run(
        remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "create", cache_parent]),
        check=False,
    )
    if result.returncode == 0:
        _source_refresh_cache_path(cache_index, source, cache_parent, sudo=sudo, btrfs_command=btrfs_command)
        return

    if "target path already exists" in result.stderr.lower():
        if _source_refresh_cache_path(cache_index, source, cache_parent, sudo=sudo, btrfs_command=btrfs_command):
            return
        elif source_cache_contains(source, sudo, btrfs_command, cache_root, cache_parent):
            return
        raise RuntimeError(
            "Source cache parent path already exists but is not detected as a Btrfs subvolume:\n"
            f"  {cache_parent}\n"
            "This may be a stale ordinary directory in the cache root. Inspect it manually."
        )

    detail = result.stderr.strip() or result.stdout.strip() or f"return code {result.returncode}"
    raise RuntimeError("Failed to create source cache parent subvolume.\n" + detail)


def source_ensure_readonly_send_path(
    source: SourceRunner,
    *,
    sudo: str,
    btrfs_command: str,
    original_path: str,
    cache_root: str | None,
    snapshot_name: str,
    subvolume_name: str,
    create_readonly_cache: bool,
    cache_index: "BtrfsIndex | None" = None,
    original_index: "BtrfsIndex | None" = None,
) -> str:
    """Return the original read-only source or create/reuse a read-only cache snapshot.

    When a bulk source snapshot-root index is available, use it first. That
    avoids a source-side ``btrfs subvolume show`` for every writable Timeshift
    snapshot just to decide whether a read-only cache snapshot is needed.
    """

    original_meta = original_index.meta(original_path) if original_index is not None else None
    if original_meta is None:
        original_meta = source_get_subvolume_meta(source, original_path, subvolume_name, sudo, btrfs_command, required=False)
    if not original_meta:
        raise RuntimeError(f"Source path is not a Btrfs subvolume or cannot be read: {original_path}")
    if original_meta.readonly is True:
        return original_path

    if not create_readonly_cache:
        raise RuntimeError(f"Source subvolume is not confirmed read-only and cache creation is disabled: {original_path}")
    if not cache_root:
        raise RuntimeError("Source subvolume is not confirmed read-only and source.cache_root is not configured")

    cache_parent = readonly_cache_parent_path(cache_root, snapshot_name)
    cache_path = readonly_cache_path(cache_root, snapshot_name, subvolume_name)

    existing_cache_path = _reuse_existing_cache_snapshot(
        source,
        sudo=sudo,
        btrfs_command=btrfs_command,
        cache_root=cache_root,
        cache_path=cache_path,
        subvolume_name=subvolume_name,
        original_meta=original_meta,
        cache_index=cache_index,
    )
    if existing_cache_path:
        return existing_cache_path

    source_ensure_cache_parent(
        source,
        sudo=sudo,
        btrfs_command=btrfs_command,
        cache_root=cache_root,
        cache_parent=cache_parent,
        cache_index=cache_index,
    )

    result = source.run(
        remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "snapshot", "-r", original_path, cache_path]),
        check=False,
    )
    if result.returncode == 0:
        if cache_index is not None:
            from .remote_index import refresh_source_path

            refresh_source_path(cache_index, source, cache_path, name=subvolume_name, sudo=sudo, btrfs_command=btrfs_command)
        return cache_path

    if "target path already exists" in result.stderr.lower():
        existing_cache_path = _reuse_existing_cache_snapshot(
            source,
            sudo=sudo,
            btrfs_command=btrfs_command,
            cache_root=cache_root,
            cache_path=cache_path,
            subvolume_name=subvolume_name,
            original_meta=original_meta,
            cache_index=cache_index,
        )
        if existing_cache_path:
            return existing_cache_path
    detail = result.stderr.strip() or result.stdout.strip() or f"return code {result.returncode}"
    raise RuntimeError("Failed to create read-only source cache snapshot.\n" + detail)


def source_delete_subvolume(
    source: SourceRunner,
    sudo: str,
    btrfs_command: str,
    path: str,
    *,
    protected_snapshot_root: str | None = None,
    check: bool = False,
    log_stderr: bool = True,
    mirror_stderr: bool = True,
):
    """Delete a source-side Btrfs subvolume.

    The optional protected_snapshot_root guard is a final safety net: the app
    must never delete Timeshift-owned source.snapshot_root or any path below it.
    """

    reject_protected_source_snapshot_path(path, protected_snapshot_root, action="delete")
    return source.run(
        remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "delete", path]),
        check=check,
        log_stderr=log_stderr,
        mirror_stderr=mirror_stderr,
    )


def source_send_cmd(
    source: SourceRunner,
    *,
    sudo: str,
    btrfs_command: str,
    current_path: str,
    parent_path: str | None = None,
    compressed_data: bool = False,
    proto: int | None = None,
    verbose: bool = False,
) -> list[str]:
    """Build command argv that runs source-side ``btrfs send``."""

    args = ["send"]
    if verbose:
        args += ["-v"]
    if proto is not None:
        args += ["--proto", str(proto)]
    if compressed_data:
        args += ["--compressed-data"]
    if parent_path:
        args += ["-p", parent_path]
    args.append(current_path)
    return source.command(remote_btrfs_cmd(sudo, btrfs_command, args))


def remote_ensure_cache_parent(
    ssh: SSHRunner,
    *,
    sudo: str,
    btrfs_command: str,
    cache_root: str,
    cache_parent: str,
    cache_index: "BtrfsIndex | None" = None,
) -> None:
    """Ensure the per-snapshot cache parent exists as a Btrfs subvolume."""

    return source_ensure_cache_parent(
        SourceRunner(mode="ssh", ssh=ssh),
        sudo=sudo,
        btrfs_command=btrfs_command,
        cache_root=cache_root,
        cache_parent=cache_parent,
        cache_index=cache_index,
    )


def remote_ensure_readonly_send_path(
    ssh: SSHRunner,
    *,
    sudo: str,
    btrfs_command: str,
    original_path: str,
    cache_root: str | None,
    snapshot_name: str,
    subvolume_name: str,
    create_readonly_cache: bool,
    cache_index: "BtrfsIndex | None" = None,
) -> str:
    """Return the original read-only source or create/reuse a read-only cache snapshot."""

    return source_ensure_readonly_send_path(
        SourceRunner(mode="ssh", ssh=ssh),
        sudo=sudo,
        btrfs_command=btrfs_command,
        original_path=original_path,
        cache_root=cache_root,
        snapshot_name=snapshot_name,
        subvolume_name=subvolume_name,
        create_readonly_cache=create_readonly_cache,
        cache_index=cache_index,
    )


def path_is_same_or_under(path: str | None, root: str | None) -> bool:
    """Return True when path is exactly root or below root.

    This is used for destructive safety guards. In particular,
    source.snapshot_root is Timeshift-owned and must never be deleted by this
    app, directly or through any child path.
    """

    if not path or not root:
        return False
    normalized_root = str(Path(root)).rstrip("/")
    normalized_path = str(Path(path)).rstrip("/")
    return normalized_path == normalized_root or normalized_path.startswith(normalized_root + "/")


def path_is_under_cache(path: str | None, cache_root: str | None) -> bool:
    """Return True when path points inside the configured source cache root."""

    if not path or not cache_root:
        return False
    normalized_root = str(Path(cache_root)).rstrip("/")
    normalized_path = str(Path(path)).rstrip("/")
    return normalized_path.startswith(normalized_root + "/")


def reject_protected_source_snapshot_path(path: str | None, snapshot_root: str | None, *, action: str) -> None:
    """Raise if a source-side destructive action targets Timeshift snapshots.

    Timeshift owns source.snapshot_root and every snapshot subvolume below it.
    Prune, destroy-leftovers, cache cleanup, and any source-side delete path must
    refuse those paths even if stale state or a bad config points at them.
    """

    if path_is_same_or_under(path, snapshot_root):
        raise RuntimeError(
            f"Refusing to {action} Timeshift-owned source.snapshot_root path: {path}. "
            f"Protected root: {snapshot_root}. This app may only delete source paths "
            "inside source.cache_root."
        )


def remote_delete_subvolume(
    ssh: SSHRunner,
    sudo: str,
    btrfs_command: str,
    path: str,
    *,
    protected_snapshot_root: str | None = None,
    check: bool = False,
    log_stderr: bool = True,
    mirror_stderr: bool = True,
):
    """Delete a source-side Btrfs subvolume."""

    return source_delete_subvolume(
        SourceRunner(mode="ssh", ssh=ssh),
        sudo,
        btrfs_command,
        path,
        protected_snapshot_root=protected_snapshot_root,
        check=check,
        log_stderr=log_stderr,
        mirror_stderr=mirror_stderr,
    )


def remote_send_cmd(
    ssh: SSHRunner,
    *,
    sudo: str,
    btrfs_command: str,
    current_path: str,
    parent_path: str | None = None,
    compressed_data: bool = False,
    proto: int | None = None,
    verbose: bool = False,
) -> list[str]:
    """Build SSH command that runs source-side `btrfs send`."""

    return source_send_cmd(
        SourceRunner(mode="ssh", ssh=ssh),
        sudo=sudo,
        btrfs_command=btrfs_command,
        current_path=current_path,
        parent_path=parent_path,
        compressed_data=compressed_data,
        proto=proto,
        verbose=verbose,
    )


def local_receive_cmd(destination_dir: Path, sudo: str, btrfs_command: str = "btrfs", *, verbose: bool = False) -> list[str]:
    """Build local `btrfs receive` command."""

    args = ["receive"]
    if verbose:
        args += ["-v"]
    args.append(str(destination_dir))
    return local_btrfs_cmd(sudo, btrfs_command, args)


def delete_local_subvolume(path: Path, sudo: str, btrfs_command: str = "btrfs") -> None:
    """Delete one local Btrfs subvolume."""

    run_local(local_btrfs_cmd(sudo, btrfs_command, ["subvolume", "delete", str(path)]))
