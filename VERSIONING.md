## 0.1.27

- Added source-cache UUID adoption for incremental parent selection.
- Existing read-only cache snapshots below `source.cache_root` can now be used as parent candidates when their UUID exactly matches the destination parent's `Received UUID`.
- This supports switching from SSH pull to local sync on the same source when the earlier SSH pull already created valid read-only cache snapshots.
- Kept the safety rule that missing cache parents are never recreated for parent matching, because recreated Btrfs snapshots get new UUIDs.

## 0.1.26 - source snapshot-root SSH preflight order and ordinary-directory fallback

- Changed source snapshot-root preflight to run a second Btrfs-only fallback check with `btrfs filesystem df <snapshot_root>` when `btrfs subvolume list -o <snapshot_root>` does not accept an ordinary Timeshift directory. This keeps ordinary Timeshift-owned snapshot roots valid.
- Clarified in code/config/docs that the source snapshot-root check runs on the selected source endpoint: through SSH/sshpass in SSH mode and locally in local mode.
- Changed source preflight ordering so `source.cache_root` is not created or modified when `source.snapshot_root` fails verification. This avoids creating send-cache storage on the wrong source/mount when the Timeshift-owned snapshot root is missing or misconfigured.
- Kept the global safety invariant that `source.snapshot_root` and everything below it are never created, pruned, deleted, destroyed, or cleaned by the app.

This build is version `0.1.26`.

# Versioning

## 0.1.25 - protect Timeshift snapshot root and improve remote snapshot-root diagnostics

- Added explicit source-delete safety guards so prune, source send-cache cleanup, and destroy-leftovers refuse any source path that is `source.snapshot_root` or below it.
- Updated `source.snapshot_root` comments in code and config example: Timeshift owns this path and the app must never create, prune, delete, destroy, or clean it.
- Changed `source.snapshot_root` preflight to try the configured `sudo btrfs subvolume list -o <snapshot_root>` check first, then use shell visibility checks only for diagnostics. This gives clearer SSH-mode errors when the remote path is wrong, not mounted, or not accessible through sudo btrfs.
- Updated README.md and COMMENTED_CODE_MAP.md to describe the current protected-root safety invariant.

This build is version `0.1.25`.

## 0.1.24 - separate source cache root validation

- Added config/preflight validation that `source.cache_root` must be outside `source.snapshot_root`.
- Clarified the failure case where `source.snapshot_root` is accepted as a normal Timeshift-owned directory, but `source.cache_root` is incorrectly pointed at the same snapshots directory.
- Kept `source.cache_root` as app-owned send-cache storage that is created as a Btrfs subvolume when missing and cache creation is enabled.
- Updated README.md, COMMENTED_CODE_MAP.md, VERSIONING.md, and the example config to describe the current cache-root rule.

This build is version `0.1.24`.

## 0.1.23 - Timeshift-owned snapshot root preflight

- Changed source snapshot-root preflight so `source.snapshot_root` is never created by the app.
- `source.snapshot_root` may be an ordinary directory on a Btrfs filesystem, because Timeshift creates snapshot subvolumes inside that directory.
- Missing, non-directory, or non-Btrfs-accessible `source.snapshot_root` is now a hard preflight error in both dry-run and real-run mode.
- Updated README.md, COMMENTED_CODE_MAP.md, and the example config to describe the current snapshot-root rule without adding old-version notes there.

This build is version `0.1.23`.

## 0.1.22 - early lock path and Btrfs-first helper creation

- Changed real-run ordering so the lock file parent is prepared before source roots, destination helper folders, state paths, logs, or sync/prune work are checked.
- If the lock path chain includes `destination.target_root`, that component is created by the strict target-root rule and must become a Btrfs subvolume.
- Missing lock/helper folders now try `btrfs subvolume create` first, because the app works on Btrfs storage, then fall back to normal `mkdir` when Btrfs creation is not possible.
- Updated README.md and COMMENTED_CODE_MAP.md to describe the current lock-path and helper-folder order without old-version explanations.

This build is version `0.1.22`.

