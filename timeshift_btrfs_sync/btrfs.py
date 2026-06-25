"""Btrfs command builders and parsers."""

from __future__ import annotations

from pathlib import Path
import re
from .commands import quote_join, run_local, sudo_prefix
from .models import SubvolumeMeta
from .ssh import SSHRunner

UUID_KEYS = {"UUID": "uuid", "Parent UUID": "parent_uuid", "Received UUID": "received_uuid"}



def _clean_uuid(value: str) -> str | None:
    """Normalize Btrfs UUID fields."""

    value = value.strip()
    return None if not value or value == "-" else value


def parse_subvolume_show(output: str, name: str, path: str) -> SubvolumeMeta:
    """Parse selected fields from `btrfs subvolume show`.

    Besides UUID fields, this also reads the `Flags:` line when present. Newer
    btrfs-progs versions print read-only state here as `Flags: readonly`, so
    this one command is the source of truth for both UUID metadata and read-only
    detection.
    """

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

        # Example outputs seen across btrfs-progs versions:
        #   Flags: readonly
        #   Flags: -
        #   Flags: readonly|something-else
        # A read-only source can be sent directly, so detecting this avoids
        # creating an unnecessary send-cache snapshot.
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


def remote_try_subvolume_show(ssh: SSHRunner, sudo: str, btrfs_command: str, path: str, name: str) -> SubvolumeMeta | None:
    """Return remote subvolume metadata or None if invalid."""

    result = ssh.run(remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "show", path]), check=False)
    if result.returncode != 0:
        return None
    return parse_subvolume_show(result.stdout, name=name, path=path)


def local_subvolume_show(path: Path, sudo: str, name: str, btrfs_command: str = "btrfs") -> SubvolumeMeta:
    """Return local subvolume metadata."""

    result = run_local(local_btrfs_cmd(sudo, btrfs_command, ["subvolume", "show", str(path)]))
    return parse_subvolume_show(result.stdout, name=name, path=str(path))

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


def _parse_subvolume_list_paths(output: str) -> list[str]:
    """Extract path fields from `btrfs subvolume list` output."""

    paths: list[str] = []
    for line in output.splitlines():
        match = re.search(r"\bpath\s+(.+)$", line.strip())
        if match:
            paths.append(match.group(1).strip().rstrip("/"))
    return paths


def _cache_path_suffix_candidates(cache_root: str, path: str) -> set[str]:
    """Build possible Btrfs-list suffixes for a cache path.

    `btrfs subvolume list <path>` prints paths relative to the Btrfs filesystem
    root, not necessarily the absolute mount path used in the config. Because
    the app does not know the source mount point, compare by conservative
    suffixes derived from both the configured cache root and the path below it.
    """

    root_path = Path(cache_root)
    target_path = Path(path)
    candidates: set[str] = set()

    try:
        rel = target_path.relative_to(root_path)
    except ValueError:
        rel = Path(target_path.name)

    rel_text = str(rel).strip("/")
    if rel_text:
        candidates.add(rel_text)

    root_parts = [part for part in root_path.parts if part not in {"/", ""}]
    for index in range(len(root_parts)):
        tail = "/".join(root_parts[index:]).strip("/")
        if not tail:
            continue
        candidates.add(f"{tail}/{rel_text}".strip("/"))

    abs_text = str(target_path).strip("/")
    if abs_text:
        candidates.add(abs_text)
    return {candidate.rstrip("/") for candidate in candidates if candidate}


def _subvolume_list_contains_cache_path(output: str, cache_root: str, path: str) -> bool:
    """Return True if `btrfs subvolume list` appears to contain `path`."""

    listed_paths = _parse_subvolume_list_paths(output)
    candidates = _cache_path_suffix_candidates(cache_root, path)
    for listed in listed_paths:
        listed = listed.strip("/").rstrip("/")
        if listed in candidates:
            return True
        for candidate in candidates:
            if listed.endswith("/" + candidate):
                return True
    return False


