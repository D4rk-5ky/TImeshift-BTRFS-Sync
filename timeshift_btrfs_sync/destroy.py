"""Destructive leftover cleanup for removing a ts-btrfs setup."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import shlex
import subprocess

from . import btrfs
from . import remote_index
from . import payload_stats
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
    """Run a probe/delete command without duplicating expected stderr."""

    try:
        return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=env)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


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
    """Return source path existence status without sudo.

    Source-side sudoers is intentionally narrow and should only need
    passwordless ``btrfs`` and ``timeshift``. Existence checks use the source
    user's normal shell permissions instead of ``sudo test``.
    """

    result = _run_source_quiet(source, "test -e " + shlex.quote(path))
    return _path_exists_status(result)


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


def _local_remove_empty_child_dirs(path: str, sudo: str) -> int:
    """Remove empty ordinary directories directly under a local path without find."""

    removed = 0
    try:
        entries = list(Path(path).iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_dir():
            continue
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
    """Delete one local tree after deleting nested Btrfs subvolumes deepest-first."""

    result = DestroyResult(label=label, path=path, location="destination")
    exists, exists_error = _local_exists(path, sudo)
    if exists is None:
        result.errors.append(f"could not check local path existence: {exists_error}")
        return result
    result.exists = exists
    if not result.exists:
        return result

    meta = _local_subvolume_meta(path, sudo, btrfs_command)
    result.root_is_subvolume = meta is not None
    children = _collect_recursive_subvolumes(path, lambda current: _local_child_subvolumes(current, sudo, btrfs_command))
    if children is None:
        if result.root_is_subvolume:
            result.errors.append("could not recursively list local child subvolumes")
            return result
        children = []

    result.subvolumes = _sort_deepest_first(children + ([path] if result.root_is_subvolume else []))
    if dry_run:
        return result

    for subvol in result.subvolumes:
        result.removed_stale_dirs += _local_remove_empty_child_dirs(subvol, sudo)
        try:
            btrfs.delete_local_subvolume(Path(subvol), sudo, btrfs_command)
            result.deleted_subvolumes += 1
            if _local_remove_stale_path(subvol, sudo):
                result.removed_stale_dirs += 1
        except Exception as exc:
            result.errors.append(f"failed deleting local subvolume {subvol}: {exc}")

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
    for child in "$base"/* "$base"/.[!.]* "$base"/..?*; do
        [ -e "$child" ] || continue
        [ -d "$child" ] || continue
        if rmdir -- "$child" 2>/dev/null; then
            removed=$((removed + 1))
        fi
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

    # Source-side sudoers should only need passwordless timeshift/btrfs. Build
    # the existence/listing view from Btrfs metadata instead of using
    # ``sudo test`` or other broad sudo commands.
    index = remote_index.build_source_btrfs_index(
        source,
        path,
        sudo=sudo,
        btrfs_command=btrfs_command,
        include_root=True,
    )
    if index.root_missing:
        result.exists = False
        return result
    if index.errors:
        result.errors.extend(f"could not build source Btrfs index: {error}" for error in index.errors)
        return result

    result.exists = True
    result.root_is_subvolume = index.contains(path)
    result.subvolumes = _sort_deepest_first(index.child_paths(path) + ([path] if result.root_is_subvolume else []))
    if dry_run:
        return result

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

    exists_after, _ = _source_exists(source, path, sudo)
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