## 0.1.21 - lock/helper folder creation safety

- Added real-run lock path preflight before opening the lock file.
- The lock-file parent is created if missing and may be either an ordinary directory or a Btrfs subvolume.
- Destination helper folders accept existing directories or Btrfs subvolumes.
- `FileLock` no longer creates parent directories itself, preventing accidental normal-directory creation of `destination.target_root`.
- File logging no longer creates missing parent directories before preflight; if the log directory is not ready, the command continues with terminal-only logging.
- Updated README.md and COMMENTED_CODE_MAP.md to describe the current helper-folder behavior.

This build is version `0.1.21`.

## 0.1.20 - require destination target root subvolume

- Fixed destination preflight so an existing `destination.target_root` must pass `btrfs subvolume show`.
- A plain directory inside a Btrfs filesystem is now a hard preflight error instead of being reported as OK by the broader Btrfs-accessibility check.
- Kept missing-target behavior from the previous release: when allowed, the app creates the missing target root with `btrfs subvolume create <target_root>` and verifies it before continuing.
- Updated README, commented code map, and config example comments to describe the current target-root rule.

This build is version `0.1.20`.

## 0.1.19 - release zip directory permissions fix

- Rebuilt the release zip with correct Unix directory permissions so package folders extract as usable directories.
- Ensured `timeshift_btrfs_sync/data/` is a real directory containing `config.example.toml`.
- Kept only one `config.example.toml`, inside the package data folder.
- Updated README and commented code map to describe the current package-data layout without adding old-version details there.

This build is version `0.1.19`.

## 0.1.18 - destination target root subvolume creation

- Changed real-run preflight so a missing `destination.target_root` is created with `btrfs subvolume create <target_root>` instead of Python `mkdir`.
- Preflight now verifies that the destination target-root parent already exists and is Btrfs-accessible before creating the exact configured target root.
- Existing destination target roots are not converted; existing directory-based backup roots keep working as long as they are Btrfs-accessible.
- Updated README, commented code map, and config example comments to describe the current target-root behavior.

This build is version `0.1.18`.

## 0.1.17 - documentation cleanup

- Cleaned `README.md` so it documents the app as it currently works instead of listing release-by-release changes.
- Cleaned `COMMENTED_CODE_MAP.md` so it focuses on current CLI commands, shell command families, functions, and classes, with explanations of what each does and why.
- Kept historical version notes in this `VERSIONING.md` file instead of duplicating them in the README or code map.

This build is version `0.1.17`.

### 0.1.16

- Added `*.toml` to `.gitignore` so local TOML configuration files are ignored by default.
- Kept the example-config exception after the ignore rule so example configuration files can still be tracked.
- Kept release packaging free of `__pycache__`, `.pyc`, and `.pyo` files.

### 0.1.15

- Real-run sync path preflight now attempts to create missing configured roots before Timeshift on-demand creation or send/receive work starts.
- `source.snapshot_root` is created as a normal source-side directory only after its parent is proven Btrfs-accessible.
- `source.cache_root` is created during preflight as a Btrfs subvolume when missing and `source.create_readonly_cache = true`; existing ordinary directories are still refused.
- `destination.target_root` is created locally during preflight when missing and `destination.create_target_root = true`, then verified with Btrfs before sync continues.
- Preflight hard errors now name the exact configured path that could not be verified or created.
- Included the requested `.gitignore` exactly as supplied and kept release packaging free of `__pycache__`, `.pyc`, and `.pyo` files.
- Updated README, commented code map, versioning, and config example comments.

### 0.1.14

- Added lazy source cache-root creation for writable Timeshift snapshots that need a read-only send copy.
- `source.cache_root` is now created as a Btrfs subvolume with `btrfs subvolume create <cache_root>` when it is missing and cache is actually needed.
- Existing `source.cache_root` paths must already be Btrfs subvolumes; ordinary directories are refused so app-owned send-cache cleanup remains safe.
- Preflight now accepts a missing `source.cache_root` only when `create_readonly_cache = true` and the cache-root parent is Btrfs-accessible.
- The same logic works in both `source.mode = "ssh"` and `source.mode = "local"` through `SourceRunner`.
- Updated README, commented code map, and the example config comments for lazy cache-root subvolume creation.