def remote_cache_subvolume_exists(
    ssh: SSHRunner,
    sudo: str,
    btrfs_command: str,
    cache_root: str | None,
    path: str,
) -> bool:
    """Check whether a cache subvolume exists without probing the missing path.

    This must avoid two different traps:

    * `btrfs subvolume show <missing-cache-path>` prints noisy expected stderr
      when a cache child such as @home has not been created yet.
    * `btrfs subvolume list <cache_root>` without `-o` may list every subvolume
      in the filesystem. A normal Timeshift snapshot path like
      `timeshift-btrfs/snapshots/<date>/@` then has the same suffix as the
      wanted cache path `<date>/@`, causing a false positive.

    Use `btrfs subvolume list -o <cache_root>` so Btrfs restricts the result to
    descendants of the configured cache root. Then suffix matching is safe for
    both possible output styles: relative paths such as `<date>/@` and full
    filesystem-relative paths such as `timeshift-btrfs/.ts-btrfs-sync/send-cache/<date>/@`.
    """

    if not cache_root or not path_is_under_cache(path, cache_root):
        return False
    result = ssh.run(
        remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "list", "-o", cache_root]),
        check=False,
    )
    if result.returncode != 0:
        return False
    return _subvolume_list_contains_cache_path(result.stdout, cache_root, path)


def _cache_child_display_path(cache_parent: str, listed_path: str) -> str:
    """Return a short display path for a listed cache child subvolume.

    `btrfs subvolume list -o <cache-parent>` can print paths relative to the
    filesystem root rather than relative to <cache-parent>. For logging, prefer
    the child tail such as `@home`, but fall back to the raw listed path when
    the exact parent prefix is not visible.
    """

    parent_text = str(Path(cache_parent)).strip("/").rstrip("/")
    listed_text = listed_path.strip("/").rstrip("/")
    if parent_text and listed_text.startswith(parent_text + "/"):
        return listed_text[len(parent_text) + 1 :] or listed_text
    parent_name = Path(cache_parent).name
    marker = f"/{parent_name}/"
    if marker in "/" + listed_text:
        return ("/" + listed_text).split(marker, 1)[1] or listed_text
    return listed_text


def remote_cache_child_subvolumes(
    ssh: SSHRunner,
    *,
    sudo: str,
    btrfs_command: str,
    cache_root: str | None,
    cache_parent: str,
) -> list[str] | None:
    """List remaining child subvolumes below one cache date parent.

    The source cache layout is normally:

        <cache_root>/<snapshot-name>/@
        <cache_root>/<snapshot-name>/@home

    The `<snapshot-name>` directory is itself a Btrfs subvolume. It must only be
    deleted after all child cache snapshots below it are gone. This helper uses
    `btrfs subvolume list -o <cache-parent>` so it checks for any descendant
    subvolume, not only the currently configured names.

    Returns None when the emptiness check could not be performed. Callers should
    keep the parent in that case instead of risking a noisy failed delete.
    """

    if not cache_root or not path_is_under_cache(cache_parent, cache_root):
        return None

    result = ssh.run(
        remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "list", "-o", cache_parent]),
        check=False,
    )
    if result.returncode != 0:
        return None

    listed_paths = _parse_subvolume_list_paths(result.stdout)
    return [_cache_child_display_path(cache_parent, path) for path in listed_paths]


