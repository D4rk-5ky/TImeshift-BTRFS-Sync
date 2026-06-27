"""Btrfs command builders and parsers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from .commands import quote_join, run_local, sudo_prefix
from .models import SubvolumeMeta
from .ssh import SSHRunner

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
    """Build a quoted remote command that invokes sudo+btrfs only."""

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


def remote_list_child_subvolumes(
    ssh: SSHRunner,
    *,
    sudo: str,
    btrfs_command: str,
    path: str,
) -> list[str] | None:
    """Return child subvolume paths from `btrfs subvolume list -o <path>`."""

    result = ssh.run(remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "list", "-o", path]), check=False)
    return None if result.returncode != 0 else _subvolume_list_paths(result.stdout)


def remote_cache_existing_paths(
    ssh: SSHRunner,
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
    listed_paths = remote_list_child_subvolumes(ssh, sudo=sudo, btrfs_command=btrfs_command, path=cache_root)
    if listed_paths is None:
        return None
    return {
        path
        for path in candidates
        if any(_listed_cache_path_matches(listed, cache_root, path) for listed in listed_paths)
    }


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

    if not cache_root or not path_is_under_cache(parent_path, cache_root):
        return set()
    candidates = [path for path in paths if path_is_under_cache(path, parent_path)]
    if not candidates:
        return set()
    listed_paths = remote_list_child_subvolumes(ssh, sudo=sudo, btrfs_command=btrfs_command, path=parent_path)
    if listed_paths is None:
        return None
    return {
        path
        for path in candidates
        if any(_listed_cache_path_matches(listed, cache_root, path) for listed in listed_paths)
    }


def remote_cache_contains(
    ssh: SSHRunner,
    sudo: str,
    btrfs_command: str,
    cache_root: str | None,
    path: str,
) -> bool:
    """Return True when the configured source cache contains `path` as a subvolume."""

    existing = remote_cache_existing_paths(
        ssh, sudo=sudo, btrfs_command=btrfs_command, cache_root=cache_root, paths=[path]
    )
    return bool(existing)


def remote_cache_is_empty(
    ssh: SSHRunner,
    *,
    sudo: str,
    btrfs_command: str,
    cache_root: str | None,
    path: str,
) -> bool | None:
    """Return True/False for cache child subvolumes, or None when verification fails."""

    if not cache_root or not path_is_under_cache(path, cache_root):
        return None
    listed_paths = remote_list_child_subvolumes(ssh, sudo=sudo, btrfs_command=btrfs_command, path=path)
    return None if listed_paths is None else not listed_paths


def cache_child_display_path(cache_parent: str, listed_path: str) -> str:
    """Return a short display path for a listed cache child subvolume."""

    parent_name = Path(cache_parent).name
    listed_text = listed_path.strip("/").rstrip("/")
    marker = f"/{parent_name}/"
    if marker in "/" + listed_text:
        return ("/" + listed_text).split(marker, 1)[1] or listed_text
    return listed_text


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

    if cache_index is not None and cache_index.contains(cache_parent):
        return
    if cache_index is None and remote_cache_contains(ssh, sudo, btrfs_command, cache_root, cache_parent):
        return

    result = ssh.run(
        remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "create", cache_parent]),
        check=False,
    )
    if result.returncode == 0:
        if cache_index is not None:
            from .remote_index import refresh_remote_path

            refresh_remote_path(cache_index, ssh, cache_parent, sudo=sudo, btrfs_command=btrfs_command)
        return

    # Race/previous-subvolume case: another subvolume path may have created the
    # date parent just before this command. Re-check with the non-probing list
    # helper and continue if it really exists as a Btrfs subvolume.
    if "target path already exists" in result.stderr.lower():
        if cache_index is not None:
            from .remote_index import refresh_remote_path

            if refresh_remote_path(cache_index, ssh, cache_parent, sudo=sudo, btrfs_command=btrfs_command):
                return
        elif remote_cache_contains(ssh, sudo, btrfs_command, cache_root, cache_parent):
            return
        raise RuntimeError(
            "Source cache parent path already exists but is not detected as a Btrfs subvolume:\n"
            f"  {cache_parent}\n"
            "This may be a stale ordinary directory in the cache root. Inspect it manually."
        )

    raise RuntimeError("Failed to create source cache parent subvolume.\n" + result.stderr.strip())


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

    original_meta = get_subvolume_meta("remote", original_path, subvolume_name, sudo, btrfs_command, ssh=ssh, required=False)
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

    if cache_index is not None and cache_index.contains(cache_path):
        return cache_path
    if cache_index is None and remote_cache_contains(ssh, sudo, btrfs_command, cache_root, cache_path):
        return cache_path

    remote_ensure_cache_parent(
        ssh,
        sudo=sudo,
        btrfs_command=btrfs_command,
        cache_root=cache_root,
        cache_parent=cache_parent,
        cache_index=cache_index,
    )

    result = ssh.run(
        remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "snapshot", "-r", original_path, cache_path]),
        check=False,
    )
    if result.returncode == 0:
        if cache_index is not None:
            from .remote_index import refresh_remote_path

            refresh_remote_path(cache_index, ssh, cache_path, name=subvolume_name, sudo=sudo, btrfs_command=btrfs_command)
        return cache_path

    if "target path already exists" in result.stderr.lower():
        if cache_index is not None:
            from .remote_index import refresh_remote_path

            if refresh_remote_path(cache_index, ssh, cache_path, name=subvolume_name, sudo=sudo, btrfs_command=btrfs_command):
                return cache_path
        elif remote_cache_contains(ssh, sudo, btrfs_command, cache_root, cache_path):
            return cache_path
    raise RuntimeError("Failed to create read-only source cache snapshot.\n" + result.stderr.strip())


def path_is_under_cache(path: str | None, cache_root: str | None) -> bool:
    """Return True when path points inside the configured source cache root."""

    if not path or not cache_root:
        return False
    normalized_root = str(Path(cache_root)).rstrip("/")
    normalized_path = str(Path(path)).rstrip("/")
    return normalized_path.startswith(normalized_root + "/")


def remote_delete_subvolume(
    ssh: SSHRunner,
    sudo: str,
    btrfs_command: str,
    path: str,
    *,
    check: bool = False,
    log_stderr: bool = True,
    mirror_stderr: bool = True,
):
    """Delete a source-side Btrfs subvolume."""

    return ssh.run(
        remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "delete", path]),
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
    """Build SSH command that runs remote `btrfs send`."""

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
    return ssh.command(remote_btrfs_cmd(sudo, btrfs_command, args))


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