### 0.1.13

- Rebuilt the release archive without Python cache folders or compiled cache files.
- Confirmed no `__pycache__`, `.pyc`, or `.pyo` entries are present in the zip.
- Kept exactly one canonical example config at `timeshift_btrfs_sync/data/config.example.toml`.

### 0.1.12

- Removed the extra top-level `config.example.toml` from the source archive.
- Kept exactly one canonical example config at `timeshift_btrfs_sync/data/config.example.toml`.
- Kept `ts-btrfs init-config` using the packaged data-folder template.
- Removed Python `__pycache__` files from the release archive.

### 0.1.11

- Added a top-level `config.example.toml` copy to the source archive so the complete example config is visible without looking inside package data.
- Kept `timeshift_btrfs_sync/data/config.example.toml` as the packaged template used by `ts-btrfs init-config`.
- Confirmed both config examples include `source.mode = "ssh"` with comments for `ssh` and `local` modes.

### 0.1.10

- Added `source.mode = "local"` so the same sync/prune/destroy workflow can use Timeshift Btrfs snapshots on the machine running `ts-btrfs` without wrapping source commands in SSH.
- Added `source.py` with a shared `SourceRunner` abstraction. Existing SSH pull behavior now uses `SourceRunner(mode="ssh")`; local sync uses `SourceRunner(mode="local")` and runs source-side shell commands locally.
- Generalized Timeshift listing/creation, Btrfs source metadata, source send-cache indexing/cleanup, preflight source checks, pruning, destroy-leftovers, and the `btrfs send` side of the pipeline to use the shared source runner.
- Added `test-source` as the primary source connectivity/sudo check. Existing `test-ssh` remains as a backward-compatible alias and skips SSH checks in local mode.
- Updated README, the commented code map, and the example config to document SSH and local source modes.

### 0.1.9

- Removed source-side `sudo find`, `sudo test`, and `sudo rm` usage from `destroy-leftovers`.
- Source-side destroy cleanup now uses passwordless `sudo btrfs` for Btrfs subvolume deletion/listing only.
- Empty stale ordinary directories left after source subvolume deletion are removed with normal non-sudo `rmdir` as a best-effort cleanup. If permissions prevent that, the app reports the remaining directory instead of requesting broader sudo.
- This preserves the intended least-privilege source sudoers model where the source user only needs passwordless `timeshift` and `btrfs`.

### 0.1.7

- Added a sync path preflight before automatic/manual on-demand creation and before send/receive work.
- The preflight checks `source.snapshot_root`, configured `source.cache_root`, and `destination.target_root` up front, so a missing/mis-mounted path fails before a fresh on-demand Timeshift snapshot is created.
- The source checks are batched into one SSH call and use the configured `sudo btrfs subvolume list -o <path>` access instead of generic sudo filesystem permissions.
- `create-manual` now runs the same path preflight before creating a standalone Timeshift on-demand snapshot.

### 0.1.6

- On-demand retry-order guarantee: if an app-created on-demand snapshot itself failed or was only partially received on the destination, sync deletes only that incomplete destination path and retries it when the existing oldest-to-newest queue reaches that snapshot/subvolume.
- Added explicit output for incomplete destination cleanup showing the retry policy and order policy, so failed on-demand snapshots are visibly not jumped ahead or handled out of order.
- Added code comments around the sorted snapshot/subvolume loop documenting that incomplete destination cleanup is intentionally done inside the normal order loop.

### 0.1.5

- Fresh on-demand creation after interrupted runs: if an older app-created on-demand snapshot is still pending, the next normal `sync` keeps that pending snapshot in the oldest-to-newest queue but still creates a new on-demand snapshot for the current run.
- The pending snapshot notice now explains that older failed-run snapshots remain queued and that a fresh snapshot is still created because the old one may no longer represent the current system state.
- Interrupted/partial destination receives are still deleted and retried when their snapshot/subvolume is reached in the normal send order.