def remote_ensure_cache_parent(
    ssh: SSHRunner,
    *,
    sudo: str,
    btrfs_command: str,
    cache_root: str,
    cache_parent: str,
) -> None:
    """Ensure the per-snapshot cache parent exists as a Btrfs subvolume.

    The cache layout is `<cache_root>/<snapshot-name>/@` and optionally
    `<cache_root>/<snapshot-name>/@home`. When @ is prepared first it creates
    the date parent; when @home is prepared later, the parent already exists and
    that must be treated as normal, not as a failing condition.
    """

    if remote_cache_subvolume_exists(ssh, sudo, btrfs_command, cache_root, cache_parent):
        return

    result = ssh.run(
        remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "create", cache_parent]),
        check=False,
    )
    if result.returncode == 0:
        return

    # Race/previous-subvolume case: another subvolume path may have created the
    # date parent just before this command. Re-check with the non-probing list
    # helper and continue if it really exists as a Btrfs subvolume.
    if "target path already exists" in result.stderr.lower():
        if remote_cache_subvolume_exists(ssh, sudo, btrfs_command, cache_root, cache_parent):
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
) -> str:
    """Return a source path safe for `btrfs send`.

    Read-only detection comes from the same `btrfs subvolume show` call that
    reads UUID metadata. If `Flags: readonly` is present, the original Timeshift
    snapshot is sent directly. If no read-only flag is detected, the app creates
    a read-only cache snapshot before `btrfs send`.

    This uses only one Btrfs metadata command while still avoiding unnecessary
    cache snapshots for already-read-only sources.
    """

    original_meta = remote_try_subvolume_show(ssh, sudo, btrfs_command, original_path, subvolume_name)
    if not original_meta:
        raise RuntimeError(f"Source path is not a Btrfs subvolume or cannot be read: {original_path}")
    if original_meta.readonly is True:
        return original_path

    # If no read-only flag is detected, treat the source as needing a read-only
    # send-cache snapshot. That is safe for writable sources and avoids the
    # separate read-only probe command entirely.
    if not create_readonly_cache:
        raise RuntimeError(f"Source subvolume is not confirmed read-only and cache creation is disabled: {original_path}")
    if not cache_root:
        raise RuntimeError("Source subvolume is not confirmed read-only and source.cache_root is not configured")

    cache_parent = readonly_cache_parent_path(cache_root, snapshot_name)
    cache_path = readonly_cache_path(cache_root, snapshot_name, subvolume_name)

    # Check for an existing cache snapshot without probing the missing path
    # directly. Missing @home under an existing date parent is normal when @ was
    # prepared first, so avoid noisy expected "No such file" stderr here.
    if remote_cache_subvolume_exists(ssh, sudo, btrfs_command, cache_root, cache_path):
        return cache_path

    remote_ensure_cache_parent(
        ssh,
        sudo=sudo,
        btrfs_command=btrfs_command,
        cache_root=cache_root,
        cache_parent=cache_parent,
    )

    result = ssh.run(
        remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "snapshot", "-r", original_path, cache_path]),
        check=False,
    )
    if result.returncode == 0:
        return cache_path

    # If another attempt created the target between the list check and the
    # snapshot command, accept it only after Btrfs confirms that the target is a
    # cache subvolume.
    if "target path already exists" in result.stderr.lower():
        if remote_cache_subvolume_exists(ssh, sudo, btrfs_command, cache_root, cache_path):
            return cache_path
    raise RuntimeError("Failed to create read-only source cache snapshot.\n" + result.stderr.strip())


def path_is_under_cache(path: str | None, cache_root: str | None) -> bool:
    """Return True when path points inside the configured source cache root.

    This is used before deleting any source-side cache subvolume. The check is
    intentionally simple and conservative: only absolute-looking paths below the
    configured cache_root are treated as deletable cache paths.
    """

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
    """Delete a source-side Btrfs subvolume with `btrfs subvolume delete`.

    This is used for temporary read-only cache snapshots after they are no
    longer needed as incremental parents. It still only requires passwordless
    source-side `btrfs`; no rm/mkdir/cat/helper command is introduced.
    """

    return ssh.run(
        remote_btrfs_cmd(sudo, btrfs_command, ["subvolume", "delete", path]),
        check=check,
        log_stderr=log_stderr,
        mirror_stderr=mirror_stderr,
    )


def remote_try_delete_cache_subvolume(
    ssh: SSHRunner,
    *,
    sudo: str,
    btrfs_command: str,
    cache_root: str | None,
    path: str | None,
) -> bool:
    """Best-effort delete for one source cache subvolume.

    Returns True only when the delete command succeeded. Paths outside
    cache_root are refused so this cleanup can never delete original Timeshift
    snapshots by accident.
    """

    if not path_is_under_cache(path, cache_root):
        return False
    assert path is not None
    result = remote_delete_subvolume(ssh, sudo, btrfs_command, path, check=False)
    return result.returncode == 0


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
    """Build SSH command that runs remote `btrfs send`.

    verbose=True adds `-v`. Btrfs send verbose output is operation/detail
    logging, not a percentage progress bar. Throughput/total progress is still
    best provided by mbuffer.
    """

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
    """Build local `btrfs receive` command.

    verbose=True adds `-v` so Btrfs receive can print operation details.
    """

    args = ["receive"]
    if verbose:
        args += ["-v"]
    args.append(str(destination_dir))
    return local_btrfs_cmd(sudo, btrfs_command, args)


def delete_local_subvolume(path: Path, sudo: str, btrfs_command: str = "btrfs") -> None:
    """Delete one local Btrfs subvolume."""

    run_local(local_btrfs_cmd(sudo, btrfs_command, ["subvolume", "delete", str(path)]))