### 0.1.4

- Interrupted-run retry safety: if an earlier sync already created an app on-demand Timeshift snapshot but did not finish syncing it, the next normal `sync` skips creating a duplicate manual snapshot and processes the existing pending app-created snapshot in normal oldest-to-newest order.
- Incomplete destination receives are still cleaned before retry, and the destination per-run index is invalidated for the deleted path so later parent checks do not use stale metadata.
- If `state.json` says a snapshot is fully synced but one of the expected destination paths is missing, sync no longer skips the whole snapshot; it retries the missing path(s).

### 0.1.3

- Added normalized payload match statistics to `destroy-leftovers --delete-both`.
- The summary now compares real source-side send payload against destination received payload by snapshot/subvolume name, so raw Btrfs helper/container subvolume counts no longer look like retention mismatches.
- The comparison understands v0.1.2 direct read-only Timeshift sends: protected Timeshift original send paths from state.json are counted as source-side payload, but are still never deleted by `destroy-leftovers` or prune.

### 0.1.2

- Added explicit read-only Timeshift direct-send support and state labeling.
- If an original Timeshift snapshot child is already read-only, sync sends directly from the original Timeshift path instead of creating an app cache copy.
- State now records `send_path_kind`, `send_path_owned_by_app`, and `send_path_prune_protected` for each subvolume.
- Prune safety was tightened so only app-owned source-cache paths below `source.cache_root` are deleted. Direct Timeshift original send paths are listed as protected and are never removed by prune.

## Changelog

### 0.1.1

- `ssh.control_master = true` now creates a missing `ssh.control_path` parent directory automatically with owner-only permissions (`0700`), including missing intermediate directories when the user running the app is allowed to create it.
- Existing ControlPath parent directories are still validated and refused if they are not owned by the app user, are group/other accessible, or are inside shared temporary storage such as `/tmp`, `/var/tmp`, or `/dev/shm`.
- Updated README and config comments to explain automatic private directory creation and the remaining socket-reuse risk.

### 0.1.0

- Added safety validation and documentation for SSH ControlMaster/ControlPath connection reuse.
- `ssh.control_master = true` requires an explicit absolute `ssh.control_path` whose parent directory is owned by the user running the app, is private (`chmod 0700` style), and is not inside shared temporary storage such as `/tmp`, `/var/tmp`, or `/dev/shm`.
- Documented what OpenSSH multiplexing is, how it speeds up passphrase-protected keys, and why the local control socket must be protected.

### 0.0.99

- Added `remote_index.py`, a per-run Btrfs subvolume index used to cache source send-cache and destination path/UUID lookups.
- Sync now builds a source send-cache index and destination index once per run, then reuses those dictionaries for parent/floor validation where safe.
- Source cache index entries are refreshed after cache snapshot creation; destination index entries are refreshed after each receive; prune removes deleted cache paths from the index.
- Prune source send-cache cleanup now uses the per-run source cache index instead of repeatedly listing cache parents/children.
- `destroy-leftovers` now builds the remote source cache tree in one SSH command and deletes remote source cache subvolumes in one batched SSH command.
- Added optional `[ssh]` `control_master`, `control_persist`, and `control_path` settings for OpenSSH connection reuse, useful with password-protected keys and high KDF iterations.

### 0.0.98

- Changed `destroy-leftovers --delete-source` so it never deletes `source.snapshot_root`, because that path belongs to Timeshift and contains the user's original OS snapshots.
- `--delete-source` now only deletes app-created source send-cache paths under `source.cache_root`; `--delete-both` deletes source send-cache plus destination target.

### 0.0.97

- Fixed destroy-leftovers recursive Btrfs cleanup so nested source send-cache subvolumes are discovered before deleting timestamp parent subvolumes.
- After each subvolume delete, removes stale ordinary directories that can be left behind before deleting parent subvolumes.

### 0.0.96

- Added `destroy-leftovers`, a destructive retirement cleanup command for deleting configured cleanup leftovers and/or destination target root after the app is no longer used. Superseded by 0.0.98: source.snapshot_root is no longer a destroy target.
- Real deletion requires `--run`, `--i-understand-this-destroys-data`, an explicit target flag, and two typed confirmations.

### 0.0.95

- Version-only renumber from 0.9.5 to match the old release-count scheme where 0.9.5 corresponds to release 95.
- No code behavior changed.

### 0.9.5

- Existing-destination sync can now use a saved source send-cache parent even when Timeshift has already pruned the original parent snapshot.
- This lets a delayed backup continue incrementally from the newest UUID-confirmed destination/source-cache parent, then prune normally afterward.

### 0.9.4

- Fresh/full sync now preselects only source snapshots that the active retention rules would keep, then sends that reduced set oldest-to-newest.
- This avoids wasting time and disk wear sending old snapshots that post-sync prune would immediately delete. Existing non-empty destination sync behavior is unchanged.

### 0.9.3

- Moved per-snapshot prune state result into its own unindented `State` section with a blank line before it.
- Output-only readability change; prune/delete logic is unchanged.

### 0.9.2

- Readability-only prune output change: each retention delete item now separates destination deletion from source send-cache deletion with clear section headers.

### 0.9.1

- Version-only bump from 0.8.11.

### 0.8.11

- Reworked prune deletion as one coordinated per-snapshot item: destination subvolumes and source send-cache are both attempted before state is removed.
- Prune now keeps the state entry unless destination and source send-cache are both confirmed gone or already absent, while still attempting the available side when the other side is missing/unavailable.
- Retention delete plans now show both destination subvolume paths and source send-cache paths for each candidate.

### 0.8.10

- Fixed source send-cache prune cleanup for nested `@` and `@home` cache subvolumes. The app now lists the timestamp cache parent before deciding child cache subvolumes are missing.
- Renamed prune output from `SOURCE CACHE RETENTION CLEANUP` to `SOURCE SEND-CACHE RETENTION CLEANUP` to avoid confusion with original Timeshift snapshots.
- Prints a retention delete summary to the normal run log as well as the success summary.

### 0.8.9

- Made retention deletion idempotent: state entries are removed only after destination and source cache cleanup are confirmed gone or already absent.
- Kept state entries when source cache cleanup cannot be verified so a later prune can retry safely.

### 0.8.8

- Fixed retention-based source cache cleanup so it also checks the timestamp cache parent. If `@`/`@home` are already missing but the empty parent still exists, prune now deletes the parent instead of stopping after child skips.
- Source cache cleanup still only deletes app-created cache paths under `source.cache_root`; it does not delete original Timeshift source snapshots.

### 0.8.7

- Fixed first-run multi-subvolume seeding: when the destination was empty at sync start, remaining first-chain subvolumes may still full-send after the first subvolume makes the destination non-empty.
- Preserved the strict mixed-chain guard for normal non-empty destinations.

### 0.8.6

- Fixed retention-based source cache cleanup to pre-check which cache subvolumes still exist before deleting.
- Missing source cache paths are now skipped cleanly instead of producing noisy Btrfs delete/list errors.
- Removed duplicate stderr printing from cache cleanup failures; command stderr is already emitted by the command runner.

### 0.8.5

- Changed source cache cleanup to retention-based cleanup. `sync` now keeps every read-only cache snapshot it creates, and `prune` deletes matching source cache snapshots only for destination snapshots selected by the same retention delete plan.
- This preserves more common Btrfs UUID ground when short-lived snapshots are removed later.

### 0.8.4

- Refreshed `COMMENTED_CODE_MAP.md` to document only current commands, classes, and functions, and to add concise notes explaining safety-driven code paths.

### 0.8.3

- Added light CLI parser helpers for subparser creation, shared `--config`, shared run-mode flags, and shared delete-confirmation flags.
- Preserved command-specific help output and command flag visibility.

### 0.8.2

- Added shared `tags_text()` display helper and removed duplicate `_tags_text()` formatting helpers from sync/prune paths.

### 0.8.1

- Refactored safer config parsing patterns with shared table, optional-string, positive-integer, stripped-string, boolean, and integer helpers.
- Kept password/password_file pair validation explicit for a later, more focused refactor.

### 0.8.0

- Version-only bump from 0.7.10.

### 0.7.10

- Refactored pipeline stream reader setup into one compact stream-routing table.
- Preserved successful btrfs/mbuffer stderr routing to `.btrfs`/`.mbuffer` without polluting `.err`.

### 0.7.9

- Shared state metadata refresh/report/save logic between sync and prune without changing send/receive, retention, or parent UUID behavior.

### 0.7.3

- Consolidated source cache listing helpers around `remote_list_child_subvolumes`, `remote_cache_contains`, and cache child display formatting.
- Removed older overlapping cache list parsing/existence helpers while keeping the same cache parent cleanup behavior.

### 0.7.2

- Consolidated parent/source UUID matching into one shared helper, `match_source_path_to_destination_received_uuid` internally, so parent selection and sync-floor validation use the same Btrfs identity rule.
- Removed older overlapping helper code for parent/source UUID checks while keeping the same strict behavior: source path UUID must match destination received UUID or trusted state UUID history before it can be used.

### 0.6.11

- Reused one parsed Timeshift source snapshot index per sync stage.
- Manual snapshot creation still re-reads `timeshift --list` after creating a new snapshot, but metadata refresh, manual identity checks, sync-floor checks, parent selection, and the sync loop now share the same source index for that stage.

### 0.6.8

- Fixed successful transfer pipeline stderr handling. Successful `btrfs send` status lines such as `At subvol ...` and mbuffer progress no longer make `.err` non-empty.
- Transfer stderr is now buffered and copied to `.err` only if the send/mbuffer/receive pipeline fails.
- Btrfs transfer status is still written to `.btrfs`, and mbuffer progress is still written to `.mbuffer`.

### 0.6.7

- Added a separate `.succes` run log for readable sync and retention statistics.
- Sync summaries and retention delete plans are written to `.succes` and still shown in the terminal, instead of being mixed into the normal `.log` file.
- Email notifications use non-empty `.succes` text as the plain-text message body when present.
- Email log attachments are conditional and include only non-empty `.log`, `.err`, `.btrfs`, `.mbuffer`, and `.succes` files.
- Renamed the Btrfs verbose-output log suffix from `.btrfs-out` to `.btrfs`.

### 0.6.5

- Removed legacy config-option compatibility checks from the runtime config loader.
- Unknown removed config keys are no longer handled by special warning/error branches. The loader now only parses the active configuration needed by current functionality.
- Removed stale documentation text describing those old compatibility warnings.

### 0.6.4

- Destination retention uses only native Timeshift tags: H, D, W, M, B, and O.
- Non-native retention categories are not part of the active config model, examples, embedded `init-config` output, docs, or retention tag map.

### 0.6.3

- Added strict incremental parent source-path selection. For an existing destination parent, the app checks the saved state `send_path` first and requires its current source UUID to match the destination `received_uuid`.
- If the saved `send_path` is missing or does not match, the app tries the original Timeshift source path and only uses it if its UUID matches the destination `received_uuid`.
- Parent selection never creates a replacement cache snapshot. A recreated cache snapshot has a new UUID and cannot be a valid parent for an already received destination snapshot.

### 0.6.2

- Refreshed mutable Timeshift metadata in `state.json` from the latest `timeshift --list` during sync. Existing synced snapshots update `tags`, `comment`, `created`, and top-level target-relative `path` without re-sending data or changing UUID/parent/send identity fields.
- Improved Timeshift tag parsing so separated tag tokens such as `B H D W M` are recognized as tags instead of partly becoming the comment.

### 0.6.1

- Clarified and guarded the automatic manual/on-demand snapshot flow: the app may create a source-side Timeshift snapshot before syncing, but it never sends that snapshot directly or as a special priority target.
- After creating a manual snapshot, sync re-reads `timeshift --list`, reports newly detected snapshot names, and sends them only through the normal oldest-to-newest sync loop.

### 0.6.0

- Version-only bump.
